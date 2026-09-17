"""O fluxo de produção inteiro, na camada Evolution, com identidade como verdade.

UMA mensagem recebida -> UMA solicitação -> UM request_id -> UM message_id
-> UM resultado. Estes testes sobem o manager, a fila, o banco e a rota do
webhook de verdade. Só o que está fora do processo é substituído:

* a Evolution vira um servidor HTTP falso (``httpx.MockTransport``) que
  responde no formato real -- inclusive ``contextInfo.stanzaId``, que é o
  que permite conferir se a citação pegou;
* o Santander vira um simulador que devolve um resultado derivado do próprio
  job (qualquer cruzamento aparece no valor);
* o renderizador grava um PNG pequeno e VÁLIDO.

A primeira parte cobre os 20 comportamentos pedidos; a segunda, os dez
defeitos conhecidos, um teste de regressão para cada.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clock import iso_atras
from app.db import ENTRADA_SOLICITACAO, Database
from app.evolution import EvolutionClient
from app.events import EventHub
from app.jobs import QueueService
from app.manager import BotManager
from app.models import (Delivery, IncomingMessage, QuoteStatus, SimulationJob,
                        SimulationResult, Stage, Status)
from app.renderer import PngInvalido, PngRenderer, validar_png
from app.web import create_app
from tests.test_concurrency import _aguardar, _config, png_valido

GRUPO = "120363111222333@g.us"
TOKEN = "token-do-webhook-de-producao"
CABECALHO = {"X-Webhook-Token": TOKEN}

CONSULTOR_A = "5562999990001@s.whatsapp.net"
CONSULTOR_B = "5562999990002@s.whatsapp.net"
CPF_A = "52998224725"
CPF_B = "11144477735"


# =================================================================== dublês
class EvolutionFalsa:
    """Servidor HTTP falso no formato da Evolution v2.

    ``roteiro`` é uma lista de funções ``(rota, payload) -> httpx.Response |
    None``, consumida em ordem; ``None`` (ou roteiro vazio) devolve a resposta
    normal. É assim que cada teste injeta 429, 503, 400 de citação, 2xx sem
    id... sem mexer no código de produção.
    """

    def __init__(self) -> None:
        self.chamadas: list[tuple[str, dict]] = []
        self.roteiro: list = []
        self.estado = "open"
        self._lock = threading.Lock()
        self._seq = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        rota = request.url.path
        if rota.startswith("/instance/connectionState"):
            return httpx.Response(200, json={"instance": {"state": self.estado}})
        corpo = json.loads(request.content) if request.content else {}
        with self._lock:
            self.chamadas.append((rota, corpo))
            passo = self.roteiro.pop(0) if self.roteiro else None
            self._seq += 1
            seq = self._seq
        if passo is not None:
            resposta = passo(rota, corpo)
            if resposta is not None:
                return resposta
        return httpx.Response(201, json=self.resposta_real(rota, corpo, f"BAE5{seq:08d}"))

    @staticmethod
    def resposta_real(rota: str, payload: dict, key_id: str) -> dict:
        imagem = "sendMedia" in rota
        tipo = "imageMessage" if imagem else "extendedTextMessage"
        conteudo: dict = ({"caption": payload.get("caption"), "mimetype": "image/png",
                           "fileSha256": "q1w2e3r4t5y6", "mediaKey": "SEGREDO-NAO-GRAVAR"}
                          if imagem else {"text": payload.get("text")})
        if "quoted" in payload:
            conteudo["contextInfo"] = {
                "stanzaId": payload["quoted"]["key"]["id"],
                "participant": payload["quoted"]["key"].get("participant"),
                "quotedMessage": payload["quoted"]["message"],
            }
        return {"key": {"remoteJid": payload.get("number"), "fromMe": True, "id": key_id},
                "status": "PENDING", "message": {tipo: conteudo}, "messageType": tipo}

    # ------------------------------------------------------------ consultas
    def envios(self) -> list[tuple[str, dict]]:
        with self._lock:
            return [(r, p) for r, p in self.chamadas if r.startswith("/message/send")]


def recusa(status: int, texto: str = "erro"):
    return lambda rota, payload: httpx.Response(status, text=texto)


def recusa_se_citar(status: int = 400):
    """A Evolution recusando a CITAÇÃO (ex.: mensagem original fora do histórico)."""
    def passo(rota, payload):
        if "quoted" in payload:
            return httpx.Response(status, json={"status": status, "error": "Bad Request",
                                                "response": {"message": ["quoted not found"]}})
        return None
    return passo


class RenderizadorFalso:
    def __init__(self, falha: bool = False, vazio: bool = False) -> None:
        self.falha = falha
        self.vazio = vazio
        self.caminhos: list[str] = []

    def start(self): pass
    def stop(self, timeout=10.0): pass

    def render_png(self, html, path, width=900, timeout=60.0):
        if self.falha:
            raise RuntimeError("o Chromium do renderizador caiu")
        destino = Path(path)
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_bytes(b"" if self.vazio else png_valido(semente=html.encode()[-3:]))
        self.caminhos.append(str(destino))
        return str(destino)


class SimuladorPorCpf:
    """Resultado derivado do job; atraso configurável por CPF.

    Com dois despachantes, o atraso por CPF é o que faz B terminar ANTES de
    A -- a ordem de conclusão diferente da ordem de chegada.
    """

    def __init__(self, name="sim", atrasos=None) -> None:
        self.name = name
        self.atrasos = atrasos or {}
        self.vistos: list[str] = []
        self.concluidos: list[str] = []
        self._lock = threading.Lock()

    def execute(self, job: SimulationJob, on_stage=None, timeout=None) -> SimulationResult:
        with self._lock:
            self.vistos.append(job.request_id)
        if on_stage:
            on_stage(Stage.CONSULTING)
        time.sleep(self.atrasos.get(job.request.cpf, 0.01))
        if on_stage:
            on_stage(Stage.EXTRACTING)
        with self._lock:
            self.concluidos.append(job.request_id)
        return SimulationResult(job=job, ok=True, status="Sim",
                                reduction_value=float(int(job.request.cpf[-4:])),
                                contracts=({"contrato": job.request.cpf[-6:]},),
                                installment_sum=10.0, installment_count=1)

    def start(self): ...
    def stop(self, *a, **k): ...
    def status(self): return {"name": self.name, "running": True, "ready": True, "busy": False}


# ================================================================== sistema
class Sistema:
    """Manager real em modo Evolution, com o webhook, a fila e o banco reais."""

    def __init__(self, tmp_path: Path, *, com_imagem: bool = True, workers: int = 1,
                 atrasos=None, renderizador=None, servidor=None, db_path=None) -> None:
        self.tmp_path = tmp_path
        self.config = _config(tmp_path, whatsapp_mode="evolution", send_image=com_imagem,
                              evolution_group_jid=GRUPO, evolution_webhook_token=TOKEN,
                              workers=workers)
        if db_path is not None:
            from dataclasses import replace
            self.config = replace(self.config, db_path=db_path)
        self.db = Database(self.config.db_path)
        self.hub = EventHub(self.db)
        self.manager = BotManager(self.config, self.db, self.hub)
        self.manager.comprovantes_dir = tmp_path / "comprovantes"

        self.servidor = servidor or EvolutionFalsa()
        self.renderizador = renderizador or RenderizadorFalso()
        self.manager.whatsapp = EvolutionClient(
            base_url="http://evolution:8080", api_key="k", instance="allana",
            group_jid=GRUPO, group_name="Consultores",
            client=httpx.Client(transport=httpx.MockTransport(self.servidor),
                                base_url="http://evolution:8080"),
            renderer=self.renderizador,
        )
        self.simuladores = [SimuladorPorCpf(name=f"sim-{i}", atrasos=atrasos)
                            for i in range(workers)]
        self.manager.simulators = self.simuladores
        self.manager.queue = QueueService(
            db=self.db, hub=self.hub, simulators=self.simuladores,
            max_attempts=2, job_timeout=30.0,
            on_result=self.manager._deliver_result, on_log=self.manager._queue_log)
        self._leitor: threading.Thread | None = None
        self.cliente: TestClient | None = None

    def ligar(self) -> "Sistema":
        self.manager.queue.start()
        self._leitor = threading.Thread(target=self.manager._inbox_loop, daemon=True)
        self._leitor.start()
        self.cliente = TestClient(create_app(self.config, self.db, self.hub, self.manager))
        self.cliente.__enter__()
        return self

    def desligar(self) -> None:
        self.manager._stop.set()
        if self._leitor:
            self._leitor.join(timeout=5)
        self.manager.queue.stop()
        if self.cliente:
            self.cliente.__exit__(None, None, None)

    # ---------------------------------------------------------------- ações
    def webhook(self, texto: str, id_msg: str, participante: str = CONSULTOR_A,
                push: str = "Ryan", grupo: str = GRUPO):
        return self.cliente.post("/webhook/whatsapp", headers=CABECALHO, json={
            "event": "messages.upsert", "instance": "allana",
            "data": {"key": {"remoteJid": grupo, "fromMe": False, "id": id_msg,
                             "participant": participante},
                     "pushName": push, "message": {"conversation": texto},
                     "messageTimestamp": 1756600000}})

    def linha(self, **onde) -> dict | None:
        chave, valor = next(iter(onde.items()))
        return self.db.fetchone(f"SELECT * FROM simulations WHERE {chave}=?", (valor,))

    def esperar_desfecho(self, quantas: int = 1, timeout: float = 20.0) -> bool:
        return _aguardar(lambda: self.db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE COALESCE(delivery_status,'') "
            "NOT IN ('', 'pending')") >= quantas, timeout=timeout)


@pytest.fixture()
def sistema(tmp_path):
    s = Sistema(tmp_path).ligar()
    try:
        yield s
    finally:
        s.desligar()


def _pedido(nome: str, cpf: str) -> str:
    return f"{nome}\nAmapá\n{cpf}"


# ======================================================= §25 — comportamentos
class TestEntradaEIdentidade:
    def test_01_mensagem_recebida_cria_request(self, sistema):
        assert sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0A001").status_code == 200
        assert sistema.esperar_desfecho()
        linhas = sistema.db.fetchall("SELECT * FROM simulations")
        assert len(linhas) == 1
        assert linhas[0]["request_id"].startswith("REQ")
        assert linhas[0]["cpf"] == CPF_A

    def test_02_mesmo_message_id_nao_cria_request_duplicado(self, sistema):
        for _ in range(3):
            assert sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0A002").status_code == 200
        assert sistema.esperar_desfecho()
        time.sleep(0.4)
        assert sistema.db.scalar("SELECT COUNT(*) FROM simulations") == 1
        assert sistema.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE direction='in' AND wa_message_id='3EB0A002'") == 1
        assert len(sistema.servidor.envios()) == 1, "duplicidade virou segunda resposta"

    def test_03_04_05_identidade_da_origem_persiste(self, sistema):
        sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0A003",
                        participante="12345678901234@lid", push="Ryan")
        assert sistema.esperar_desfecho()
        linha = sistema.db.fetchone("SELECT * FROM simulations")
        assert linha["source_message_id"] == "3EB0A003"          # 3
        assert linha["chat_id"] == GRUPO                           # 4
        assert linha["sender_id"] == "12345678901234@lid"          # 5
        assert linha["participant"] == "12345678901234@lid"
        assert linha["chat_name"] == "Consultores"
        assert linha["sender_name"] == "Ryan"
        assert CPF_A in linha["raw_message"], "o texto original precisa ficar para citar"

        entrada = sistema.db.fetchone("SELECT * FROM messages WHERE direction='in'")
        assert entrada["wa_message_id"] == "3EB0A003"
        assert entrada["request_id"] == linha["request_id"]
        assert entrada["status"] == ENTRADA_SOLICITACAO
        assert entrada["participant"] == "12345678901234@lid"

    def test_consultor_vem_do_remetente_e_nao_do_corpo(self, sistema):
        sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0A004",
                        participante=CONSULTOR_B, push="Joana")
        assert sistema.esperar_desfecho()
        linha = sistema.db.fetchone("SELECT * FROM simulations")
        assert linha["consultant_name"] == "Joana"
        assert linha["customer_name"] == "Cliente Teste"
        consultor = sistema.db.fetchone("SELECT * FROM consultants")
        assert consultor["wa_id"] == CONSULTOR_B


class TestRetryPreservaIdentidade:
    def test_06_07_retry_da_entrega_mantem_request_e_message_id(self, tmp_path):
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            s.servidor.roteiro = [recusa(503, "Service Unavailable")]
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0B001")
            assert s.esperar_desfecho()
            antes = s.db.fetchone("SELECT * FROM simulations")
            assert antes["delivery_status"] == Delivery.RETRYING
            assert antes["stage"] == Stage.DELIVERY_RETRY

            s.db.execute("UPDATE simulations SET next_delivery_at=?", (iso_atras(5),))
            s.manager._reenviar_pendentes()

            depois = s.db.fetchone("SELECT * FROM simulations")
            assert s.db.scalar("SELECT COUNT(*) FROM simulations") == 1, "retry criou request"
            assert depois["request_id"] == antes["request_id"]           # 6
            assert depois["delivery_status"] == Delivery.DELIVERED
            assert depois["stage"] == Stage.COMPLETED
            _rota, payload = s.servidor.envios()[-1]
            assert payload["quoted"]["key"]["id"] == "3EB0B001"            # 7
            assert payload["number"] == GRUPO
            assert antes["request_id"] in payload["text"]
            assert s.simuladores[0].vistos == [antes["request_id"]], "retry de entrega re-simulou"
        finally:
            s.desligar()

    def test_retry_da_simulacao_mantem_identidade(self, tmp_path):
        """Falha transitória NO SANTANDER: mesma solicitação, mesma mensagem."""
        s = Sistema(tmp_path, com_imagem=False)

        class Instavel(SimuladorPorCpf):
            falhou = False

            def execute(self, job, on_stage=None, timeout=None):
                if not Instavel.falhou:
                    Instavel.falhou = True
                    self.vistos.append(job.request_id)
                    return SimulationResult(job=job, ok=False, status="Timeout",
                                            error="portal lento", retryable=True)
                return super().execute(job, on_stage, timeout)

        simulador = Instavel()
        s.simuladores[:] = [simulador]
        s.manager.queue = QueueService(db=s.db, hub=s.hub, simulators=[simulador],
                                       max_attempts=2, job_timeout=30.0,
                                       on_result=s.manager._deliver_result,
                                       on_log=s.manager._queue_log)
        import app.jobs as jobs
        original = jobs.RETRY_BACKOFF_SECONDS
        jobs.RETRY_BACKOFF_SECONDS = 0.05
        s.ligar()
        try:
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0B002")
            assert s.esperar_desfecho(timeout=30)
            assert len(set(simulador.vistos)) == 1 and len(simulador.vistos) == 2
            linha = s.db.fetchone("SELECT * FROM simulations")
            assert linha["attempts"] == 2
            _rota, payload = s.servidor.envios()[-1]
            assert payload["quoted"]["key"]["id"] == "3EB0B002"
        finally:
            jobs.RETRY_BACKOFF_SECONDS = original
            s.desligar()


class TestCitacao:
    def test_08_quote_recebe_o_message_id_texto_e_autor_corretos(self, sistema):
        sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0C001", participante=CONSULTOR_A)
        assert sistema.esperar_desfecho()
        _rota, payload = sistema.servidor.envios()[0]
        citada = payload["quoted"]
        assert citada["key"]["id"] == "3EB0C001"
        assert citada["key"]["remoteJid"] == GRUPO
        assert citada["key"]["participant"] == CONSULTOR_A
        assert CPF_A in citada["message"]["conversation"]
        linha = sistema.db.fetchone("SELECT * FROM simulations")
        assert linha["quote_status"] == QuoteStatus.OK, "a citação foi conferida na resposta"

    def test_09_quote_falhando_gera_fallback_sem_perder_a_resposta(self, tmp_path):
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            s.servidor.roteiro = [recusa_se_citar(400)]
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0C002", push="Ryan")
            assert s.esperar_desfecho()
            envios = s.servidor.envios()
            assert len(envios) == 2
            assert "quoted" in envios[0][1] and "quoted" not in envios[1][1]
            assert "↩ Ryan" in envios[1][1]["text"], "fallback sem identificar o consultor"
            linha = s.db.fetchone("SELECT * FROM simulations")
            assert linha["delivery_status"] == Delivery.DELIVERED
            assert linha["quote_status"] == QuoteStatus.FALLBACK
            assert linha["replied_at"]
        finally:
            s.desligar()

    def test_2xx_sem_stanza_fica_unverified_e_nao_duplica(self, tmp_path):
        """A Evolution manda solta quando não acha o original -- e não avisa.

        Sem `stanzaId` na resposta não dá para provar nada sobre a citação:
        fica `unverified`. O que NÃO muda é o principal -- a mensagem chegou,
        então não sai uma segunda.
        """
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            def solta(rota, payload):
                sem = {k: v for k, v in payload.items() if k != "quoted"}
                return httpx.Response(201, json=EvolutionFalsa.resposta_real(rota, sem, "BAE5SOLTA"))
            s.servidor.roteiro = [solta]
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0C003")
            assert s.esperar_desfecho()
            linha = s.db.fetchone("SELECT * FROM simulations")
            assert linha["quote_status"] == QuoteStatus.UNVERIFIED
            assert linha["delivery_status"] == Delivery.DELIVERED
            assert len(s.servidor.envios()) == 1, "não pode reenviar o que já chegou"
        finally:
            s.desligar()


class TestImagem:
    def test_10_png_falhando_nao_elimina_resposta_em_texto(self, tmp_path):
        s = Sistema(tmp_path, renderizador=RenderizadorFalso(falha=True)).ligar()
        try:
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0D001")
            assert s.esperar_desfecho()
            envios = s.servidor.envios()
            assert [r for r, _ in envios] == ["/message/sendText/allana"]
            linha = s.db.fetchone("SELECT * FROM simulations")
            assert linha["delivery_status"] == Delivery.DELIVERED
            assert linha["media_status"] == "render_failed"
            assert linha["stage"] == Stage.COMPLETED
        finally:
            s.desligar()

    def test_14_png_vazio_e_rejeitado(self, tmp_path):
        vazio = tmp_path / "REQ000001.png"
        vazio.write_bytes(b"")
        with pytest.raises(PngInvalido):
            validar_png(vazio)
        truncado = tmp_path / "REQ000002.png"
        truncado.write_bytes(png_valido()[:-10])
        with pytest.raises(PngInvalido):
            validar_png(truncado)
        corrompido = tmp_path / "REQ000003.png"
        dados = bytearray(png_valido())
        dados[40] ^= 0xFF
        corrompido.write_bytes(bytes(dados))
        with pytest.raises(PngInvalido):
            validar_png(corrompido)

    def test_14_png_vazio_no_fluxo_vira_texto(self, tmp_path):
        s = Sistema(tmp_path, renderizador=RenderizadorFalso(vazio=True)).ligar()
        try:
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0D002")
            assert s.esperar_desfecho()
            assert [r for r, _ in s.servidor.envios()] == ["/message/sendText/allana"]
            linha = s.db.fetchone("SELECT * FROM simulations")
            assert linha["media_status"] == "invalid"
            assert not (tmp_path / "comprovantes" / f"{linha['request_id']}.png").exists()
        finally:
            s.desligar()

    def test_15_arquivo_pertence_ao_request_correto(self, sistema, tmp_path):
        pasta = tmp_path / "comprovantes"
        pasta.mkdir(exist_ok=True)
        arquivo = pasta / "REQ000122.png"
        arquivo.write_bytes(png_valido())
        with pytest.raises(PngInvalido, match="não pertence"):
            validar_png(arquivo, request_id="REQ000123", pasta=pasta)
        with pytest.raises(PngInvalido, match="fora da pasta"):
            validar_png(arquivo, request_id="REQ000122", pasta=tmp_path)
        assert validar_png(arquivo, request_id="REQ000122", pasta=pasta) == (4, 3)

        sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0D003")
        assert sistema.esperar_desfecho()
        linha = sistema.db.fetchone("SELECT * FROM simulations")
        rota, payload = sistema.servidor.envios()[0]
        assert rota == "/message/sendMedia/allana"
        assert payload["fileName"] == f"{linha['request_id']}.png"
        saida = sistema.db.fetchone("SELECT * FROM messages WHERE direction='out'")
        assert Path(saida["media_path"]).name == f"{linha['request_id']}.png"
        assert saida["media_id"] == "q1w2e3r4t5y6"
        assert "SEGREDO" not in json.dumps(dict(saida)), "mediaKey não pode ir para o banco"

    def test_png_velho_de_outra_tentativa_nunca_e_reaproveitado(self, tmp_path):
        """Render que falha não pode deixar o PNG anterior passar por novo."""
        s = Sistema(tmp_path, renderizador=RenderizadorFalso(falha=True)).ligar()
        try:
            pasta = tmp_path / "comprovantes"
            pasta.mkdir(exist_ok=True)
            (pasta / "REQ000001.png").write_bytes(png_valido(semente=b"OLD"))
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0D004")
            assert s.esperar_desfecho()
            assert [r for r, _ in s.servidor.envios()] == ["/message/sendText/allana"], (
                "mandou uma imagem que não foi gerada para esta solicitação")
        finally:
            s.desligar()


class TestClassificacaoDeFalhas:
    @pytest.mark.parametrize("status", [429, 503])
    def test_11_12_transitorio_gera_retry(self, tmp_path, status):
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            s.servidor.roteiro = [recusa(status)]
            s.webhook(_pedido("Cliente Teste", CPF_A), f"3EB0E{status}")
            assert s.esperar_desfecho()
            linha = s.db.fetchone("SELECT * FROM simulations")
            assert linha["delivery_status"] == Delivery.RETRYING
            assert linha["status"] == Status.COMPLETED
            assert linha["stage"] == Stage.DELIVERY_RETRY, "marcou concluída sem entregar"
            assert linha["replied_at"] is None
            falha = s.db.fetchone("SELECT * FROM messages WHERE direction='out'")
            assert falha["http_status"] == status and falha["status"] == "failed"
        finally:
            s.desligar()

    def test_13_erro_permanente_nao_gera_retry(self, tmp_path):
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            s.servidor.roteiro = [recusa(401, "unauthorized")]
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0E401")
            assert s.esperar_desfecho()
            linha = s.db.fetchone("SELECT * FROM simulations")
            assert linha["delivery_status"] == Delivery.FAILED
            assert linha["stage"] == Stage.DELIVERY_FAILED
            s.db.execute("UPDATE simulations SET next_delivery_at=?, updated_at=?",
                         (iso_atras(9999), iso_atras(9999)))
            s.manager._reenviar_pendentes()
            assert len(s.servidor.envios()) == 1, "repetiu um erro permanente"
        finally:
            s.desligar()

    def test_13_cpf_invalido_nao_vira_solicitacao(self, sistema):
        sistema.webhook("Cliente Teste\n529.982.247-26", "3EB0E999")
        assert _aguardar(lambda: sistema.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE direction='in' AND status='rejected'") == 1)
        assert sistema.db.scalar("SELECT COUNT(*) FROM simulations") == 0

    def test_20_2xx_sem_id_nao_e_entrega_confirmada(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        try:
            s.servidor.roteiro = [lambda rota, payload: httpx.Response(200, json={"status": "PENDING"})]
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0E200")
            assert s.esperar_desfecho()
            linha = s.db.fetchone("SELECT * FROM simulations")
            assert linha["delivery_status"] == Delivery.UNCONFIRMED
            assert linha["stage"] == Stage.DELIVERY_UNCONFIRMED
            assert linha["replied_at"] is None, "inventou confirmação"
            assert len(s.servidor.envios()) == 1, "mandou texto por cima de uma imagem que pode ter chegado"
            s.db.execute("UPDATE simulations SET updated_at=?, finished_at=?",
                         (iso_atras(9999), iso_atras(9999)))
            s.manager._reenviar_pendentes()
            assert len(s.servidor.envios()) == 1, "reenviou sozinho uma entrega sem confirmação"
        finally:
            s.desligar()

    def test_19_sent_message_id_e_registrado(self, sistema):
        sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0E019")
        assert sistema.esperar_desfecho()
        linha = sistema.db.fetchone("SELECT * FROM simulations")
        assert linha["sent_message_id"].startswith("BAE5")
        saida = sistema.db.fetchone("SELECT * FROM messages WHERE direction='out'")
        assert saida["wa_message_id"] == linha["sent_message_id"]
        assert saida["origin_message_id"] == "3EB0E019"
        assert saida["quoted_message_id"] == "3EB0E019"
        assert saida["quote_status"] == QuoteStatus.OK
        assert saida["provider"] == "evolution"
        assert saida["http_status"] == 201
        assert saida["attempt"] == 1


class TestConcorrenciaReinicioEIdempotencia:
    def test_16_jobs_simultaneos_nao_cruzam_estado(self, tmp_path):
        # A chega primeiro e demora; B chega depois e termina ANTES.
        s = Sistema(tmp_path, workers=2, atrasos={CPF_A: 0.8, CPF_B: 0.05}).ligar()
        try:
            s.webhook(_pedido("Cliente A", CPF_A), "3EB0MSG_A", participante=CONSULTOR_A, push="Ana")
            time.sleep(0.05)
            s.webhook(_pedido("Cliente B", CPF_B), "3EB0MSG_B", participante=CONSULTOR_B, push="Bruno")
            assert s.esperar_desfecho(2)
            concluidos = [rid for sim in s.simuladores for rid in sim.concluidos]
            assert len(concluidos) == 2

            a = s.linha(source_message_id="3EB0MSG_A")
            b = s.linha(source_message_id="3EB0MSG_B")
            assert a["request_id"] != b["request_id"]
            # A ordem em que as respostas CHEGARAM à Evolution (replied_at tem
            # resolução de segundos e empataria).
            ordem = [p["fileName"].removesuffix(".png") for _r, p in s.servidor.envios()]
            assert ordem == [b["request_id"], a["request_id"]], (
                f"o teste precisa de B terminando antes: {ordem}")
            por_request = {}
            for rota, payload in s.servidor.envios():
                por_request[payload["fileName"].removesuffix(".png")] = payload
            for linha, msg, cpf, cliente in ((a, "3EB0MSG_A", CPF_A, "Cliente A"),
                                             (b, "3EB0MSG_B", CPF_B, "Cliente B")):
                payload = por_request[linha["request_id"]]
                assert payload["quoted"]["key"]["id"] == msg
                assert payload["number"] == GRUPO
                assert cpf in payload["quoted"]["message"]["conversation"]
                assert linha["request_id"] in payload["caption"]
                assert cliente in payload["caption"]
                assert linha["reduction_value"] == float(int(cpf[-4:]))
        finally:
            s.desligar()

    def test_17_restart_no_meio_da_entrega_nao_resimula(self, tmp_path):
        """Resultado pronto, processo caiu antes de responder."""
        primeiro = Sistema(tmp_path, com_imagem=False)
        primeiro.db.insert("simulations", {
            "request_id": "REQ000050", "consultant_name": "Ryan", "chat_id": GRUPO,
            "chat_name": "Consultores", "sender_id": CONSULTOR_A, "sender_name": "Ryan",
            "participant": CONSULTOR_A, "source_message_id": "3EB0ANTIGA",
            "cpf": CPF_A, "bank": "Santander", "contract": "", "customer_name": "Cliente Teste",
            "raw_message": f"Ivone\n{CPF_A}", "status": Status.PROCESSING,
            "stage": Stage.REPLYING, "result_ok": 1, "refin": "Sim",
            "reduction_value": 4725.0, "delivery_status": Delivery.PENDING,
            "attempts": 1, "max_attempts": 2,
            "created_at": iso_atras(60), "updated_at": iso_atras(30),
            "finished_at": iso_atras(30)})

        # "reinicio": manager novo sobre o mesmo banco
        s = Sistema(tmp_path, com_imagem=False, db_path=primeiro.config.db_path).ligar()
        try:
            assert s.manager.queue.recover() == 0, "voltou para a fila de SIMULAÇÃO"
            linha = s.linha(request_id="REQ000050")
            assert linha["delivery_status"] == Delivery.RETRYING
            s.manager._reenviar_pendentes()
            linha = s.linha(request_id="REQ000050")
            assert linha["delivery_status"] == Delivery.DELIVERED
            assert s.simuladores[0].vistos == [], "re-simulou no Santander"
            _rota, payload = s.servidor.envios()[0]
            assert payload["quoted"]["key"]["id"] == "3EB0ANTIGA"
            assert payload["quoted"]["key"]["participant"] == CONSULTOR_A
            assert CPF_A in payload["quoted"]["message"]["conversation"]
        finally:
            s.desligar()

    def test_17_queda_no_meio_de_um_reenvio_volta_a_ser_reenviavel(self, tmp_path):
        """O reenvio marca `pending` antes de enviar; cair ali não pode travar a linha."""
        s = Sistema(tmp_path, com_imagem=False)
        s.db.insert("simulations", {
            "request_id": "REQ000060", "consultant_name": "Ryan", "chat_id": GRUPO,
            "sender_id": CONSULTOR_A, "participant": CONSULTOR_A,
            "source_message_id": "3EB0REENVIO", "raw_message": _pedido("Cliente Teste", CPF_A),
            "cpf": CPF_A, "bank": "Santander", "status": Status.COMPLETED,
            "stage": Stage.DELIVERY_RETRY, "result_ok": 1, "reduction_value": 10.0,
            "delivery_status": Delivery.PENDING, "reply_attempts": 1,
            "created_at": iso_atras(600), "updated_at": iso_atras(60),
            "finished_at": iso_atras(600)})
        s.manager.queue.recover()
        linha = s.linha(request_id="REQ000060")
        assert linha["delivery_status"] == Delivery.RETRYING
        s.manager._reenviar_pendentes()
        linha = s.linha(request_id="REQ000060")
        assert linha["delivery_status"] == Delivery.DELIVERED
        assert linha["stage"] == Stage.COMPLETED
        assert s.servidor.envios()[0][1]["quoted"]["key"]["id"] == "3EB0REENVIO"

    def test_17_mensagem_aceita_e_nao_tratada_e_retomada(self, tmp_path):
        """O webhook respondeu 200 e o processo caiu antes de criar o REQ."""
        s = Sistema(tmp_path)
        s.manager._registrar_entrada(IncomingMessage(
            message_id="3EB0PERDIDA", chat_id=GRUPO, chat_name="Consultores",
            sender_id=CONSULTOR_A, sender_name="Ryan", text=_pedido("Cliente Teste", CPF_A),
            participant=CONSULTOR_A))
        s.ligar()
        try:
            assert s.manager._retomar_entradas() == 1
            assert s.esperar_desfecho()
            linha = s.linha(source_message_id="3EB0PERDIDA")
            assert linha is not None and linha["delivery_status"] == Delivery.DELIVERED
            assert s.manager._retomar_entradas() == 0, "retomou de novo o que já tratou"
        finally:
            s.desligar()

    def test_queda_entre_criar_o_req_e_ligar_a_mensagem_nao_duplica(self, tmp_path):
        s = Sistema(tmp_path)
        s.db.insert("simulations", {
            "request_id": "REQ000077", "consultant_name": "Ryan", "chat_id": GRUPO,
            "source_message_id": "3EB0MEIO", "cpf": CPF_A, "bank": "Santander",
            "status": Status.QUEUED, "stage": Stage.QUEUED,
            "created_at": iso_atras(10), "updated_at": iso_atras(10)})
        mensagem = IncomingMessage(message_id="3EB0MEIO", chat_id=GRUPO, chat_name="C",
                                   sender_id=CONSULTOR_A, sender_name="Ryan",
                                   text=_pedido("Cliente Teste", CPF_A))
        s.manager._registrar_entrada(mensagem)
        s.manager._handle_message(mensagem)
        assert s.db.scalar("SELECT COUNT(*) FROM simulations") == 1
        entrada = s.db.fetchone("SELECT * FROM messages WHERE wa_message_id='3EB0MEIO'")
        assert entrada["request_id"] == "REQ000077"
        assert entrada["status"] == ENTRADA_SOLICITACAO

    def test_18_request_concluido_nao_e_criado_novamente(self, sistema):
        sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0F018")
        assert sistema.esperar_desfecho()
        r = sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0F018")
        assert r.json().get("ignorado") == "já processado"
        assert r.json().get("request_id", "").startswith("REQ")
        sistema.manager._handle_message(IncomingMessage(
            message_id="3EB0F018", chat_id=GRUPO, chat_name="Consultores",
            sender_id=CONSULTOR_A, sender_name="Ryan", text=_pedido("Cliente Teste", CPF_A)))
        time.sleep(0.3)
        assert sistema.db.scalar("SELECT COUNT(*) FROM simulations") == 1
        assert len(sistema.servidor.envios()) == 1


# ====================================== a política de HTTP, resposta por resposta
def _corpo_de_erro(mensagem: str, status: int = 400) -> dict:
    """Formato de erro da Evolution v2: {status, error, response.message[]}."""
    return {"status": status, "error": "Bad Request", "response": {"message": [mensagem]}}


class TestPoliticaHttpDaEvolution:
    """A pergunta de cada caso: **a mensagem pode ter saído?**

    Se puder, não sai uma segunda por nada -- nem sem citação, nem em texto,
    nem pelo laço de reenvio. Se provadamente não saiu, o consultor é atendido.
    """

    def _pedido(self, s: Sistema, mid: str, push: str = "Ryan"):
        s.webhook(_pedido("Cliente Teste", CPF_A), mid, push=push)
        assert s.esperar_desfecho(), "a entrega não se resolveu"
        return s.linha(source_message_id=mid) or {}

    # ---------------------------------------------------------------- TESTE A
    def test_a_500_com_citacao_nao_manda_segunda_mensagem(self, tmp_path):
        """500 não prova que nada saiu: pode ser erro DEPOIS de enviar."""
        s = Sistema(tmp_path).ligar()
        try:
            s.servidor.roteiro = [recusa(500, "Internal server error")]
            linha = self._pedido(s, "3EB0H500")
            assert len(s.servidor.envios()) == 1, (
                "mandou uma segunda mensagem depois de um 500: "
                f"{[r for r, _ in s.servidor.envios()]}")
            assert linha["delivery_status"] == Delivery.UNCONFIRMED
            assert linha["stage"] == Stage.DELIVERY_UNCONFIRMED
            assert linha["sent_message_id"] == "", "inventou prova de entrega"
            assert linha["replied_at"] is None
            assert linha["quote_status"] == QuoteStatus.UNVERIFIED
            # E o laço de reenvio também não manda.
            s.db.execute("UPDATE simulations SET updated_at=?, finished_at=?, next_delivery_at=?",
                         (iso_atras(9999), iso_atras(9999), iso_atras(9999)))
            s.manager._reenviar_pendentes()
            assert len(s.servidor.envios()) == 1, "o reenvio duplicou uma entrega incerta"
        finally:
            s.desligar()

    # ---------------------------------------------------------------- TESTE B
    def test_b_timeout_de_leitura_fica_incerto_e_nao_cai_para_texto(self, tmp_path):
        """A requisição subiu e a resposta não veio: pode ter sido enviada."""
        s = Sistema(tmp_path).ligar()
        try:
            def estoura(rota, payload):
                raise httpx.ReadTimeout("a Evolution não respondeu a tempo")
            s.servidor.roteiro = [estoura]
            linha = self._pedido(s, "3EB0TIMEOUT")
            assert len(s.servidor.envios()) == 1, "mandou texto por cima de um timeout"
            assert linha["delivery_status"] == Delivery.UNCONFIRMED
            assert linha["replied_at"] is None
        finally:
            s.desligar()

    def test_b_conexao_recusada_nao_saiu_nada_entao_repete_com_citacao(self, tmp_path):
        """Não conectar é diferente de não receber resposta: aqui nada saiu."""
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            def recusa_conexao(rota, payload):
                raise httpx.ConnectError("conexão recusada")
            s.servidor.roteiro = [recusa_conexao]
            linha = self._pedido(s, "3EB0CONEXAO")
            assert linha["delivery_status"] == Delivery.RETRYING
            s.db.execute("UPDATE simulations SET next_delivery_at=?", (iso_atras(5),))
            s.manager._reenviar_pendentes()
            _rota, payload = s.servidor.envios()[-1]
            assert payload["quoted"]["key"]["id"] == "3EB0CONEXAO", "o retry perdeu a citação"
            assert (s.linha(source_message_id="3EB0CONEXAO") or {})[
                "delivery_status"] == Delivery.DELIVERED
        finally:
            s.desligar()

    # ------------------------------------------------------------- TESTES C, D
    @pytest.mark.parametrize("status", [400, 422])
    def test_c_d_citacao_invalida_vira_um_unico_envio_sem_citacao(self, tmp_path, status):
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            s.servidor.roteiro = [lambda rota, p: (
                httpx.Response(status, json=_corpo_de_erro(
                    "quoted message not found in the instance", status))
                if "quoted" in p else None)]
            linha = self._pedido(s, f"3EB0Q{status}", push="Ryan")
            envios = s.servidor.envios()
            assert len(envios) == 2, "esperava a recusa e o reenvio sem citação"
            assert "quoted" in envios[0][1] and "quoted" not in envios[1][1]
            entregues = [p for _r, p in envios if _r]  # todas as tentativas
            assert len(entregues) == 2
            assert "↩ Ryan" in envios[1][1]["text"]
            assert linha["quote_status"] == QuoteStatus.FALLBACK
            assert linha["delivery_status"] == Delivery.DELIVERED
            assert linha["quote_error"], "o motivo da recusa da citação não foi gravado"
            # UMA resposta final: a recusa não conta como mensagem entregue.
            saidas = s.db.fetchall(
                "SELECT status FROM messages WHERE direction='out' AND request_id=?",
                (linha["request_id"],))
            assert sum(1 for x in saidas if x["status"] not in ("failed", "unconfirmed")) == 1
        finally:
            s.desligar()

    def test_400_sem_explicacao_nao_vira_fallback_nem_texto(self, tmp_path):
        """Sem evidência do que foi recusado, a mensagem pode ter saído."""
        s = Sistema(tmp_path).ligar()
        try:
            s.servidor.roteiro = [recusa(400, "Bad Request")]
            linha = self._pedido(s, "3EB0OPACO")
            assert len(s.servidor.envios()) == 1
            assert linha["delivery_status"] == Delivery.UNCONFIRMED
        finally:
            s.desligar()

    def test_400_de_validacao_na_imagem_cai_para_texto(self, tmp_path):
        """Aqui a Evolution DIZ o que recusou: nada saiu, então o texto vai."""
        s = Sistema(tmp_path).ligar()
        try:
            s.servidor.roteiro = [lambda rota, p: httpx.Response(
                400, json=_corpo_de_erro("Owned media must be a url or base64"))]
            linha = self._pedido(s, "3EB0MIDIA")
            rotas = [r for r, _ in s.servidor.envios()]
            assert rotas == ["/message/sendMedia/allana", "/message/sendText/allana"]
            assert linha["delivery_status"] == Delivery.DELIVERED
            assert linha["media_status"] == "send_failed"
        finally:
            s.desligar()

    # ------------------------------------------------------------- TESTES E, F
    @pytest.mark.parametrize("status", [429, 503])
    def test_e_f_transitorio_repete_com_a_mesma_citacao(self, tmp_path, status):
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            s.servidor.roteiro = [recusa(status)]
            linha = self._pedido(s, f"3EB0T{status}")
            assert linha["delivery_status"] == Delivery.RETRYING
            assert linha["quote_status"] == "", "citação de requisição não processada"
            s.db.execute("UPDATE simulations SET next_delivery_at=?", (iso_atras(5),))
            s.manager._reenviar_pendentes()
            _rota, payload = s.servidor.envios()[-1]
            assert payload["quoted"]["key"]["id"] == f"3EB0T{status}", "o retry tirou a citação"
            assert (s.linha(source_message_id=f"3EB0T{status}") or {})[
                "delivery_status"] == Delivery.DELIVERED
        finally:
            s.desligar()

    # ---------------------------------------------------------------- TESTE G
    def test_g_2xx_sem_key_id_fica_incerto(self, tmp_path):
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            s.servidor.roteiro = [lambda rota, p: httpx.Response(200, json={"status": "PENDING"})]
            linha = self._pedido(s, "3EB0SEMID")
            assert linha["delivery_status"] == Delivery.UNCONFIRMED
            assert linha["sent_message_id"] == ""
            s.db.execute("UPDATE simulations SET updated_at=?, finished_at=?, next_delivery_at=?",
                         (iso_atras(9999), iso_atras(9999), iso_atras(9999)))
            s.manager._reenviar_pendentes()
            assert len(s.servidor.envios()) == 1, "reenviou sozinho uma entrega sem prova"
        finally:
            s.desligar()

    # ---------------------------------------------------------------- TESTE H
    def test_h_2xx_com_key_id_e_entrega_confirmada(self, tmp_path):
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            linha = self._pedido(s, "3EB0COMID")
            assert linha["delivery_status"] == Delivery.DELIVERED
            assert linha["sent_message_id"].startswith("BAE5")
            assert linha["quote_status"] == QuoteStatus.OK
            assert linha["replied_at"]
        finally:
            s.desligar()

    def test_a_classificacao_e_uma_tabela(self):
        """A política, sem servidor nenhum."""
        from app.evolution import classificar_resposta
        from app.models import Desfecho

        assert classificar_resposta(429, "").desfecho == Desfecho.TRANSITORIA
        assert classificar_resposta(502, "").desfecho == Desfecho.INCERTA
        assert classificar_resposta(503, "").desfecho == Desfecho.TRANSITORIA
        assert classificar_resposta(504, "").desfecho == Desfecho.INCERTA
        assert classificar_resposta(500, "boom").desfecho == Desfecho.INCERTA
        assert classificar_resposta(401, "").desfecho == Desfecho.PERMANENTE
        assert classificar_resposta(403, "").desfecho == Desfecho.PERMANENTE
        assert classificar_resposta(404, "").desfecho == Desfecho.PERMANENTE
        assert classificar_resposta(503, '{"error":"LICENSE_REQUIRED"}').desfecho == Desfecho.PERMANENTE
        assert classificar_resposta(400, "quoted message not found",
                                    com_citacao=True).desfecho == Desfecho.CITACAO_RECUSADA
        assert classificar_resposta(422, "instance.quoted.key is not allowed",
                                    com_citacao=True).desfecho == Desfecho.CITACAO_RECUSADA
        # sem citação na requisição, um 400 que fala de quoted não é sobre a nossa
        assert classificar_resposta(400, "quoted message not found").desfecho != Desfecho.CITACAO_RECUSADA
        assert classificar_resposta(400, 'requires property "number"').desfecho == Desfecho.RECUSADA
        assert classificar_resposta(400, "Bad Request").desfecho == Desfecho.INCERTA
        # erro depois do envio vence a marca de citação: pode ter saído
        assert classificar_resposta(400, "Invalid prisma.message.create() with quotedMessage",
                                    com_citacao=True).desfecho == Desfecho.INCERTA

    def test_transporte_separa_nao_conectou_de_nao_respondeu(self):
        from app.evolution import classificar_falha_de_transporte
        from app.models import Desfecho

        for exc in (httpx.ConnectError("x"), httpx.ConnectTimeout("x"),
                    httpx.WriteTimeout("x"), httpx.PoolTimeout("x")):
            assert classificar_falha_de_transporte(exc).desfecho == Desfecho.TRANSITORIA
        for exc in (httpx.ReadTimeout("x"), httpx.ReadError("x"),
                    httpx.RemoteProtocolError("x")):
            assert classificar_falha_de_transporte(exc).desfecho == Desfecho.INCERTA


class TestNuncaDuasRespostas:
    """Cenários em que a PRIMEIRA pode ter sido aceita pelo WhatsApp.

    Todos têm a mesma forma: o bot não sabe se saiu. A resposta certa é
    sempre a mesma -- registrar como incerta e não mandar nada por cima.
    """

    def _linha_em_entrega(self, s: Sistema, mid="3EB0VOO", **extra) -> int:
        """Uma solicitação com resultado pronto e entrega em curso (`pending`)."""
        return s.db.insert("simulations", {
            "request_id": extra.pop("request_id", "REQ000700"),
            "consultant_name": "Ryan", "chat_id": GRUPO, "chat_name": "Consultores",
            "sender_id": CONSULTOR_A, "participant": CONSULTOR_A,
            "source_message_id": mid, "raw_message": _pedido("Cliente Teste", CPF_A),
            "cpf": CPF_A, "bank": "Santander", "customer_name": "Cliente Teste",
            "status": Status.PROCESSING, "stage": Stage.REPLYING, "result_ok": 1,
            "reduction_value": 4725.0, "delivery_status": Delivery.PENDING,
            "attempts": 1, "max_attempts": 2, "created_at": iso_atras(90),
            "updated_at": iso_atras(30), "finished_at": iso_atras(30), **extra})

    def _saida(self, s: Sistema, simulation_id: int, status: str, **extra):
        return s.db.insert("messages", {
            "simulation_id": simulation_id, "request_id": "REQ000700", "direction": "out",
            "kind": "image", "chat_id": GRUPO, "status": status, "provider": "evolution",
            "attempt": 1, "origin_message_id": "3EB0VOO", "created_at": iso_atras(30),
            **extra})

    def test_queda_entre_o_post_e_a_gravacao_vira_incerta(self, tmp_path):
        """O caso mais perigoso: a Evolution aceitou e o processo morreu antes de gravar."""
        s = Sistema(tmp_path, com_imagem=False)
        sid = self._linha_em_entrega(s)
        self._saida(s, sid, "sending")          # a chamada tinha começado
        s.ligar()
        try:
            assert s.manager.queue.recover() == 0
            linha = s.linha(request_id="REQ000700")
            assert linha["delivery_status"] == Delivery.UNCONFIRMED, (
                "reenviaria uma resposta que pode ter chegado")
            assert linha["stage"] == Stage.DELIVERY_UNCONFIRMED
            s.manager._reenviar_pendentes()
            assert s.servidor.envios() == [], "o laço de reenvio duplicou"
            saida = s.db.fetchone("SELECT status FROM messages WHERE direction='out'")
            assert saida["status"] == Delivery.UNCONFIRMED
        finally:
            s.desligar()

    def test_resposta_ja_entregue_sem_gravar_o_desfecho_nao_reenvia(self, tmp_path):
        """A mensagem saiu COM id e a atualização da solicitação falhou."""
        s = Sistema(tmp_path, com_imagem=False)
        sid = self._linha_em_entrega(s)
        self._saida(s, sid, Status.COMPLETED, wa_message_id="BAE5JASAIU")
        s.ligar()
        try:
            s.manager.queue.recover()
            linha = s.linha(request_id="REQ000700")
            assert linha["delivery_status"] == Delivery.DELIVERED
            assert linha["sent_message_id"] == "BAE5JASAIU"
            assert linha["replied_at"]
            s.manager._reenviar_pendentes()
            assert s.servidor.envios() == [], "reenviou algo que já tinha saído"
        finally:
            s.desligar()

    def test_queda_no_meio_de_um_reenvio_vira_incerta(self, tmp_path):
        """Mesma regra no laço de reenvio: `sending` gravado = pode ter saído."""
        s = Sistema(tmp_path, com_imagem=False)
        sid = self._linha_em_entrega(s, status=Status.COMPLETED, stage=Stage.DELIVERY_RETRY,
                                     reply_attempts=1)
        self._saida(s, sid, "sending", kind="text")
        s.ligar()
        try:
            s.manager.queue.recover()
            linha = s.linha(request_id="REQ000700")
            assert linha["delivery_status"] == Delivery.UNCONFIRMED
            s.manager._reenviar_pendentes()
            assert s.servidor.envios() == []
        finally:
            s.desligar()

    def test_queda_depois_de_gravar_a_saida_unconfirmed(self, tmp_path):
        """500 gravado na SAÍDA, processo morre antes de gravar a SIMULAÇÃO.

        A saída já diz "pode ter saído"; a simulação ainda está `pending`. A
        recuperação lia só `sending` como incerta, via "nada saiu" aqui e
        reenviava -- segunda mensagem no grupo.
        """
        s = Sistema(tmp_path, com_imagem=True)
        sid = self._linha_em_entrega(s)
        self._saida(s, sid, Delivery.UNCONFIRMED, desfecho="incerta", http_status=500)
        s.ligar()
        try:
            s.manager.queue.recover()
            linha = s.linha(request_id="REQ000700")
            assert linha["delivery_status"] == Delivery.UNCONFIRMED
            s.db.execute("UPDATE simulations SET updated_at=?, finished_at=?, next_delivery_at=?",
                         (iso_atras(9999), iso_atras(9999), iso_atras(9999)))
            s.manager._reenviar_pendentes()
            assert s.servidor.envios() == [], "reenviou algo que pode ter saído (500)"
        finally:
            s.desligar()

    def test_falha_ao_gravar_o_desfecho_depois_de_um_500(self, tmp_path):
        """A imagem recebe 500 e gravar o desfecho da simulação quebra uma vez."""
        s = Sistema(tmp_path, com_imagem=True)
        s.servidor.roteiro = [recusa(500, "Internal server error")]
        original = s.manager._registrar_entrega
        chamadas = {"n": 0}

        def quebra_uma_vez(*a, **k):
            chamadas["n"] += 1
            if chamadas["n"] == 1:
                raise RuntimeError("database is locked")
            return original(*a, **k)

        s.manager._registrar_entrega = quebra_uma_vez
        s.ligar()
        try:
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0TRAVOU")
            assert _aguardar(lambda: (s.linha(source_message_id="3EB0TRAVOU") or {}).get(
                "delivery_status") not in (None, "", Delivery.PENDING), timeout=20)
            linha = s.linha(source_message_id="3EB0TRAVOU")
            assert linha["delivery_status"] == Delivery.UNCONFIRMED
            s.db.execute("UPDATE simulations SET next_delivery_at=?, updated_at=?, finished_at=?",
                         (iso_atras(9999), iso_atras(9999), iso_atras(9999)))
            s.manager._reenviar_pendentes()
            assert len(s.servidor.envios()) == 1, "segunda mensagem depois de um 500"
        finally:
            s.desligar()

    def test_saida_unconfirmed_bloqueia_ate_o_reenvio_legado(self, tmp_path):
        """Linha sem delivery_status (legado) com saída incerta: o laço não manda."""
        s = Sistema(tmp_path, com_imagem=False)
        sid = self._linha_em_entrega(s, delivery_status="", status=Status.COMPLETED,
                                     stage=Stage.COMPLETED)
        self._saida(s, sid, Delivery.UNCONFIRMED, desfecho="incerta", http_status=504)
        s.ligar()
        try:
            s.db.execute("UPDATE simulations SET updated_at=?, finished_at=?",
                         (iso_atras(9999), iso_atras(9999)))
            s.manager._reenviar_pendentes()
            assert s.servidor.envios() == []
            assert s.linha(request_id="REQ000700")["delivery_status"] == Delivery.UNCONFIRMED
        finally:
            s.desligar()

    def test_excecao_depois_do_envio_nao_vira_reenvio(self, tmp_path):
        """A API aceitou, o código quebrou depois. Reenviar duplicaria.

        Como a saída ficou gravada COM id, o desfecho é melhor que "incerta":
        dá para afirmar que foi entregue.
        """
        s = Sistema(tmp_path, com_imagem=False)
        original = s.manager._send_reply

        def envia_e_quebra(*a, **k):
            original(*a, **k)
            raise RuntimeError("o banco caiu depois do envio")

        s.manager._send_reply = envia_e_quebra
        s.ligar()
        try:
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0QUEBRA")
            assert _aguardar(lambda: (s.linha(source_message_id="3EB0QUEBRA") or {}).get(
                "delivery_status") in (Delivery.UNCONFIRMED, Delivery.DELIVERED), timeout=20)
            linha = s.linha(source_message_id="3EB0QUEBRA")
            assert linha["delivery_status"] == Delivery.DELIVERED, (
                "a saída gravada com id prova a entrega")
            assert linha["sent_message_id"].startswith("BAE5")
            assert len(s.servidor.envios()) == 1, "mandou duas respostas"
            s.db.execute("UPDATE simulations SET updated_at=?, finished_at=?",
                         (iso_atras(9999), iso_atras(9999)))
            s.manager._reenviar_pendentes()
            assert len(s.servidor.envios()) == 1
        finally:
            s.desligar()

    def test_o_vigia_nao_reenvia_uma_entrega_que_comecou(self, tmp_path):
        """A solicitação travou em `processing` com o envio já em curso."""
        s = Sistema(tmp_path, com_imagem=False)
        sid = self._linha_em_entrega(s, created_at=iso_atras(9000),
                                     started_at=iso_atras(9000))
        self._saida(s, sid, "sending")
        s.ligar()
        try:
            assert s.manager.queue._resolver_presas() == 1
            linha = s.linha(request_id="REQ000700")
            assert linha["delivery_status"] == Delivery.UNCONFIRMED
            assert "pode ter saído" in (linha["delivery_error"] or "")
        finally:
            s.desligar()


class TestParticipantEIdentidade:
    """`sender_id` identifica o CONSULTOR; `participant` cita o AUTOR.

    São coisas diferentes e não podem ser trocadas: um participante pode
    chegar como `@c.us`, `@s.whatsapp.net` ou `@lid`, e o `@lid` não é
    telefone nenhum.
    """

    def test_participante_c_us_vira_identidade_e_citacao(self, sistema):
        antigo = "5588999990000@c.us"
        sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0CUS", participante=antigo)
        assert sistema.esperar_desfecho()
        linha = sistema.linha(source_message_id="3EB0CUS")
        assert linha["sender_id"] == antigo and linha["participant"] == antigo
        _rota, payload = sistema.servidor.envios()[0]
        assert payload["quoted"]["key"]["participant"] == antigo
        consultor = sistema.db.fetchone("SELECT * FROM consultants")
        assert consultor["phone"] == "5588999990000"

    def test_lid_resolvido_separa_identidade_de_autor(self, sistema):
        """O telefone identifica o consultor; o LID continua sendo o autor citado."""
        lid = "209384756473829@lid"
        r = sistema.cliente.post("/webhook/whatsapp", headers=CABECALHO, json={
            "event": "messages.upsert",
            "data": {"key": {"remoteJid": GRUPO, "fromMe": False, "id": "3EB0LID1",
                             "participant": lid, "senderPn": CONSULTOR_B},
                     "pushName": "Bruno",
                     "message": {"conversation": _pedido("Cliente Teste", CPF_A)}}})
        assert r.status_code == 200
        assert sistema.esperar_desfecho()
        linha = sistema.linha(source_message_id="3EB0LID1")
        assert linha["sender_id"] == CONSULTOR_B, "identidade tinha de ser o telefone"
        assert linha["participant"] == lid, "a citação tem de usar o autor como veio"
        _rota, payload = sistema.servidor.envios()[0]
        assert payload["quoted"]["key"]["participant"] == lid

    def test_lid_nao_resolvido_nao_perde_o_pedido(self, sistema):
        lid = "111222333444555@lid"
        sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0LID2", participante=lid)
        assert sistema.esperar_desfecho()
        linha = sistema.linha(source_message_id="3EB0LID2")
        assert linha["sender_id"] == lid and linha["participant"] == lid
        avisos = sistema.db.fetchall(
            "SELECT message FROM logs WHERE level='WARNING' AND message LIKE '%LID%'")
        assert avisos, "o LID não resolvido tem de virar aviso no log"

    def test_leitura_do_webhook_preserva_os_dois(self):
        from app.evolution_webhook import interpretar

        base = {"event": "messages.upsert", "data": {
            "key": {"remoteJid": GRUPO, "fromMe": False, "id": "3EB0X",
                    "participant": "5511988887777@s.whatsapp.net"},
            "pushName": "Ana", "message": {"conversation": "oi"}}}
        leitura = interpretar(base, GRUPO)
        assert leitura.mensagem.sender_id == "5511988887777@s.whatsapp.net"
        assert leitura.mensagem.participant == "5511988887777@s.whatsapp.net"

        sem_participante = {"event": "messages.upsert", "data": {
            "key": {"remoteJid": "5511988887777@s.whatsapp.net", "fromMe": False, "id": "3EB0Y"},
            "message": {"conversation": "oi"}}}
        leitura = interpretar(sem_participante, "")
        assert leitura.mensagem.participant == "5511988887777@s.whatsapp.net", (
            "em conversa individual o próprio chat é o autor")


# ============================================ §26 — regressões dos bugs conhecidos
class TestRegressoes:
    def test_bug1_resposta_vai_para_o_chat_da_mensagem_e_nao_para_outro(self, tmp_path):
        """No DOM a resposta saía da conversa ABERTA. Na API, do chat_id gravado."""
        s = Sistema(tmp_path, com_imagem=False)
        outro_chat = "120363000000000999@g.us"
        mensagem = IncomingMessage(message_id="3EB0BUG1", chat_id=outro_chat,
                                   chat_name="Outro grupo", sender_id=CONSULTOR_A,
                                   sender_name="Ryan", text=_pedido("Cliente Teste", CPF_A))
        s.ligar()
        try:
            s.manager._handle_message(mensagem)
            assert s.esperar_desfecho()
            _rota, payload = s.servidor.envios()[0]
            assert payload["number"] == outro_chat, "respondeu no grupo configurado, não no de origem"
            assert payload["quoted"]["key"]["remoteJid"] == outro_chat
        finally:
            s.desligar()

    def test_bug1_sem_chat_id_nao_adivinha_destino(self, tmp_path):
        s = Sistema(tmp_path, com_imagem=False)
        r = s.manager.whatsapp.send("", "Consultores", "texto", quote_message_id="3EB0X")
        assert not r.ok and "chat_id" in r.motivo
        assert s.servidor.envios() == [], "caiu para o grupo configurado"

    def test_bug2_message_id_antigo_e_citado_exatamente(self, tmp_path):
        s = Sistema(tmp_path, com_imagem=False, atrasos={CPF_A: 0.6}).ligar()
        try:
            s.webhook(_pedido("Cliente A", CPF_A), "3EB0VELHA")
            for i in range(5):   # conversa nova chegando enquanto A simula
                s.webhook(f"bom dia {i}", f"3EB0NOVA{i}", participante=CONSULTOR_B)
            assert s.esperar_desfecho()
            _rota, payload = s.servidor.envios()[0]
            assert payload["quoted"]["key"]["id"] == "3EB0VELHA"
        finally:
            s.desligar()

    def test_bug3_quote_falha_resposta_chega(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        try:
            s.servidor.roteiro = [recusa_se_citar(400)]
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0BUG3", push="Ryan")
            assert s.esperar_desfecho()
            rotas = [r for r, _ in s.servidor.envios()]
            assert rotas == ["/message/sendMedia/allana", "/message/sendMedia/allana"]
            _rota, final = s.servidor.envios()[-1]
            assert "quoted" not in final and "↩ Ryan" in final["caption"]
            assert s.db.fetchone("SELECT delivery_status FROM simulations")[
                "delivery_status"] == Delivery.DELIVERED
        finally:
            s.desligar()

    def test_bug4_imagem_recusada_texto_chega(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        try:
            recusa_midia = (lambda rota, p: httpx.Response(400, text="media inválida")
                            if "sendMedia" in rota else None)
            # com citação e, no fallback da citação, sem ela: as duas recusadas
            s.servidor.roteiro = [recusa_midia, recusa_midia]
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0BUG4")
            assert s.esperar_desfecho()
            rotas = [r for r, _ in s.servidor.envios()]
            # a imagem é recusada com citação, tenta sem citação (400), depois texto
            assert rotas[-1] == "/message/sendText/allana"
            linha = s.db.fetchone("SELECT * FROM simulations")
            assert linha["delivery_status"] == Delivery.DELIVERED
            assert linha["media_status"] == "send_failed"
        finally:
            s.desligar()

    def test_bug5_worker_reinicia_e_continua_ligado_a_mensagem_original(self, tmp_path):
        s1 = Sistema(tmp_path, com_imagem=False).ligar()
        s1.servidor.roteiro = [recusa(503)]
        try:
            s1.webhook(_pedido("Cliente Teste", CPF_A), "3EB0BUG5", participante=CONSULTOR_A)
            assert s1.esperar_desfecho()
        finally:
            s1.desligar()
        request_id = s1.db.fetchone("SELECT request_id FROM simulations")["request_id"]

        s2 = Sistema(tmp_path, com_imagem=False, db_path=s1.config.db_path).ligar()
        try:
            s2.manager.queue.recover()
            s2.db.execute("UPDATE simulations SET next_delivery_at=?", (iso_atras(5),))
            s2.manager._reenviar_pendentes()
            linha = s2.linha(request_id=request_id)
            assert linha["delivery_status"] == Delivery.DELIVERED
            _rota, payload = s2.servidor.envios()[0]
            assert payload["quoted"]["key"]["id"] == "3EB0BUG5"
            assert CPF_A in payload["quoted"]["message"]["conversation"], (
                "a citação dependia de memória em RAM")
            assert request_id in payload["text"]
        finally:
            s2.desligar()

    def test_bug6_duplo_webhook_um_request_mesmo_em_paralelo_e_apos_reinicio(self, tmp_path):
        s = Sistema(tmp_path)
        mensagem = IncomingMessage(message_id="3EB0BUG6", chat_id=GRUPO, chat_name="C",
                                   sender_id=CONSULTOR_A, sender_name="Ryan",
                                   text=_pedido("Cliente Teste", CPF_A))
        aceitas: list[bool] = []
        threads = [threading.Thread(target=lambda: aceitas.append(
            s.manager.receber_mensagem(mensagem)["aceita"])) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert aceitas.count(True) == 1

        # "reinício": outro manager no mesmo banco recebe a reentrega
        s2 = Sistema(tmp_path, db_path=s.config.db_path)
        assert s2.manager.receber_mensagem(mensagem)["aceita"] is False

    def test_bug7_dois_consultores_simultaneos_nao_cruzam(self, tmp_path):
        s = Sistema(tmp_path, workers=2, atrasos={CPF_A: 0.5, CPF_B: 0.01}).ligar()
        try:
            barreira = threading.Barrier(2)

            def manda(texto, mid, participante, push):
                barreira.wait()
                s.manager.receber_mensagem(IncomingMessage(
                    message_id=mid, chat_id=GRUPO, chat_name="Consultores",
                    sender_id=participante, sender_name=push, text=texto,
                    participant=participante))

            ts = [threading.Thread(target=manda, args=(_pedido("Cliente A", CPF_A), "3EB0A7", CONSULTOR_A, "Ana")),
                  threading.Thread(target=manda, args=(_pedido("Cliente B", CPF_B), "3EB0B7", CONSULTOR_B, "Bruno"))]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            assert s.esperar_desfecho(2)
            for mid, cpf, consultor, participante in (("3EB0A7", CPF_A, "Ana", CONSULTOR_A),
                                                     ("3EB0B7", CPF_B, "Bruno", CONSULTOR_B)):
                linha = s.linha(source_message_id=mid)
                assert linha["cpf"] == cpf and linha["consultant_name"] == consultor
                assert linha["sender_id"] == participante
                payload = next(p for _r, p in s.servidor.envios()
                               if p.get("fileName") == f"{linha['request_id']}.png")
                assert payload["quoted"]["key"]["id"] == mid
                assert payload["quoted"]["key"]["participant"] == participante
        finally:
            s.desligar()

    def test_bug8_evolution_desconecta_e_o_job_fica_recuperavel(self, tmp_path):
        s = Sistema(tmp_path, com_imagem=False).ligar()
        try:
            def cai(rota, payload):
                raise httpx.ConnectError("Evolution fora do ar")
            s.servidor.roteiro = [cai]
            s.servidor.estado = "close"
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0BUG8")
            assert s.esperar_desfecho()
            linha = s.db.fetchone("SELECT * FROM simulations")
            assert linha["delivery_status"] == Delivery.RETRYING
            assert s.manager.whatsapp.atualizar_estado() == "close"
            assert not s.manager.whatsapp.status.connected   # o laço espera

            s.servidor.estado = "open"
            assert s.manager.whatsapp.atualizar_estado() == "open"
            s.db.execute("UPDATE simulations SET next_delivery_at=?", (iso_atras(5),))
            s.manager._reenviar_pendentes()
            linha = s.db.fetchone("SELECT * FROM simulations")
            assert linha["delivery_status"] == Delivery.DELIVERED
            assert linha["reply_attempts"] == 1
        finally:
            s.desligar()

    def test_bug9_renderizador_cai_e_o_proximo_render_recria(self, tmp_path):
        """Chromium real: o navegador morre, e depois a thread dona morre."""
        renderizador = PngRenderer()
        html = "<html><body><div class='folha' style='width:200px;height:80px'>ok</div></body></html>"
        try:
            primeiro = renderizador.render_png(html, tmp_path / "REQ000001.png", timeout=90)
            validar_png(primeiro)

            # o navegador cai (processo encerrado por fora)
            renderizador.call(lambda: renderizador._browser.close(), timeout=30)
            segundo = renderizador.render_png(html, tmp_path / "REQ000002.png", timeout=90)
            validar_png(segundo)

            # a thread dona morre sem ninguém ter pedido para parar
            from app.actor import SHUTDOWN
            renderizador._mailbox.put(SHUTDOWN)
            assert _aguardar(lambda: not renderizador.running, timeout=10)
            terceiro = renderizador.render_png(html, tmp_path / "REQ000003.png", timeout=90)
            validar_png(terceiro)
        finally:
            renderizador.stop()

    def test_bug10_orfao_so_o_perfil_do_bot_e_encerrado(self, monkeypatch, tmp_path):
        import app.navegador_zumbi as zumbi

        bot = str(tmp_path / ".simulator-profile")
        pessoal = r"C:\Users\Ryyan\AppData\Local\BraveSoftware\Brave-Browser\User Data"
        monkeypatch.setattr(zumbi.os, "name", "nt")
        monkeypatch.setattr(zumbi, "_processos_do_windows", lambda: [
            {"ProcessId": "111", "Name": "brave.exe",
             "CommandLine": f'brave.exe --user-data-dir="{bot}" --no-first-run'},
            {"ProcessId": "222", "Name": "brave.exe", "CommandLine": "brave.exe"},
            {"ProcessId": "333", "Name": "brave.exe",
             "CommandLine": f'brave.exe --user-data-dir="{pessoal}"'},
        ])
        mortos: list[str] = []
        monkeypatch.setattr(zumbi.subprocess, "run",
                            lambda cmd, **k: mortos.append(cmd[2]))

        assert zumbi.encerrar_orfaos(bot) == 1
        assert mortos == ["111"]

        mortos.clear()
        assert zumbi.encerrar_orfaos(pessoal) == 0, "fechou o navegador pessoal do operador"
        assert mortos == []


class TestAgenteRemotoAmarradoASimulacao:
    """Resultado do agente só entra na simulação para a qual foi pedido."""

    def _pendente(self, simulation_id: int = 5):
        from app.models import ParsedRequest
        from app.remote import RemoteSimulator

        remoto = RemoteSimulator()
        job = SimulationJob(
            request=ParsedRequest(consultant_name="Ryan", cpf=CPF_A, bank="Santander",
                                  contract=""),
            message=IncomingMessage(message_id="3EB0R", chat_id=GRUPO, chat_name="C",
                                    sender_id=CONSULTOR_A, sender_name="Ryan", text="x"),
            request_id="REQ000009", simulation_id=simulation_id)
        saida: list = []
        t = threading.Thread(target=lambda: saida.append(remoto.execute(job, timeout=5)))
        t.start()
        assert _aguardar(lambda: remoto.claim("e2e") is not None, timeout=5)
        return remoto, t, saida

    def test_resultado_de_outra_simulacao_com_o_mesmo_req_e_recusado(self):
        remoto, t, saida = self._pendente(simulation_id=5)
        assert remoto.submit_result("REQ000009", {"simulation_id": 99, "ok": True}) is False
        assert remoto.submit_result("REQ000009", {
            "simulation_id": 5, "ok": True, "status": "Não",
            "motivos": [{"texto": "CLIENTE EM ATRASO", "categoria": "cadastro"}]}) is True
        t.join(timeout=5)
        assert saida[0].job.simulation_id == 5
        assert saida[0].motivos[0]["texto"] == "CLIENTE EM ATRASO", "motivos do portal perdidos"

    def test_agente_antigo_sem_simulation_id_continua_aceito(self):
        remoto, t, saida = self._pendente()
        assert remoto.submit_result("REQ000009", {"ok": True, "status": "Sim"}) is True
        t.join(timeout=5)
        assert saida and saida[0].ok


class TestPerfisEObservabilidade:
    def test_whatsapp_e_santander_nunca_no_mesmo_perfil(self, tmp_path):
        from dataclasses import replace

        config = replace(_config(tmp_path), whatsapp_profile_dir=tmp_path / "perfil",
                         simulator_profile_dir=tmp_path / "perfil")
        db = Database(config.db_path)
        with pytest.raises(RuntimeError, match="MESMO"):
            BotManager(config, db, EventHub(db))

    def test_timeline_reconstroi_o_incidente(self, sistema):
        sistema.webhook(_pedido("Cliente Teste", CPF_A), "3EB0TL01")
        assert sistema.esperar_desfecho()
        request_id = sistema.db.fetchone("SELECT request_id FROM simulations")["request_id"]
        etapas = [e["stage"] for e in sistema.hub.timeline(request_id) if e["stage"]]
        for esperada in (Stage.VALIDATED, Stage.QUEUED, Stage.PROCESSING, Stage.CONSULTING,
                         Stage.EXTRACTING, Stage.RENDERING, Stage.REPLYING, Stage.COMPLETED):
            assert esperada in etapas, f"faltou {esperada} na timeline: {etapas}"
        assert etapas.index(Stage.RENDERING) < etapas.index(Stage.COMPLETED)
        assert etapas[-1] == Stage.COMPLETED, "completed antes do fim da entrega"

    def test_webhook_em_lote_nao_perde_mensagem(self, sistema):
        """Tratar só o primeiro item do lote descartava o resto com 200."""
        def item(mid, participante, cpf, nome):
            return {"key": {"remoteJid": GRUPO, "fromMe": False, "id": mid,
                            "participant": participante},
                    "pushName": nome, "message": {"conversation": _pedido(nome, cpf)}}

        r = sistema.cliente.post("/webhook/whatsapp", headers=CABECALHO, json={
            "event": "messages.upsert",
            "data": [item("3EB0LOTE1", CONSULTOR_A, CPF_A, "Ana"),
                     item("3EB0LOTE2", CONSULTOR_B, CPF_B, "Bruno")]})
        assert r.status_code == 200
        assert len(r.json()["mensagens"]) == 2
        assert sistema.esperar_desfecho(2)
        assert {l["source_message_id"] for l in sistema.db.fetchall(
            "SELECT source_message_id FROM simulations")} == {"3EB0LOTE1", "3EB0LOTE2"}

    @pytest.mark.parametrize("escrito", [
        "529.982.247-25", "529.982.247.25", "529 982 247 25", "52998224725"])
    def test_cpf_mascarado_em_todos_os_formatos_do_grupo(self, escrito):
        from app.security import redact
        saida = redact(f"Paulo Testes\n{escrito}\nAmapá")
        assert "529.***.***-25" in saida
        assert "98224" not in saida.replace(".", "").replace(" ", "")

    def test_mascara_nao_come_ids_nem_telefones(self):
        from app.security import redact
        texto = "REQ000123 5567999990001@s.whatsapp.net 3EB0C5A277F7F9B6C599"
        assert redact(texto) == texto

    def test_logs_e_eventos_nao_levam_cpf_nem_chave(self, sistema):
        sistema.webhook("Paulo Testes\nAmapá\n529.982.247.25", "3EB0SEG1")
        assert sistema.esperar_desfecho()
        logs = " ".join(r["message"] for r in sistema.db.fetchall("SELECT message FROM logs"))
        eventos = " ".join(r["payload_json"] or "" for r in
                           sistema.db.fetchall("SELECT payload_json FROM events"))
        for texto in (logs, eventos):
            assert "529.982.247.25" not in texto and "52998224725" not in texto
            assert "chave-de-teste" not in texto


# ================================================ entrega incerta: ação manual
class TestAcaoManualDaEntregaIncerta:
    """`unconfirmed` nunca reenvia sozinho. Quem desempata é uma pessoa no painel.

    "Chegou" fecha sem mandar nada. "Não chegou" libera UM reenvio, citando o
    mesmo pedido, pelas mesmas travas do laço. Dois cliques não viram dois.
    """

    def _incerta(self, s: Sistema, mid: str = "3EB0MANUAL") -> dict:
        s.servidor.roteiro = [recusa(500, "Internal server error")]
        s.webhook(_pedido("Cliente Teste", CPF_A), mid)
        assert s.esperar_desfecho(), "a entrega não se resolveu"
        linha = s.linha(source_message_id=mid) or {}
        assert linha["delivery_status"] == Delivery.UNCONFIRMED
        assert len(s.servidor.envios()) == 1
        return linha

    def _login(self, s: Sistema) -> None:
        assert s.cliente.post("/api/login", data={"password": "senha-de-teste"}).status_code == 200

    def test_chegou_fecha_como_entregue_sem_enviar_nada(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        try:
            linha = self._incerta(s)
            self._login(s)
            r = s.cliente.post(f"/api/simulations/{linha['id']}/entrega", json={"acao": "chegou"})
            assert r.status_code == 200, r.text
            depois = s.linha(id=linha["id"])
            assert depois["delivery_status"] == Delivery.DELIVERED
            assert depois["replied_at"]
            assert depois["stage"] == Stage.COMPLETED
            assert depois["delivery_resolution"] == "manual:chegou"
            assert depois["sent_message_id"] == "", "inventou prova de entrega"
            s.manager._reenviar_pendentes()
            assert len(s.servidor.envios()) == 1, "marcar como entregue não pode enviar nada"
        finally:
            s.desligar()

    def test_nao_chegou_libera_um_reenvio_citando_o_mesmo_pedido(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        try:
            linha = self._incerta(s, "3EB0NAOCHEGOU")
            self._login(s)
            r = s.cliente.post(f"/api/simulations/{linha['id']}/entrega",
                               json={"acao": "nao_chegou"})
            assert r.status_code == 200, r.text
            assert s.linha(id=linha["id"])["delivery_status"] == Delivery.RETRYING
            saidas = [m["status"] for m in s.db.fetchall(
                "SELECT status FROM messages WHERE simulation_id=? AND direction='out'",
                (linha["id"],))]
            assert saidas == ["failed"], (
                f"a saída incerta tinha de virar 'failed' (conferida no painel): {saidas}")

            s.manager._reenviar_pendentes()
            envios = s.servidor.envios()
            assert len(envios) == 2, [rota for rota, _ in envios]
            assert envios[-1][1]["quoted"]["key"]["id"] == "3EB0NAOCHEGOU"
            depois = s.linha(id=linha["id"])
            assert depois["delivery_status"] == Delivery.DELIVERED
            assert depois["sent_message_id"]
            assert depois["delivery_resolution"] == "manual:nao_chegou"

            s.manager._reenviar_pendentes()
            assert len(s.servidor.envios()) == 2, "o reenvio liberado saiu mais de uma vez"
        finally:
            s.desligar()

    def test_dois_cliques_simultaneos_liberam_um_reenvio_so(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        try:
            linha = self._incerta(s, "3EB0DOISCLIQUES")
            resultados: list[dict] = []
            largada = threading.Barrier(4)

            def clicar(acao):
                largada.wait()
                resultados.append(s.manager.resolver_entrega_incerta(linha["id"], acao))

            threads = [threading.Thread(target=clicar, args=(acao,))
                       for acao in ("nao_chegou", "nao_chegou", "chegou", "nao_chegou")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)
            assert sum(1 for r in resultados if r["ok"]) == 1, resultados
            s.manager._reenviar_pendentes()
            s.manager._reenviar_pendentes()
            assert len(s.servidor.envios()) <= 2
        finally:
            s.desligar()

    def test_so_vale_para_entrega_incerta(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        try:
            s.webhook(_pedido("Cliente Teste", CPF_A), "3EB0ENTREGUE")
            assert s.esperar_desfecho()
            entregue = s.linha(source_message_id="3EB0ENTREGUE")
            assert entregue["delivery_status"] == Delivery.DELIVERED
            self._login(s)
            url = f"/api/simulations/{entregue['id']}/entrega"
            r = s.cliente.post(url, json={"acao": "nao_chegou"})
            assert r.status_code == 409, r.text
            assert s.cliente.post(url, json={"acao": "apagar"}).status_code == 400
            assert s.cliente.post(url, content=b"nao e json",
                                  headers={"Content-Type": "application/json"}).status_code == 400
            # Formulário text/plain de outra página do mesmo site: recusado.
            assert s.cliente.post(url, content=b'{"acao":"nao_chegou"}',
                                  headers={"Content-Type": "text/plain"}).status_code == 415
            assert s.cliente.post("/api/simulations/999999/entrega",
                                  json={"acao": "chegou"}).status_code == 404
            assert len(s.servidor.envios()) == 1
        finally:
            s.desligar()

    def test_exige_sessao(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        try:
            linha = self._incerta(s, "3EB0SEMSESSAO")
            r = s.cliente.post(f"/api/simulations/{linha['id']}/entrega", json={"acao": "nao_chegou"})
            assert r.status_code == 401
            assert s.linha(id=linha["id"])["delivery_status"] == Delivery.UNCONFIRMED
        finally:
            s.desligar()

    def test_envio_preso_em_sending_nao_trava_o_reenvio_liberado(self, tmp_path):
        """Sem isto o botão não faria nada: situacao_do_envio veria 'incerta'."""
        s = Sistema(tmp_path).ligar()
        try:
            linha = self._incerta(s, "3EB0PRESO")
            s.db.execute("UPDATE messages SET status='sending' WHERE simulation_id=? "
                         "AND direction='out'", (linha["id"],))
            assert s.manager.resolver_entrega_incerta(linha["id"], "nao_chegou")["ok"]
            s.manager._reenviar_pendentes()
            assert len(s.servidor.envios()) == 2
            assert s.linha(id=linha["id"])["delivery_status"] == Delivery.DELIVERED
        finally:
            s.desligar()

    def test_tentativas_esgotadas_ainda_liberam_uma(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        try:
            linha = self._incerta(s, "3EB0ESGOTADA")
            s.db.execute("UPDATE simulations SET reply_attempts=? WHERE id=?",
                         (s.manager._MAX_REENVIOS, linha["id"]))
            assert s.manager.resolver_entrega_incerta(linha["id"], "nao_chegou")["ok"]
            s.manager._reenviar_pendentes()
            assert len(s.servidor.envios()) == 2
        finally:
            s.desligar()

    def test_decisao_fica_registrada_no_log_e_na_timeline(self, tmp_path):
        s = Sistema(tmp_path).ligar()
        try:
            linha = self._incerta(s, "3EB0AUDITORIA")
            assert s.manager.resolver_entrega_incerta(linha["id"], "chegou", quem="admin")["ok"]
            logs = [l["message"] for l in s.db.fetchall(
                "SELECT message FROM logs WHERE request_id=?", (linha["request_id"],))]
            assert any("chegou (por admin)" in m for m in logs), logs
            tipos = [e["type"] for e in s.db.fetchall(
                "SELECT type FROM events WHERE request_id=?", (linha["request_id"],))]
            assert "delivery_manual" in tipos, tipos
        finally:
            s.desligar()
