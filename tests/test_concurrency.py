"""Testes de ponta a ponta com varias solicitacoes simultaneas.

O requisito central e' este: *uma solicitacao nunca pode receber o resultado de
outra*. Aqui o WhatsApp e o simulador sao substituidos por dublês, mas o
manager, a fila, o banco e o barramento de eventos sao os reais.
"""

from __future__ import annotations

import random
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import Config
from app.db import Database
from app.events import EventHub
from app.jobs import QueueService
from app.manager import BotManager
from app.models import (IncomingMessage, ParsedRequest, SimulationJob,
                        SimulationResult, Stage, Status)
from app.whatsapp import WhatsAppStatus

TZ = ZoneInfo("America/Sao_Paulo")

# CPFs validos distintos, um por consultor, para provar que nada se mistura.
CPFS = ["52998224725", "11144477735", "19195258400", "12345678909", "40481494235"]
CONSULTORES = ["Ryan", "João", "Pedro", "Ana", "Marcos"]


# ------------------------------------------------------------------- dubles
def png_valido(largura: int = 4, altura: int = 3, semente: bytes = b"") -> bytes:
    """Um PNG de verdade, pequeno, que passa em `renderer.validar_png`.

    Os dublês gravavam só a assinatura seguida de lixo ("basta existir").
    Com a validação real do PNG isso deixou de passar -- e é bom que não
    passe: um PNG truncado chegava ao grupo como imagem quebrada.
    `semente` muda os pixels para cada imagem ser distinguível.
    """
    import struct
    import zlib

    def bloco(tipo: bytes, dados: bytes) -> bytes:
        return (struct.pack(">I", len(dados)) + tipo + dados
                + struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF))

    cor = (semente or b"\x10\x20\x30")[:3].ljust(3, b"\x00")
    linhas = b"".join(b"\x00" + cor * largura for _ in range(altura))
    return (b"\x89PNG\r\n\x1a\n"
            + bloco(b"IHDR", struct.pack(">IIBBBBB", largura, altura, 8, 2, 0, 0, 0))
            + bloco(b"IDAT", zlib.compress(linhas))
            + bloco(b"IEND", b""))


class FakeWhatsApp:
    """Substitui o servico real. Registra tudo que foi enviado, com o destino."""

    def __init__(self, delay: float = 0.0, fail_every: int = 0,
                 image_fails: bool = False, render_fails: bool = False,
                 cita: bool = True):
        self.sent: list[dict] = []
        self.images: list[dict] = []
        self.inbox: list = []
        self._lock = threading.Lock()
        self._delay = delay
        self._fail_every = fail_every
        self._image_fails = image_fails
        self._render_fails = render_fails
        # `cita=False` reproduz a citação falhando: a camada real escolhe a
        # versão com o nome do consultor, e o dublê tem de fazer o mesmo.
        self._cita = cita
        self.status = WhatsAppStatus(state="connected", phone="+55 67 90000-0000",
                                     chat_name="Consultores", chat_id="grupo@g.us")
        self._count = 0

    def render_png(self, html, path, width=900, timeout=60.0):
        if self._render_fails:
            raise RuntimeError("navegador indisponível para renderizar")
        destino = Path(path)
        destino.parent.mkdir(parents=True, exist_ok=True)
        # PNG real e pequeno: a validação do manager lê o arquivo de verdade.
        destino.write_bytes(png_valido(semente=html.encode("utf-8")[-3:]))
        return str(destino)

    def send_image(self, chat_id, chat_name, image_path, caption="",
                   quote_message_id="", timeout=120.0, caption_sem_citacao="",
                   quote_text="", quote_participant=""):
        # O dublê acompanha a assinatura real de propósito. Quando ele fica
        # para trás, o erro aparece como "resultado pronto mas não entregue"
        # em dezenas de testes — e o motivo real (`unexpected keyword
        # argument`) fica escondido num log.
        if caption_sem_citacao and not self._cita:
            caption = caption_sem_citacao
        if self._image_fails:
            raise RuntimeError("não encontrei o campo de anexo do WhatsApp")
        with self._lock:
            self.images.append({
                "chat_id": chat_id, "chat_name": chat_name,
                "path": str(image_path), "caption": caption, "quote": quote_message_id,
                "quote_text": quote_text, "quote_participant": quote_participant,
            })
        return True

    def send(self, chat_id, chat_name, text, quote_message_id="", timeout=90.0,
             texto_sem_citacao="", quote_text="", quote_participant=""):
        if texto_sem_citacao and not self._cita:
            text = texto_sem_citacao
        if self._delay:
            time.sleep(self._delay)
        with self._lock:
            self._count += 1
            if self._fail_every and self._count % self._fail_every == 0:
                raise RuntimeError("WhatsApp indisponível")
            self.sent.append(
                {
                    "chat_id": chat_id,
                    "chat_name": chat_name,
                    "text": text,
                    "quote": quote_message_id,
                    "quote_text": quote_text,
                    "quote_participant": quote_participant,
                }
            )
        return True

    def start(self): ...
    def stop(self, *a, **k): ...
    def request_reconnect(self): ...
    def qr_data_url(self): return ""


class FakeSimulator:
    """Devolve um resultado derivado *do proprio job*, para expor cruzamentos."""

    def __init__(self, name="fake-sim", delay_range=(0.01, 0.05), fail_for=(), flaky_for=()):
        self.name = name
        self._delay_range = delay_range
        self._fail_for = set(fail_for)
        self._flaky_for = dict.fromkeys(flaky_for, 0)
        self.seen: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0
        self._lock = threading.Lock()

    def execute(self, job: SimulationJob, on_stage=None, timeout=None) -> SimulationResult:
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            self.seen.append(job.request_id)
        try:
            if on_stage:
                on_stage(Stage.CONSULTING)
            time.sleep(random.uniform(*self._delay_range))
            if on_stage:
                on_stage(Stage.EXTRACTING)

            if job.request_id in self._fail_for:
                return SimulationResult(job=job, ok=False, status="Erro",
                                        error="falha permanente", retryable=False)
            if job.request_id in self._flaky_for and self._flaky_for[job.request_id] == 0:
                self._flaky_for[job.request_id] += 1
                return SimulationResult(job=job, ok=False, status="Timeout",
                                        error="instabilidade", retryable=True)

            # O valor codifica o CPF do proprio job: se houver mistura, aparece aqui.
            return SimulationResult(
                job=job,
                ok=True,
                status="Sim",
                reduction_value=float(int(job.request.cpf[-4:])),
                margin=f"margem-{job.request.contract}",
                contracts=({"contrato": job.request.contract},),
                installment_sum=10.0,
                installment_count=1,
                debt_sum=20.0,
            )
        finally:
            with self._lock:
                self.concurrent -= 1

    def start(self): ...
    def stop(self, *a, **k): ...
    def status(self): return {"name": self.name, "running": True, "ready": True, "busy": False}


# ----------------------------------------------------------------- fixtures
def _config(tmp_path, workers=1, max_attempts=2, timeout=30.0,
            send_image=False, simulator_mode="local", agent_token="",
            require_trigger=False, whatsapp_mode="dom",
            evolution_group_jid="120363000000000000@g.us",
            evolution_webhook_token="", alerta_fila=10,
            alerta_espera_minutos=5.0,
            imagem_da_resposta="portal") -> Config:
    return Config(
        sim_bot_path=tmp_path / "arqueiro",
        simulator_profile_dir=tmp_path / "sim-profile",
        simulator_enabled=False,
        job_timeout_seconds=timeout,
        max_attempts=max_attempts,
        default_phone="11999998888",
        browser_executable="",
        simulator_mode=simulator_mode,
        agent_token=agent_token,
        whatsapp_group_name="Consultores",
        bot_self_name="Operacional Capital",
        whatsapp_profile_dir=tmp_path / "wa-profile",
        whatsapp_headless=True,
        # false é o padrão real: o CPF é o gatilho.
        require_trigger=require_trigger,
        poll_seconds=1.0,
        reply_quote=True,
        send_result_image=send_image,
        image_show_client_data=True,
        imagem_da_resposta=imagem_da_resposta,
        whatsapp_mode=whatsapp_mode,
        evolution_url="http://evolution-de-teste:8080",
        evolution_api_key="chave-de-teste",
        evolution_instance="allana",
        evolution_group_jid=evolution_group_jid,
        evolution_webhook_token=evolution_webhook_token,
        alerta_fila=alerta_fila,
        alerta_espera_minutos=alerta_espera_minutos,
        supported_banks={"santander"},
        worker_count=workers,
        db_path=tmp_path / "teste.db",
        state_path=tmp_path / "state.json",
        dashboard_password="senha-de-teste",
        web_host="127.0.0.1",
        web_port=8123,
        session_secret="segredo-de-teste",
        session_hours=12,
        mask_cpf_in_ui=True,
        timezone="America/Sao_Paulo",
        retention_days=180,
    )


@pytest.fixture()
def sistema(tmp_path):
    """Manager real, com WhatsApp e simulador substituidos."""
    config = _config(tmp_path)
    db = Database(config.db_path)
    hub = EventHub(db)
    manager = BotManager(config, db, hub)

    whatsapp = FakeWhatsApp()
    simulator = FakeSimulator()
    manager.whatsapp = whatsapp
    manager.simulators = [simulator]
    manager.queue = QueueService(
        db=db, hub=hub, simulators=[simulator],
        max_attempts=config.max_attempts, job_timeout=config.job_timeout_seconds,
        on_result=manager._deliver_result, on_log=manager._queue_log,
    )
    manager.queue.start()
    try:
        yield manager, whatsapp, simulator, db, hub
    finally:
        manager.queue.stop()


def _mensagem(indice: int, texto: str | None = None) -> IncomingMessage:
    consultor = CONSULTORES[indice]
    return IncomingMessage(
        message_id=f"false_grupo@g.us_MSG{indice:04d}_55679{indice}8887777@c.us",
        chat_id="grupo@g.us",
        chat_name="Consultores",
        sender_id=f"55679{indice}8887777@c.us",
        sender_name=consultor,
        text=texto
        or (
            f"{consultor}\n"
            f"CPF: {CPFS[indice]}\n"
            "Banco: Santander\n"
            f"Contrato: {900000 + indice}\n"
            "Fazer simulação"
        ),
    )


def _aguardar(condicao, timeout=30.0, intervalo=0.05):
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condicao():
            return True
        time.sleep(intervalo)
    return False


# ================================================================== testes
class TestIsolamentoEntreSolicitacoes:
    def test_uma_solicitacao(self, sistema):
        manager, whatsapp, _sim, db, _hub = sistema
        manager._handle_message(_mensagem(0))

        # Esperar por `replied_at`, e não por `status=completed`: o status é
        # gravado ANTES do envio da resposta, então existe uma janela em que a
        # simulação já está concluída mas o consultor ainda não recebeu nada.
        assert _aguardar(lambda: (linha := db.fetchone(
            "SELECT replied_at FROM simulations")) and linha["replied_at"])

        linha = db.fetchone("SELECT * FROM simulations")
        assert linha["status"] == Status.COMPLETED
        assert linha["consultant_name"] == "Ryan"
        assert linha["cpf"] == CPFS[0]
        assert linha["contract"] == "900000"
        assert linha["attempts"] == 1
        assert linha["processing_seconds"] is not None

        # UMA mensagem: só o resultado.
        #
        # A confirmação de recebimento foi removida de propósito — sendo o
        # próximo da fila, o resultado sai em ~90 s e o "recebi" vira a
        # segunda mensagem onde uma bastava. Ela só volta a existir quando há
        # alguém na frente (ver `test_avisa_quando_ha_espera`).
        assert len(whatsapp.sent) == 1, (
            f"esperava só o resultado, veio: {[m['text'][:40] for m in whatsapp.sent]}")
        # Sem diferenciar caixa: o nome do cliente sai em CAIXA ALTA no
        # formato novo, e o que este teste protege e' a identidade, nao a
        # grafia.
        assert "ryan" in whatsapp.sent[-1]["text"].casefold()
        assert whatsapp.sent[-1]["quote"] == _mensagem(0).message_id, (
            "o resultado tem de estar ancorado na mensagem do consultor")

    @pytest.mark.parametrize("quantidade", [2, 5])
    def test_varias_simultaneas_nunca_se_misturam(self, sistema, quantidade):
        manager, whatsapp, _sim, db, _hub = sistema

        threads = [
            threading.Thread(target=manager._handle_message, args=(_mensagem(i),))
            for i in range(quantidade)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)

        assert _aguardar(lambda: db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE status=?",
            (Status.COMPLETED,)) == quantidade), "nem todas concluíram"

        linhas = db.fetchall("SELECT * FROM simulations ORDER BY id")
        assert len({l["request_id"] for l in linhas}) == quantidade, "request_id repetido"

        for linha in linhas:
            indice = CONSULTORES.index(linha["consultant_name"])
            # Cada campo tem de continuar coerente com o pedido de origem
            assert linha["cpf"] == CPFS[indice]
            assert linha["contract"] == str(900000 + indice)
            assert linha["margin"] == f"margem-{900000 + indice}"
            assert linha["reduction_value"] == float(int(CPFS[indice][-4:]))

            # E a resposta enviada tem de citar o consultor certo e o ID certo.
            # O filtro é o request_id: as falas do bot mudaram de tom, e
            # amarrar o teste a uma frase específica quebraria a cada ajuste
            # de texto sem que nada de real tivesse mudado.
            respostas = [
                m for m in whatsapp.sent if linha["request_id"] in m["text"]
            ]
            assert len(respostas) == 1, f"{linha['request_id']} sem resposta única"
            corpo = respostas[0]["text"].casefold()
            assert linha["consultant_name"].casefold() in corpo
            for outro in CONSULTORES[:quantidade]:
                if outro != linha["consultant_name"]:
                    assert outro.casefold() not in corpo, "resposta cruzada entre consultores"

    def test_consultores_sao_cadastrados_separadamente(self, sistema):
        manager, _wa, _sim, db, _hub = sistema
        for i in range(5):
            manager._handle_message(_mensagem(i))

        assert _aguardar(lambda: db.scalar("SELECT COUNT(*) FROM consultants") == 5)
        nomes = {r["name"] for r in db.fetchall("SELECT name FROM consultants")}
        assert nomes == set(CONSULTORES)
        jids = {r["wa_id"] for r in db.fetchall("SELECT wa_id FROM consultants")}
        assert len(jids) == 5 and "false" not in jids

    def test_mesmo_consultor_com_varios_pedidos(self, sistema):
        manager, whatsapp, _sim, db, _hub = sistema
        for contrato in ("111111", "222222", "333333"):
            mensagem = _mensagem(0, texto=(
                f"Ryan\nCPF: {CPFS[0]}\nBanco: Santander\n"
                f"Contrato: {contrato}\nFazer simulação"
            ))
            # message_id unico por pedido
            mensagem = IncomingMessage(
                message_id=f"false_grupo@g.us_MSG-{contrato}_5567908887777@c.us",
                chat_id=mensagem.chat_id, chat_name=mensagem.chat_name,
                sender_id=mensagem.sender_id, sender_name=mensagem.sender_name,
                text=mensagem.text,
            )
            manager._handle_message(mensagem)

        assert _aguardar(lambda: db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE status=?", (Status.COMPLETED,)) == 3)

        assert db.scalar("SELECT COUNT(*) FROM consultants") == 1  # um consultor so'
        contratos = {r["contract"] for r in db.fetchall("SELECT contract FROM simulations")}
        assert contratos == {"111111", "222222", "333333"}
        for contrato in contratos:
            linha = db.fetchone("SELECT * FROM simulations WHERE contract=?", (contrato,))
            assert linha["margin"] == f"margem-{contrato}"

    def test_mensagens_rapidas_em_rajada(self, sistema):
        manager, _wa, _sim, db, _hub = sistema
        for i in range(5):
            manager._handle_message(_mensagem(i))

        assert _aguardar(lambda: db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE status IN (?,?)",
            (Status.COMPLETED, Status.ERROR)) == 5)
        assert db.scalar("SELECT COUNT(DISTINCT request_id) FROM simulations") == 5

    def test_simulador_nunca_recebe_dois_jobs_ao_mesmo_tempo(self, sistema):
        """A sessao do Santander e' unica: o despacho tem de ser serial."""
        manager, _wa, simulator, db, _hub = sistema
        for i in range(5):
            manager._handle_message(_mensagem(i))

        assert _aguardar(lambda: len(simulator.seen) == 5)
        assert simulator.max_concurrent == 1


class TestTentativasEFalhas:
    def test_erro_recuperavel_gera_nova_tentativa(self, tmp_path):
        config = _config(tmp_path, max_attempts=3)
        db, hub = Database(config.db_path), None
        hub = EventHub(db)
        manager = BotManager(config, db, hub)
        manager.whatsapp = FakeWhatsApp()

        simulator = FakeSimulator(flaky_for=["REQ000001"])
        manager.simulators = [simulator]
        manager.queue = QueueService(
            db=db, hub=hub, simulators=[simulator], max_attempts=3, job_timeout=30.0,
            on_result=manager._deliver_result, on_log=manager._queue_log,
        )
        manager.queue.start()
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE status=?", (Status.COMPLETED,)) == 1,
                timeout=40)
            linha = db.fetchone("SELECT * FROM simulations")
            assert linha["attempts"] == 2, "deveria ter tentado duas vezes"
            assert linha["status"] == Status.COMPLETED
        finally:
            manager.queue.stop()

    def test_erro_permanente_nao_e_repetido(self, tmp_path):
        config = _config(tmp_path, max_attempts=3)
        db = Database(config.db_path)
        hub = EventHub(db)
        manager = BotManager(config, db, hub)
        whatsapp = FakeWhatsApp()
        manager.whatsapp = whatsapp

        simulator = FakeSimulator(fail_for=["REQ000001"])
        manager.simulators = [simulator]
        manager.queue = QueueService(
            db=db, hub=hub, simulators=[simulator], max_attempts=3, job_timeout=30.0,
            on_result=manager._deliver_result, on_log=manager._queue_log,
        )
        manager.queue.start()
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE status=?", (Status.ERROR,)) == 1)
            linha = db.fetchone("SELECT * FROM simulations")
            assert linha["attempts"] == 1
            assert "falha permanente" in linha["error_message"]
            # O consultor e' avisado do erro, com o ID da propria solicitacao
            assert any("ryan" in m["text"].casefold()
                       and linha["request_id"] in m["text"]
                       for m in whatsapp.sent)
        finally:
            manager.queue.stop()

    def test_falha_no_envio_marca_a_mensagem_como_failed(self, tmp_path):
        config = _config(tmp_path)
        db = Database(config.db_path)
        hub = EventHub(db)
        manager = BotManager(config, db, hub)
        # Falha em todo envio
        manager.whatsapp = FakeWhatsApp(fail_every=1)
        simulator = FakeSimulator()
        manager.simulators = [simulator]
        manager.queue = QueueService(
            db=db, hub=hub, simulators=[simulator], max_attempts=1, job_timeout=30.0,
            on_result=manager._deliver_result, on_log=manager._queue_log,
        )
        manager.queue.start()
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM messages WHERE direction='out' AND status='failed'") >= 1)
            # A simulacao roda mesmo assim; so' nao marca replied_at
            assert _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE status=?", (Status.COMPLETED,)) == 1)
            assert db.fetchone("SELECT replied_at FROM simulations")["replied_at"] is None
        finally:
            manager.queue.stop()


class TestReinicioDoSistema:
    def test_fila_e_retomada_apos_reinicio(self, tmp_path):
        config = _config(tmp_path, max_attempts=3)
        db = Database(config.db_path)
        hub = EventHub(db)

        # Simula o estado deixado por uma queda: uma na fila, uma processando.
        for request_id, status in (("REQ000001", Status.QUEUED), ("REQ000002", Status.PROCESSING)):
            db.insert("simulations", {
                "request_id": request_id, "consultant_name": "Ryan", "chat_id": "grupo@g.us",
                "sender_id": "5567908887777@c.us", "sender_name": "Ryan",
                "source_message_id": f"false_grupo@g.us_{request_id}_5567908887777@c.us",
                "cpf": CPFS[0], "bank": "Santander", "contract": "900000",
                "status": status, "stage": status, "attempts": 1, "max_attempts": 3,
                "raw_message": "Fazer simulação",
                "created_at": "2026-08-27T10:00:00Z", "updated_at": "2026-08-27T10:00:00Z",
            })

        manager = BotManager(config, db, hub)
        manager.whatsapp = FakeWhatsApp()
        simulator = FakeSimulator()
        manager.simulators = [simulator]
        manager.queue = QueueService(
            db=db, hub=hub, simulators=[simulator], max_attempts=3, job_timeout=30.0,
            on_result=manager._deliver_result, on_log=manager._queue_log,
        )
        manager.queue.start()
        try:
            assert manager.queue.recover() == 2
            assert _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE status=?", (Status.COMPLETED,)) == 2)
            assert sorted(simulator.seen) == ["REQ000001", "REQ000002"]
        finally:
            manager.queue.stop()

    def test_tentativas_esgotadas_viram_interrompida(self, tmp_path):
        config = _config(tmp_path, max_attempts=2)
        db = Database(config.db_path)
        hub = EventHub(db)
        db.insert("simulations", {
            "request_id": "REQ000009", "consultant_name": "Ryan", "chat_id": "grupo@g.us",
            "cpf": CPFS[0], "bank": "Santander", "contract": "1",
            "status": Status.PROCESSING, "stage": Status.PROCESSING,
            "attempts": 2, "max_attempts": 2,
            "created_at": "2026-08-27T10:00:00Z", "updated_at": "2026-08-27T10:00:00Z",
        })

        simulator = FakeSimulator()
        queue = QueueService(db=db, hub=hub, simulators=[simulator], max_attempts=2,
                             job_timeout=30.0)
        assert queue.recover() == 0
        linha = db.fetchone("SELECT * FROM simulations WHERE request_id='REQ000009'")
        assert linha["status"] == Status.INTERRUPTED
        assert simulator.seen == []


class TestValidacaoDeEntrada:
    def test_mensagem_comum_nao_vira_solicitacao(self, sistema):
        manager, whatsapp, _sim, db, _hub = sistema
        manager._handle_message(_mensagem(0, texto="bom dia, alguém aí?"))
        time.sleep(0.3)
        assert db.scalar("SELECT COUNT(*) FROM simulations") == 0
        assert whatsapp.sent == []
        # ...mas a mensagem recebida fica registrada para o console operacional
        assert db.scalar("SELECT COUNT(*) FROM messages WHERE direction='in'") == 1

    def test_dados_incompletos_recebem_aviso(self, sistema):
        manager, whatsapp, _sim, db, _hub = sistema
        manager._handle_message(_mensagem(0, texto="Fazer simulação\nBanco: Santander"))
        time.sleep(0.3)
        assert db.scalar("SELECT COUNT(*) FROM simulations") == 0
        assert len(whatsapp.sent) == 1
        assert ("CPF" in whatsapp.sent[0]["text"]
                and "ryan" in whatsapp.sent[0]["text"].casefold())

    def test_banco_nao_suportado_e_recusado(self, sistema):
        manager, whatsapp, _sim, db, _hub = sistema
        manager._handle_message(_mensagem(0, texto=(
            f"Fazer simulação\nCPF: {CPFS[0]}\nBanco: Bradesco\nContrato: 123"
        )))
        time.sleep(0.3)
        assert db.scalar("SELECT COUNT(*) FROM simulations") == 0
        assert "Bradesco" in whatsapp.sent[0]["text"]
        assert "Santander" in whatsapp.sent[0]["text"]


class TestEventosEmTempoReal:
    def test_ciclo_completo_publica_a_timeline(self, sistema):
        manager, _wa, _sim, db, hub = sistema
        sub = hub.subscribe()
        manager._handle_message(_mensagem(0))

        assert _aguardar(lambda: db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE status=?", (Status.COMPLETED,)) == 1)

        tipos = []
        while True:
            evento = sub.get(timeout=0.4)
            if evento is None:
                break
            tipos.append(evento["type"])

        for esperado in (
            "message_received", "consultant_identified", "request_created",
            "job_queued", "job_progress", "job_done", "message_sent",
        ):
            assert esperado in tipos, f"faltou o evento {esperado}"

        # E a timeline persistida sobrevive a um F5
        request_id = db.fetchone("SELECT request_id FROM simulations")["request_id"]
        etapas = [e["stage"] for e in hub.timeline(request_id)]
        assert Stage.VALIDATED in etapas
        assert Stage.QUEUED in etapas
        assert Stage.PROCESSING in etapas
        assert Stage.CONSULTING in etapas
        assert Stage.COMPLETED in etapas
        assert Stage.REPLYING in etapas

    def test_monitor_sobrevive_a_recarregar_a_pagina(self, sistema):
        manager, _wa, _sim, db, hub = sistema
        manager._handle_message(_mensagem(0))
        assert _aguardar(lambda: db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE status=?", (Status.COMPLETED,)) == 1)

        # Um cliente novo (F5) monta a tela a partir do banco, nao da memoria
        recentes = hub.recent(limit=100)
        assert len(recentes) >= 6
        assert any(e["type"] == "message_received" for e in recentes)
        assert any(e["type"] == "job_done" for e in recentes)
        assert recentes == sorted(recentes, key=lambda e: e["id"])


class TestFilaEPosicao:
    def test_posicao_reflete_a_ordem_de_chegada(self, tmp_path):
        config = _config(tmp_path)
        db = Database(config.db_path)
        hub = EventHub(db)
        queue = QueueService(db=db, hub=hub, simulators=[], max_attempts=1, job_timeout=10.0)

        for i in range(3):
            db.insert("simulations", {
                "request_id": f"REQ{i:06d}", "consultant_name": CONSULTORES[i],
                "cpf": CPFS[i], "bank": "Santander", "contract": str(i),
                "status": Status.QUEUED, "stage": Stage.QUEUED,
                "attempts": 0, "max_attempts": 1,
                "created_at": f"2026-08-27T10:0{i}:00Z",
                "updated_at": f"2026-08-27T10:0{i}:00Z",
            })

        assert queue.depth() == 3
        assert queue.position_of("REQ000000") == 1
        assert queue.position_of("REQ000002") == 3

        itens = queue.snapshot()
        assert [i["position"] for i in itens] == [1, 2, 3]
        assert [i["consultant_name"] for i in itens] == CONSULTORES[:3]

    def test_em_processamento_aparece_no_topo(self, tmp_path):
        config = _config(tmp_path)
        db = Database(config.db_path)
        hub = EventHub(db)
        queue = QueueService(db=db, hub=hub, simulators=[], max_attempts=1, job_timeout=10.0)

        db.insert("simulations", {
            "request_id": "REQ_FILA", "consultant_name": "Ana", "cpf": CPFS[3],
            "bank": "Santander", "contract": "1", "status": Status.QUEUED, "stage": Stage.QUEUED,
            "attempts": 0, "max_attempts": 1,
            "created_at": "2026-08-27T09:00:00Z", "updated_at": "2026-08-27T09:00:00Z",
        })
        db.insert("simulations", {
            "request_id": "REQ_PROC", "consultant_name": "Ryan", "cpf": CPFS[0],
            "bank": "Santander", "contract": "2",
            "status": Status.PROCESSING, "stage": Stage.CONSULTING,
            "attempts": 1, "max_attempts": 1,
            "created_at": "2026-08-27T10:00:00Z", "updated_at": "2026-08-27T10:00:00Z",
        })

        itens = queue.snapshot()
        assert itens[0]["request_id"] == "REQ_PROC"
        assert itens[0]["stage_label"] == "Consultando sistema"


# ================================ falha de ambiente não gasta o pedido
class _SimuladorSemNavegador:
    """O navegador do simulador não abre — nada a ver com o pedido.

    20/09/2026: o perfil do Brave ficou preso e o Playwright estourou
    `launch_persistent_context: Timeout 180000ms exceeded`. Como isso contava
    como tentativa, DUAS falhas de ambiente matavam a solicitação: o consultor
    recebia "não consegui simular" por um Brave aberto na máquina do operador.
    """

    name = "sem-navegador"

    def __init__(self, falhas: int = 99):
        self.falhas = falhas
        self.tentativas: list[int] = []

    def execute(self, job, on_stage=None, timeout=None):
        self.tentativas.append(job.attempt)
        if len(self.tentativas) <= self.falhas:
            return SimulationResult(
                job=job, ok=False, status="Erro",
                error="falha ao abrir o navegador do simulador: Timeout",
                retryable=True, ambiental=True)
        return SimulationResult(job=job, ok=True, status="Não", margin="R$ 0,00")

    def start(self): pass
    def stop(self, timeout=None): pass
    def warm_up(self): pass


class TestFalhaDeAmbienteNaoGastaTentativa:
    def _fila(self, db, hub, simulador, ambiente_rapido):
        from app import jobs as modulo_jobs
        ambiente_rapido(modulo_jobs)
        fila = QueueService(db=db, hub=hub, simulators=[simulador], max_attempts=2,
                            job_timeout=10.0)
        fila.start()
        return fila

    @pytest.fixture()
    def ambiente_rapido(self, monkeypatch):
        def aplicar(modulo_jobs):
            monkeypatch.setattr(modulo_jobs, "ESPERA_DE_AMBIENTE_SEGUNDOS", 0.05)
        return aplicar

    def _job(self, db):
        agora = "2026-09-20T10:00:00Z"
        sim_id = db.insert("simulations", {
            "request_id": "REQ000100", "consultant_name": "Ryan", "chat_id": "g@g.us",
            "cpf": CPFS[0], "bank": "Santander", "contract": "1",
            "status": Status.QUEUED, "stage": Status.QUEUED, "attempts": 0,
            "max_attempts": 2, "created_at": agora, "updated_at": agora,
        })
        return SimulationJob(
            request=ParsedRequest(consultant_name="Ryan", cpf=CPFS[0],
                                  bank="Santander", contract="1"),
            message=_mensagem(0), request_id="REQ000100", simulation_id=sim_id)

    def test_volta_para_a_fila_sem_consumir_tentativa(self, tmp_path, ambiente_rapido):
        config = _config(tmp_path)
        db = Database(config.db_path)
        hub = EventHub(db)
        simulador = _SimuladorSemNavegador(falhas=3)
        fila = self._fila(db, hub, simulador, ambiente_rapido)
        try:
            fila.submit(self._job(db))
            fim = time.monotonic() + 15
            while time.monotonic() < fim and len(simulador.tentativas) < 4:
                time.sleep(0.05)
        finally:
            fila.stop()

        assert len(simulador.tentativas) >= 4, "desistiu depois de duas falhas de ambiente"
        assert set(simulador.tentativas) == {1}, (
            f"a falha de ambiente gastou tentativa do consultor: {simulador.tentativas}")

    def test_o_teto_existe_para_nao_virar_laco(self, tmp_path, ambiente_rapido):
        from app.jobs import MAX_FALHAS_DE_AMBIENTE

        config = _config(tmp_path)
        db = Database(config.db_path)
        hub = EventHub(db)
        simulador = _SimuladorSemNavegador(falhas=99)
        fila = self._fila(db, hub, simulador, ambiente_rapido)
        try:
            fim = time.monotonic() + 20
            fila.submit(self._job(db))
            while time.monotonic() < fim:
                linha = db.fetchone("SELECT status FROM simulations WHERE request_id='REQ000100'")
                if linha and linha["status"] in (Status.ERROR, Status.INTERRUPTED):
                    break
                time.sleep(0.1)
        finally:
            fila.stop()

        assert len(simulador.tentativas) <= MAX_FALHAS_DE_AMBIENTE + 2, (
            f"ficou repetindo sem fim: {len(simulador.tentativas)} execuções")

    def test_o_log_diz_o_que_fazer(self, tmp_path, ambiente_rapido):
        config = _config(tmp_path)
        db = Database(config.db_path)
        hub = EventHub(db)
        avisos: list[str] = []
        simulador = _SimuladorSemNavegador(falhas=1)
        from app import jobs as modulo_jobs
        ambiente_rapido(modulo_jobs)
        fila = QueueService(db=db, hub=hub, simulators=[simulador], max_attempts=2,
                            job_timeout=10.0,
                            on_log=lambda n, s, m, **k: avisos.append(m))
        fila.start()
        try:
            fila.submit(self._job(db))
            fim = time.monotonic() + 15
            while time.monotonic() < fim and len(simulador.tentativas) < 2:
                time.sleep(0.05)
        finally:
            fila.stop()

        ambiente = [a for a in avisos if "sem" in a.lower() and "tentativa" in a.lower()]
        assert ambiente, f"nenhum aviso explicou a espera: {avisos}"
        assert any("feche" in a.lower() for a in ambiente), (
            "o aviso não diz o que o operador precisa fazer")
