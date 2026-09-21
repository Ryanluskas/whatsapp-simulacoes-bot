"""E2E de PROCESSO REAL com periféricos simulados.

O que é real aqui
-----------------
* ``main.py`` sobe de verdade, como subprocesso: uvicorn, rota do webhook,
  fila, SQLite, laço de reenvio, recuperação no boot e o renderizador com
  Chromium de verdade.
* A conversa com a Evolution é HTTP de verdade, numa porta local.
* O simulador é o modo ``SIMULATOR_MODE=remote``: um agente falso fala o
  protocolo real ``/api/agent/claim|stage|result``.
* O "reinício" é um ``kill`` duro do processo no meio da fila.

O que NÃO é real
----------------
* A Evolution é um servidor falso que responde no formato da v2. Ele não
  prova que a Evolution de verdade aceita o payload, nem que o WhatsApp
  mostra a citação no celular.
* O Santander não é tocado: o agente devolve um resultado derivado do CPF.

Uso::

    .venv/Scripts/python.exe ferramentas/e2e_simulado.py
    .venv/Scripts/python.exe ferramentas/e2e_simulado.py --relatorio e2e.json

Sai com código 0 só se todos os cenários passarem.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.renderer import PngInvalido, validar_png  # noqa: E402

GRUPO = "120363400000000001@g.us"
API_KEY = "e2e-chave-da-evolution"
WEBHOOK_TOKEN = "e2e-token-do-webhook"
AGENT_TOKEN = "e2e-token-do-agente"
INSTANCIA = "allana"

# CPFs válidos, um por cenário, para nada se misturar entre eles.
CPFS = {
    "A": "52998224725", "B1": "11144477735", "B2": "12345678909",
    "C": "40481494235", "D": "98765432100", "E": "39053344705",
    "F": "71428793860", "G1": "15350946056", "G2": "86288366757",
    "G3": "39825979194", "H": "35623012353",
    "I": "78778932807", "J": "74852605700",
}


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def agora() -> str:
    return time.strftime("%H:%M:%S")


# ============================================================ Evolution falsa
def resposta_v237(payload: dict, key_id: str, *, imagem: bool, png_sha: str = "") -> dict:
    """A resposta de ``sendText``/``sendMedia`` como a Evolution v2.3.7 devolve.

    Tirada do codigo-fonte da tag 2.3.7, nao de documentacao: o controller
    devolve o ``prepareMessage(messageSent)`` de ``whatsapp.baileys.service.ts``.
    O que importa para o bot:

    * o ``contextInfo`` sobe para o TOPO do objeto;
    * no texto, o ``extendedTextMessage`` e' APAGADO e vira
      ``message.conversation`` -- entao ali nao ha' ``stanzaId`` nenhum;
    * na imagem, o ``contextInfo`` fica no topo E dentro de ``imageMessage``;
    * sem citacao, ``contextInfo`` nao aparece;
    * ``stanzaId`` e ``participant`` vem do ``quoted`` pedido (Baileys,
      ``generateWAMessageFromContent``).

    A versao anterior desta Evolution falsa punha o ``stanzaId`` dentro de
    ``message.extendedTextMessage`` -- formato que a v2.3.7 nunca devolve
    para texto. A leitura do bot procura em qualquer nivel e passava; o E2E so'
    nao provava isso contra o formato real.
    """
    contexto = None
    citado = payload.get("quoted")
    if isinstance(citado, dict):
        chave = citado.get("key") or {}
        contexto = {"stanzaId": chave.get("id"),
                    "participant": chave.get("participant") or chave.get("remoteJid"),
                    "quotedMessage": citado.get("message")}
    if imagem:
        conteudo = {"caption": payload.get("caption"), "mimetype": "image/png",
                    "fileSha256": png_sha}
        if contexto:
            conteudo["contextInfo"] = contexto
        mensagem, tipo = {"imageMessage": conteudo}, "imageMessage"
    else:
        mensagem, tipo = {"conversation": payload.get("text")}, "conversation"
    corpo = {"key": {"remoteJid": payload.get("number"), "fromMe": True, "id": key_id},
             "pushName": "Você", "status": "PENDING", "message": mensagem,
             "messageType": tipo, "messageTimestamp": int(time.time()),
             "instanceId": "00000000-0000-4000-8000-00000000e2e0",
             # getDevice(key.id) do Baileys; este id falso nao casa com nenhum padrao.
             "source": "unknown"}
    if contexto:
        corpo["contextInfo"] = contexto
    return corpo


class EvolutionFalsa:
    """Servidor HTTP no formato da Evolution v2, com falhas programáveis."""

    def __init__(self, pasta: Path) -> None:
        self.pasta = pasta
        self.chamadas: list[dict] = []
        self.regras: list[dict] = []
        self._lock = threading.Lock()
        self._seq = 0
        self.porta = porta_livre()
        falsa = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silencioso
                pass

            def _responder(self, status: int, corpo) -> None:
                dados = json.dumps(corpo).encode() if not isinstance(corpo, bytes) else corpo
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(dados)))
                self.end_headers()
                self.wfile.write(dados)

            def do_GET(self):
                if self.headers.get("apikey") != API_KEY:
                    return self._responder(401, {"error": "Unauthorized"})
                if self.path.startswith("/instance/connectionState/"):
                    return self._responder(200, {"instance": {"instanceName": INSTANCIA,
                                                              "state": "open"}})
                return self._responder(404, {"error": "not found"})

            def do_POST(self):
                tamanho = int(self.headers.get("Content-Length") or 0)
                bruto = self.rfile.read(tamanho) if tamanho else b""
                if self.headers.get("apikey") != API_KEY:
                    return self._responder(401, {"error": "Unauthorized"})
                try:
                    payload = json.loads(bruto or b"{}")
                except ValueError:
                    return self._responder(400, {"error": "json"})
                status, corpo = falsa.atender(self.path, payload)
                self._responder(status, corpo)

        self.servidor = ThreadingHTTPServer(("127.0.0.1", self.porta), Handler)
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()

    def atender(self, rota: str, payload: dict) -> tuple[int, dict]:
        with self._lock:
            self._seq += 1
            seq = self._seq
            regra = next((r for r in self.regras if r["vezes"] > 0
                          and r["rota"] in rota
                          and (r.get("citado") is None or r["citado"] == ("quoted" in payload))
                          and r.get("marca", "") in json.dumps(payload, ensure_ascii=False)),
                         None)
            if regra:
                regra["vezes"] -= 1
            registro = {"seq": seq, "rota": rota, "quando": time.time(),
                        "number": payload.get("number"),
                        "quoted_id": ((payload.get("quoted") or {}).get("key") or {}).get("id"),
                        "quoted": payload.get("quoted"),
                        "text": payload.get("text"), "caption": payload.get("caption"),
                        "fileName": payload.get("fileName"),
                        "mediatype": payload.get("mediatype")}
            if payload.get("media"):
                dados = base64.b64decode(payload["media"])
                arquivo = self.pasta / f"recebido-{seq}.png"
                arquivo.write_bytes(dados)
                try:
                    validar_png(arquivo)
                    registro["png_valido"] = True
                except PngInvalido as exc:
                    registro["png_valido"] = str(exc)
                registro["png_sha"] = hashlib.sha256(dados).hexdigest()[:16]
            self.chamadas.append(registro)

        # Responder devagar: e' assim que o roteiro mata o bot com a requisicao
        # JA' aceita aqui dentro -- o caso em que a mensagem pode ter saido.
        if regra and regra.get("atraso"):
            time.sleep(regra["atraso"])

        if regra and regra.get("status"):
            registro["status"] = regra["status"]
            return regra["status"], {"status": regra["status"], "error": "simulado",
                                     "response": {"message": [regra.get("motivo", "falha simulada")]}}

        key_id = f"BAE5E2E{seq:06d}"
        registro["status"] = 201
        registro["key_id"] = key_id
        return 201, resposta_v237(payload, key_id, imagem="sendMedia" in rota,
                                  png_sha=registro.get("png_sha", ""))

    def envios(self, marca: str = "") -> list[dict]:
        with self._lock:
            return [c for c in self.chamadas if "/message/send" in c["rota"]
                    and (not marca or marca in json.dumps(c, ensure_ascii=False))]

    def regra(self, **r) -> None:
        with self._lock:
            self.regras.append(r)


# ============================================================== agente falso
class AgenteFalso:
    """Fala o protocolo real do agente. Resultado derivado do CPF."""

    def __init__(self, url: str, atrasos: dict[str, float], trabalhadores: int = 2) -> None:
        self.url = url
        self.atrasos = atrasos
        self.claims: list[tuple[str, int]] = []
        self.parar = threading.Event()
        self._lock = threading.Lock()
        self.threads = [threading.Thread(target=self._laco, args=(i,), daemon=True)
                        for i in range(trabalhadores)]

    def iniciar(self) -> None:
        for t in self.threads:
            t.start()

    def _post(self, cliente: httpx.Client, rota: str, dados: dict | None = None):
        return cliente.post(f"{self.url}{rota}", json=dados or {},
                            headers={"x-agent-token": AGENT_TOKEN, "x-agent-name": "e2e"})

    def _laco(self, indice: int) -> None:
        with httpx.Client(timeout=10) as cliente:
            while not self.parar.is_set():
                try:
                    r = self._post(cliente, "/api/agent/claim")
                except httpx.HTTPError:
                    time.sleep(0.5)
                    continue
                if r.status_code != 200:
                    time.sleep(0.3)
                    continue
                tarefa = r.json()
                rid, cpf = tarefa["request_id"], tarefa["cpf"]
                with self._lock:
                    self.claims.append((rid, int(tarefa.get("attempt") or 1)))
                try:
                    self._post(cliente, "/api/agent/stage", {"request_id": rid, "stage": "consulting"})
                    time.sleep(self.atrasos.get(cpf, 0.3))
                    self._post(cliente, "/api/agent/stage", {"request_id": rid, "stage": "extracting"})
                    self._post(cliente, "/api/agent/result", {
                        "request_id": rid, "simulation_id": tarefa["simulation_id"],
                        "attempt": tarefa.get("attempt"), "ok": True, "status": "Sim",
                        "reduction_value": float(int(cpf[-4:])), "margin": "",
                        "contracts": [{"contrato": f"7****{cpf[-2:]}", "parcelas": "12"}],
                        "installment_sum": 100.0, "installment_count": 12, "debt_sum": 900.0,
                        "motivos": []})
                except httpx.HTTPError:
                    # O painel caiu no meio: o resultado se perde, e o painel,
                    # ao voltar, recoloca a solicitação na fila.
                    time.sleep(0.5)

    def claims_de(self, request_id: str) -> list[int]:
        with self._lock:
            return [a for r, a in self.claims if r == request_id]


# ======================================================================= bot
class Bot:
    def __init__(self, pasta: Path, porta_evolution: int) -> None:
        self.pasta = pasta
        self.porta = porta_livre()
        self.url = f"http://127.0.0.1:{self.porta}"
        self.db = pasta / "e2e.db"
        self.env = pasta / "e2e.env"
        self.log = pasta / "bot.log"
        self.env.write_text("\n".join([
            "WHATSAPP_MODE=evolution",
            # A Evolution daqui e' a FALSA: nada de ligar o WSL nem reapontar
            # o webhook da Evolution real que pode estar escutando nesta maquina.
            "EVOLUTION_AUTOSTART=false",
            f"EVOLUTION_URL=http://127.0.0.1:{porta_evolution}",
            f"EVOLUTION_API_KEY={API_KEY}",
            f"EVOLUTION_INSTANCE={INSTANCIA}",
            f"EVOLUTION_GROUP_JID={GRUPO}",
            f"EVOLUTION_WEBHOOK_TOKEN={WEBHOOK_TOKEN}",
            "WHATSAPP_GROUP_NAME=Consultores E2E",
            "SIMULATOR_MODE=remote",
            f"AGENT_TOKEN={AGENT_TOKEN}",
            "WORKER_COUNT=2",
            "MAX_ATTEMPTS=2",
            "JOB_TIMEOUT_SECONDS=60",
            "SEND_RESULT_IMAGE=true",
            f"DB_PATH={pasta / 'e2e.db'}",
            f"STATE_PATH={pasta / 'state.json'}",
            f"COMPROVANTES_DIR={pasta / 'comprovantes'}",
            f"WHATSAPP_PROFILE_DIR={pasta / 'wa-profile'}",
            f"SIMULATOR_PROFILE_DIR={pasta / 'sim-profile'}",
            f"SIM_BOT_PATH={pasta / 'arqueiro'}",
            "WEB_HOST=127.0.0.1",
            f"WEB_PORT={self.porta}",
            "DASHBOARD_PASSWORD=e2e-senha-que-nao-e-padrao",
            "SESSION_SECRET=e2e-segredo-de-sessao",
            "MASK_CPF_IN_UI=true",
        ]) + "\n", encoding="utf-8")
        self.proc: subprocess.Popen | None = None

    def subir(self, timeout: float = 60.0) -> None:
        saida = self.log.open("a", encoding="utf-8")
        saida.write(f"\n===== subindo {agora()} =====\n")
        saida.flush()
        self.proc = subprocess.Popen([sys.executable, "main.py", "--env", str(self.env)],
                                     cwd=ROOT, stdout=saida, stderr=subprocess.STDOUT)
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            if self.proc.poll() is not None:
                raise RuntimeError(f"main.py saiu com {self.proc.returncode}; veja {self.log}")
            try:
                if httpx.get(f"{self.url}/api/health", timeout=2).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.4)
        raise RuntimeError("main.py não respondeu /api/health a tempo")

    def matar(self) -> None:
        """Queda dura: sem encerramento limpo, como faltar energia."""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=20)

    def parar(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def webhook(self, texto: str, id_msg: str, participante: str, push: str) -> httpx.Response:
        return httpx.post(f"{self.url}/webhook/whatsapp", timeout=20,
                          headers={"X-Webhook-Token": WEBHOOK_TOKEN},
                          json={"event": "messages.upsert", "instance": INSTANCIA,
                                "data": {"key": {"remoteJid": GRUPO, "fromMe": False,
                                                 "id": id_msg, "participant": participante},
                                         "pushName": push,
                                         "message": {"conversation": texto},
                                         "messageTimestamp": int(time.time())}})

    # ------------------------------------------------------------------ banco
    def consultar(self, sql: str, params=()) -> list[dict]:
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(sql, params)]
        finally:
            con.close()

    def solicitacao(self, id_msg: str) -> dict | None:
        linhas = self.consultar("SELECT * FROM simulations WHERE source_message_id=?", (id_msg,))
        return linhas[0] if linhas else None

    def esperar(self, id_msgs: list[str], estados=("delivered",), timeout: float = 60.0) -> bool:
        limite = time.monotonic() + timeout
        while time.monotonic() < limite:
            try:
                linhas = [self.solicitacao(m) for m in id_msgs]
                if all(l and l.get("delivery_status") in estados for l in linhas):
                    return True
            except sqlite3.Error:
                pass
            time.sleep(0.5)
        return False


# =================================================================== cenários
class Verificador:
    def __init__(self) -> None:
        self.resultados: list[dict] = []

    def cenario(self, nome: str, fn) -> None:
        inicio = time.monotonic()
        falhas: list[str] = []
        detalhes: dict = {}

        def conferir(condicao: bool, mensagem: str) -> None:
            if not condicao:
                falhas.append(mensagem)

        print(f"[{agora()}] >>> {nome}", flush=True)
        try:
            fn(conferir, detalhes)
        except Exception as exc:  # o cenário falha, o roteiro segue
            falhas.append(f"exceção: {exc!r}")
        ok = not falhas
        self.resultados.append({"cenario": nome, "ok": ok, "falhas": falhas,
                                "segundos": round(time.monotonic() - inicio, 1),
                                "detalhes": detalhes})
        print(f"[{agora()}] {'PASSOU' if ok else 'FALHOU'} {nome}"
              + ("" if ok else f": {falhas}"), flush=True)


def pedido(nome: str, cpf: str) -> str:
    return f"{nome}\nGoiás\n{cpf}"


def rodar(relatorio: Path | None) -> int:
    pasta = Path(tempfile.mkdtemp(prefix="allana-e2e-"))
    evolution = EvolutionFalsa(pasta)
    bot = Bot(pasta, evolution.porta)
    atrasos = {CPFS["A"]: 0.5, CPFS["B1"]: 3.0, CPFS["B2"]: 0.3,
               CPFS["G1"]: 6.0, CPFS["G2"]: 6.0, CPFS["G3"]: 6.0}
    v = Verificador()
    print(f"pasta: {pasta}\nbot: {bot.url}  evolution: {evolution.porta}", flush=True)

    bot.subir()
    agente = AgenteFalso(bot.url, atrasos)
    agente.iniciar()

    # ------------------------------------------------------------------ A
    def cenario_a(conferir, det):
        r = bot.webhook(pedido("Cliente Teste", CPFS["A"]), "E2E-A", "5562900000001@s.whatsapp.net", "Ana")
        conferir(r.status_code == 200, f"webhook respondeu {r.status_code}")
        conferir(bot.esperar(["E2E-A"]), "não entregou em 60s")
        linha = bot.solicitacao("E2E-A") or {}
        det["linha"] = {k: linha.get(k) for k in ("request_id", "status", "stage", "delivery_status",
                                                    "quote_status", "media_status", "sent_message_id")}
        conferir(len(bot.consultar("SELECT id FROM simulations WHERE source_message_id='E2E-A'")) == 1,
                 "não é exatamente uma solicitação")
        envios = evolution.envios(linha.get("request_id", "?"))
        conferir(len(envios) == 1, f"{len(envios)} envios em vez de 1")
        if envios:
            e = envios[0]
            conferir(e["rota"].endswith(f"sendMedia/{INSTANCIA}"), "não saiu como imagem")
            conferir(e["mediatype"] == "image", "mediatype não é image")
            conferir(e["quoted_id"] == "E2E-A", f"citou {e['quoted_id']}")
            conferir(e["number"] == GRUPO, "chat errado")
            conferir(e.get("png_valido") is True, f"PNG no fio inválido: {e.get('png_valido')}")
            conferir(e["fileName"] == f"{linha.get('request_id')}.png", "arquivo de outro pedido")
            conferir(linha.get("sent_message_id") == e.get("key_id"), "sent_message_id não bate")
        conferir(linha.get("stage") == "completed" and linha.get("quote_status") == "ok",
                 "estado final incoerente")
        png = pasta / "comprovantes" / f"{linha.get('request_id')}.png"
        conferir(png.exists(), "comprovante não gravado")

    v.cenario("A — um consultor, uma solicitação", cenario_a)

    # ------------------------------------------------------------------ B
    def cenario_b(conferir, det):
        ts = [threading.Thread(target=bot.webhook, args=(pedido("Cliente Lento", CPFS["B1"]), "E2E-B1",
                                                         "5562900000011@s.whatsapp.net", "Bruno")),
              threading.Thread(target=bot.webhook, args=(pedido("Cliente Rapido", CPFS["B2"]), "E2E-B2",
                                                         "5562900000012@s.whatsapp.net", "Carla"))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        conferir(bot.esperar(["E2E-B1", "E2E-B2"]), "não entregou as duas")
        b1, b2 = bot.solicitacao("E2E-B1") or {}, bot.solicitacao("E2E-B2") or {}
        det["ordem_replied_at"] = [b1.get("replied_at"), b2.get("replied_at")]
        conferir(b1.get("request_id") != b2.get("request_id"), "request_id repetido")
        conferir((b2.get("replied_at") or "") < (b1.get("replied_at") or "~"),
                 "o rápido não terminou antes (cenário sem inversão)")
        for linha, mid, consultor, cpf in ((b1, "E2E-B1", "Bruno", CPFS["B1"]),
                                           (b2, "E2E-B2", "Carla", CPFS["B2"])):
            envios = evolution.envios(linha.get("request_id", "?"))
            conferir(len(envios) == 1, f"{mid}: {len(envios)} envios")
            for e in envios:
                conferir(e["quoted_id"] == mid, f"{mid} citou {e['quoted_id']}")
                conferir(cpf in json.dumps(e["quoted"]), f"{mid}: citação com texto de outro")
            conferir(linha.get("consultant_name") == consultor, f"{mid}: consultor trocado")
            conferir(linha.get("reduction_value") == float(int(cpf[-4:])), f"{mid}: resultado cruzado")

    v.cenario("B — dois consultores simultâneos, isolados", cenario_b)

    # ------------------------------------------------------------------ C
    def cenario_c(conferir, det):
        # A marca e' o nome do cliente: esta' na legenda COM e SEM citacao.
        evolution.regra(rota="sendMedia", status=400, vezes=2, marca="Imagem Falha",
                        motivo="Owned media must be a url or base64")
        bot.webhook(pedido("Imagem Falha", CPFS["C"]), "E2E-C", "5562900000021@s.whatsapp.net", "Diego")
        conferir(bot.esperar(["E2E-C"]), "não entregou")
        linha = bot.solicitacao("E2E-C") or {}
        envios = evolution.envios(linha.get("request_id", "?"))
        det["rotas"] = [(e["rota"], e["status"]) for e in envios]
        conferir(envios and envios[-1]["rota"].endswith(f"sendText/{INSTANCIA}")
                 and envios[-1]["status"] == 201, "a resposta final não saiu em texto")
        conferir(linha.get("media_status") == "send_failed", f"media_status={linha.get('media_status')}")
        conferir(linha.get("stage") == "completed", f"stage={linha.get('stage')}")

    v.cenario("C — imagem falha, texto chega", cenario_c)

    # ------------------------------------------------------------------ D
    def cenario_d(conferir, det):
        evolution.regra(rota="send", citado=True, status=400, vezes=1, marca="E2E-D",
                        motivo="quoted message not found in the instance")
        bot.webhook(pedido("Citacao Falha", CPFS["D"]), "E2E-D", "5562900000031@s.whatsapp.net", "Elisa")
        conferir(bot.esperar(["E2E-D"]), "não entregou")
        linha = bot.solicitacao("E2E-D") or {}
        envios = evolution.envios(linha.get("request_id", "?"))
        det["envios"] = [(e["rota"], e["status"], bool(e["quoted_id"])) for e in envios]
        conferir(len(envios) == 2, f"{len(envios)} envios")
        if len(envios) == 2:
            conferir(envios[0]["quoted_id"] == "E2E-D" and envios[0]["status"] == 400, "1º não era o citado")
            conferir(envios[1]["quoted_id"] is None and envios[1]["status"] == 201, "2º não saiu sem citação")
            conferir("↩ Elisa" in (envios[1]["caption"] or envios[1]["text"] or ""),
                     "fallback sem identificar o consultor")
        conferir(linha.get("quote_status") == "fallback", f"quote_status={linha.get('quote_status')}")
        conferir(linha.get("delivery_status") == "delivered", "não conta como entregue")

    v.cenario("D — citação falha, resposta sem citação + identificação", cenario_d)

    # --------------------------------------------------------------- E e F
    def transitorio(status: int, chave: str, mid: str, consultor: str):
        def cenario(conferir, det):
            evolution.regra(rota="send", status=status, vezes=2, marca=mid)   # imagem e texto
            bot.webhook(pedido(f"Transitorio {status}", CPFS[chave]), mid,
                        f"55629000000{status}@s.whatsapp.net", consultor)
            conferir(bot.esperar([mid], estados=("retrying",), timeout=40),
                     "não ficou em retrying")
            antes = bot.solicitacao(mid) or {}
            det["antes"] = {k: antes.get(k) for k in ("status", "stage", "delivery_status", "replied_at")}
            conferir(antes.get("replied_at") is None, "marcou entregue sem entregar")
            conferir(bot.esperar([mid], timeout=150), "o laço de reenvio não entregou em 150s")
            depois = bot.solicitacao(mid) or {}
            det["depois"] = {k: depois.get(k) for k in ("request_id", "stage", "delivery_status",
                                                         "reply_attempts", "sent_message_id")}
            conferir(depois.get("request_id") == antes.get("request_id"), "retry mudou o request_id")
            conferir(len(bot.consultar("SELECT id FROM simulations WHERE source_message_id=?", (mid,))) == 1,
                     "retry criou solicitação nova")
            ok = [e for e in evolution.envios(depois.get("request_id", "?")) if e["status"] == 201]
            conferir(len(ok) == 1, f"{len(ok)} envios bem-sucedidos")
            conferir(all(e["quoted_id"] == mid for e in ok), "reenvio citou outra mensagem")
            conferir(agente.claims_de(depois.get("request_id", "?")) == [1],
                     "reenvio de ENTREGA re-simulou no agente")
        return cenario

    fe = threading.Thread(target=v.cenario, args=("E — Evolution 429, retry no mesmo request",
                                                   transitorio(429, "E", "E2E-E", "Fabio")))
    ff = threading.Thread(target=v.cenario, args=("F — Evolution 503, retry no mesmo request",
                                                   transitorio(503, "F", "E2E-F", "Gabi")))
    fe.start()
    ff.start()
    fe.join()
    ff.join()

    # ---------------------------------------------------------------- G e H
    def cenario_h_antes(conferir, det):
        texto = pedido("Duplicado", CPFS["H"])
        for _ in range(3):
            bot.webhook(texto, "E2E-H", "5562900000081@s.whatsapp.net", "Hugo")
        ts = [threading.Thread(target=bot.webhook, args=(texto, "E2E-H", "5562900000081@s.whatsapp.net", "Hugo"))
              for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        conferir(bot.esperar(["E2E-H"]), "não entregou")
        qtd = len(bot.consultar("SELECT id FROM simulations WHERE source_message_id='E2E-H'"))
        conferir(qtd == 1, f"{qtd} solicitações para a mesma mensagem (esperava 1)")

    v.cenario("H — webhook duplicado (sequencial e paralelo)", cenario_h_antes)

    def cenario_g(conferir, det):
        mids = ["E2E-G1", "E2E-G2", "E2E-G3"]
        for i, mid in enumerate(mids, start=1):
            bot.webhook(pedido(f"Reinicio {i}", CPFS[f"G{i}"]), mid, f"55629000009{i}@s.whatsapp.net", f"Iris{i}")
        limite = time.monotonic() + 30
        while time.monotonic() < limite and not any(
                (bot.solicitacao(m) or {}).get("status") == "processing" for m in mids):
            time.sleep(0.2)
        time.sleep(1.5)   # no meio da simulação, com uma ainda na fila
        antes = {m: (bot.solicitacao(m) or {}).get("status") for m in mids}
        det["antes_do_kill"] = antes
        bot.matar()
        det["kill"] = agora()
        time.sleep(2)
        bot.subir()
        det["subiu"] = agora()
        conferir(bot.esperar(mids, timeout=120), "não recuperou e entregou as três após reinício")
        for mid in mids:
            linha = bot.solicitacao(mid) or {}
            rid = linha.get("request_id", "?")
            qtd = len(bot.consultar("SELECT id FROM simulations WHERE source_message_id=?", (mid,)))
            conferir(qtd == 1, f"{mid}: {qtd} solicitações (esperava 1)")
            ok = [e for e in evolution.envios(rid) if e["status"] == 201]
            conferir(len(ok) == 1, f"{mid}: {len(ok)} respostas entregues")
            conferir(all(e["quoted_id"] == mid for e in ok), f"{mid}: citou outra mensagem")
            det[mid] = {"request_id": rid, "tentativas_no_agente": agente.claims_de(rid),
                        "attempts": linha.get("attempts")}
        # duplicata DEPOIS do reinício (a trava é do banco, não da RAM)
        r = bot.webhook(pedido("Duplicado", CPFS["H"]), "E2E-H", "5562900000081@s.whatsapp.net", "Hugo")
        conferir(r.json().get("ignorado") == "já processado", f"reentrega pós-reinício: {r.json()}")
        conferir(len(bot.consultar("SELECT id FROM simulations WHERE source_message_id='E2E-H'")) == 1,
                 "reentrega pós-reinício criou solicitação")

    v.cenario("G — processo morto no meio da fila (+ H após reinício)", cenario_g)

    # ------------------------------------------------------------------ I
    def cenario_i(conferir, det):
        """500 não prova que nada saiu: nada de segunda mensagem."""
        evolution.regra(rota="send", status=500, vezes=2, marca="Erro Quinhentos",
                        motivo="Internal server error")
        bot.webhook(pedido("Erro Quinhentos", CPFS["I"]), "E2E-I",
                    "5562900000101@s.whatsapp.net", "Joana")
        conferir(bot.esperar(["E2E-I"], estados=("unconfirmed",), timeout=60),
                 "não ficou como entrega incerta")
        linha = bot.solicitacao("E2E-I") or {}
        det["linha"] = {k: linha.get(k) for k in ("stage", "delivery_status", "quote_status",
                                                    "sent_message_id", "replied_at")}
        envios = evolution.envios(linha.get("request_id", "?"))
        det["envios"] = [(e["rota"], e["status"]) for e in envios]
        conferir(len(envios) == 1, f"{len(envios)} envios depois de um 500 (esperava 1)")
        conferir(linha.get("sent_message_id") in ("", None), "inventou prova de entrega")
        conferir(linha.get("replied_at") is None, "marcou entregue sem prova")
        # e o laço de reenvio (30 s) não pode mandar nada depois
        time.sleep(45)
        conferir(len(evolution.envios(linha.get("request_id", "?"))) == 1,
                 "o laço de reenvio duplicou uma entrega incerta")

    v.cenario("I — HTTP 500 vira entrega incerta, sem segunda mensagem", cenario_i)

    # ------------------------------------------------------------------ J
    def cenario_j(conferir, det):
        """O pior caso: a Evolution ACEITOU e o processo morreu antes de gravar."""
        evolution.regra(rota="sendMedia", vezes=1, marca="Voo Cortado", atraso=25)
        bot.webhook(pedido("Voo Cortado", CPFS["J"]), "E2E-J",
                    "5562900000111@s.whatsapp.net", "Karla")

        limite = time.monotonic() + 60
        while time.monotonic() < limite and not evolution.envios("Voo Cortado"):
            time.sleep(0.2)
        aceitos = evolution.envios("Voo Cortado")
        conferir(bool(aceitos), "a Evolution não chegou a receber o envio")
        det["post_recebido"] = agora()
        bot.matar()                      # morre com a requisição já aceita lá
        det["kill"] = agora()
        time.sleep(2)
        bot.subir()
        det["subiu"] = agora()

        conferir(bot.esperar(["E2E-J"], estados=("unconfirmed",), timeout=60),
                 "não marcou a entrega como incerta depois do reinício")
        linha = bot.solicitacao("E2E-J") or {}
        det["linha"] = {k: linha.get(k) for k in ("status", "stage", "delivery_status",
                                                    "delivery_error")}
        time.sleep(45)                   # dá tempo do laço de reenvio rodar
        depois = evolution.envios(linha.get("request_id", "?"))
        det["envios"] = [(e["rota"], e.get("status")) for e in depois]
        conferir(len(depois) <= 1, f"{len(depois)} envios: reenviou algo que pode ter chegado")
        saidas = bot.consultar(
            "SELECT status FROM messages WHERE direction='out' AND request_id=?",
            (linha.get("request_id", "?"),))
        conferir(all(s["status"] in ("unconfirmed", "sending") for s in saidas),
                 f"saídas em estado inesperado: {[s['status'] for s in saidas]}")

    v.cenario("J — processo morto com o POST já aceito: incerta, sem duplicar", cenario_j)

    # --------------------------------------------------------------- segurança
    def cenario_seguranca(conferir, det):
        logs = " ".join(r["message"] or "" for r in bot.consultar("SELECT message FROM logs"))
        eventos = " ".join(r["payload_json"] or "" for r in bot.consultar("SELECT payload_json FROM events"))
        for cpf in CPFS.values():
            conferir(cpf not in logs, f"CPF completo no log: {cpf[:3]}...")
            conferir(cpf not in eventos, f"CPF completo nos eventos: {cpf[:3]}...")
        texto_log = bot.log.read_text(encoding="utf-8", errors="replace")
        conferir(API_KEY not in logs and API_KEY not in texto_log, "chave da Evolution vazou")

    v.cenario("Logs sem CPF completo e sem chave", cenario_seguranca)

    agente.parar.set()
    bot.parar()
    evolution.servidor.shutdown()

    passou = sum(1 for r in v.resultados if r["ok"])
    print(f"\n{passou}/{len(v.resultados)} cenários passaram. Pasta: {pasta}", flush=True)
    for r in v.resultados:
        print(f"  {'OK ' if r['ok'] else 'X  '} {r['cenario']} ({r['segundos']}s)"
              + ("" if r["ok"] else f" -> {r['falhas']}"))
    if relatorio:
        relatorio.write_text(json.dumps({"pasta": str(pasta), "resultados": v.resultados},
                                        ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if passou == len(v.resultados) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--relatorio", type=Path, default=None)
    raise SystemExit(rodar(parser.parse_args().relatorio))
