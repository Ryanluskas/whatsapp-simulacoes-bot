"""Simulador remoto: painel no container, automação no Windows.

O contrato a garantir é que NADA muda para o resto do sistema. A fila, as
tentativas, o isolamento por ``request_id`` e os eventos do monitor continuam
funcionando igual — só quem executa a simulação mudou de lugar.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.events import EventHub
from app.jobs import QueueService
from app.manager import BotManager
from app.models import Stage, Status
from app.remote import RemoteSimulator
from app.web import create_app
from tests.test_concurrency import (
    CONSULTORES, CPFS, FakeWhatsApp, _aguardar, _config, _mensagem,
)

TOKEN = "token-de-teste-do-agente"


def _config_remoto(tmp_path, **kwargs):
    return _config(tmp_path, simulator_mode="remote", agent_token=TOKEN, **kwargs)


@pytest.fixture()
def sistema_remoto(tmp_path):
    config = _config_remoto(tmp_path)
    db = Database(config.db_path)
    hub = EventHub(db)
    manager = BotManager(config, db, hub)
    whatsapp = FakeWhatsApp()
    manager.whatsapp = whatsapp

    remoto = manager.simulators[0]
    manager.queue = QueueService(
        db=db, hub=hub, simulators=[remoto], max_attempts=config.max_attempts,
        job_timeout=8.0, on_result=manager._deliver_result, on_log=manager._queue_log,
    )
    manager.queue.start()

    app = create_app(config, db, hub, manager)
    with TestClient(app) as client:
        try:
            yield client, manager, whatsapp, db, remoto
        finally:
            manager.queue.stop()


AGENTE = {"X-Agent-Token": TOKEN, "X-Agent-Name": "pc-do-ryan"}


def _reivindicar(client, timeout: float = 20.0) -> dict | None:
    """Pede trabalho até receber. Devolve a tarefa da PRIMEIRA resposta 200.

    Importante não usar `_aguardar` com um claim dentro da condição: o próprio
    teste consumiria o job e a asserção seguinte pegaria 204.
    """
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        resposta = client.post("/api/agent/claim", headers=AGENTE)
        if resposta.status_code == 200:
            return resposta.json()
        time.sleep(0.2)
    return None


class TestModoRemoto:
    def test_manager_usa_o_simulador_remoto(self, sistema_remoto):
        _client, manager, _wa, _db, remoto = sistema_remoto
        assert isinstance(remoto, RemoteSimulator)
        assert manager.system_status_payload()["simulator_mode"] == "remote"

    def test_ciclo_completo_com_o_agente(self, sistema_remoto):
        client, manager, whatsapp, db, _remoto = sistema_remoto
        manager._handle_message(_mensagem(0))

        tarefa = _reivindicar(client)
        assert tarefa is not None, "o agente não recebeu o trabalho"
        request_id = tarefa["request_id"]

        # Reporta progresso e devolve o resultado
        assert client.post("/api/agent/stage", headers=AGENTE, json={
            "request_id": request_id, "stage": Stage.CONSULTING}).json()["ok"] is True

        assert client.post("/api/agent/result", headers=AGENTE, json={
            "request_id": request_id, "ok": True, "status": "Sim",
            "reduction_value": 32027.25, "margin": "R$ 1.284,00",
            "contracts": [{"contrato": "7*****56", "valor_parcela": "R$ 450,00"}],
            "installment_sum": 450.0, "installment_count": 120, "debt_sum": 22079.91,
        }).status_code == 200

        assert _aguardar(lambda: db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE status=?", (Status.COMPLETED,)) == 1)

        linha = db.fetchone("SELECT * FROM simulations")
        assert linha["reduction_value"] == 32027.25
        assert linha["refin"] == "Sim"
        assert linha["contracts_count"] == 1

        # E o consultor recebeu a resposta pelo WhatsApp, como no modo local.
        #
        # Esperar pela RESPOSTA, não pelo status: `_finish` grava o status
        # antes de enviar, então checar `whatsapp.sent` logo após o status
        # virar `completed` é uma corrida — o teste passava ou falhava
        # conforme a máquina estivesse mais ou menos carregada.
        assert _aguardar(
            lambda: any(request_id in m["text"] for m in whatsapp.sent)), (
            f"resposta não chegou: {[m['text'][:40] for m in whatsapp.sent]}")

    def test_sem_agente_a_simulacao_expira_e_avisa(self, sistema_remoto):
        """Ninguém do outro lado: a fila não pode ficar presa para sempre."""
        _client, manager, whatsapp, db, _remoto = sistema_remoto
        manager._handle_message(_mensagem(1))

        assert _aguardar(lambda: db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE status=?", (Status.ERROR,)) == 1,
            timeout=40)
        linha = db.fetchone("SELECT * FROM simulations")
        assert "agente" in linha["error_message"].lower()
        # O consultor precisa saber que falhou e por quê. A frase mudou de
        # "não foi possível concluir a simulação" para algo que uma pessoa
        # diria; o que o teste garante é o CONTEÚDO, não o texto exato.
        avisos = [m["text"].lower() for m in whatsapp.sent]
        assert any("não consegui simular" in a for a in avisos), avisos
        assert any("agente" in a for a in avisos), (
            "o motivo real tem de chegar ao consultor, não só 'erro'")

    def test_tres_pedidos_saem_um_de_cada_vez_e_sem_repetir(self, sistema_remoto):
        """Com um worker, só UM job fica reivindicável por vez.

        Isso não é limitação do modo remoto: é a mesma serialização do modo
        local, porque a sessão do portal é uma só. O que se garante aqui é que
        a fila anda — e que nenhum `request_id` sai duas vezes.
        """
        client, manager, _wa, db, _remoto = sistema_remoto
        for i in range(3):
            manager._handle_message(_mensagem(i))
        assert _aguardar(lambda: db.scalar("SELECT COUNT(*) FROM simulations") == 3)

        # Enquanto um está em execução, não há outro para pegar.
        primeiro = _reivindicar(client)
        assert primeiro is not None
        assert client.post("/api/agent/claim", headers=AGENTE).status_code == 204

        vistos = [primeiro["request_id"]]
        for _ in range(2):
            client.post("/api/agent/result", headers=AGENTE, json={
                "request_id": vistos[-1], "ok": True, "status": "Não"})
            seguinte = _reivindicar(client)
            assert seguinte is not None, "a fila travou depois de concluir um job"
            vistos.append(seguinte["request_id"])

        assert len(set(vistos)) == 3, f"job entregue duas vezes: {vistos}"

    def test_tarefa_carrega_os_dados_do_cliente(self, sistema_remoto):
        """O agente recebe tudo que precisa — inclusive o órgão da 2ª linha."""
        client, manager, _wa, _db, _remoto = sistema_remoto
        manager._handle_message(_mensagem(0, texto=f"Paulo Testes\nAmapá\n{CPFS[0]}"))

        tarefa = _reivindicar(client)
        assert tarefa is not None
        assert tarefa["cpf"] == CPFS[0]
        assert tarefa["customer_name"] == "Paulo Testes"
        assert tarefa["origin"] == "Amapá"
        assert tarefa["consultant_name"] == CONSULTORES[0]
        assert tarefa["bank"] == "Santander"
        assert tarefa["attempt"] == 1
        assert tarefa["request_id"].startswith("REQ")


class TestSegurancaDoAgente:
    def test_sem_token_e_recusado(self, sistema_remoto):
        client, _m, _wa, _db, _r = sistema_remoto
        for rota in ("/api/agent/claim", "/api/agent/stage", "/api/agent/result"):
            assert client.post(rota, json={}).status_code == 401, rota

    def test_token_errado_e_recusado(self, sistema_remoto):
        client, _m, _wa, _db, _r = sistema_remoto
        assert client.post("/api/agent/claim",
                           headers={"X-Agent-Token": "chute"}).status_code == 401

    def test_sessao_do_painel_nao_serve_para_o_agente(self, sistema_remoto):
        """Público diferente, poder diferente: o cookie do painel não vale aqui."""
        client, _m, _wa, _db, _r = sistema_remoto
        client.post("/api/login", data={"password": "senha-de-teste"})
        assert client.post("/api/agent/claim").status_code == 401

    def test_resultado_de_solicitacao_desconhecida_e_recusado(self, sistema_remoto):
        client, _m, _wa, _db, _r = sistema_remoto
        resposta = client.post("/api/agent/result", headers=AGENTE, json={
            "request_id": "REQ999999", "ok": True, "status": "Sim"})
        assert resposta.status_code == 409

    def test_result_sem_request_id(self, sistema_remoto):
        client, _m, _wa, _db, _r = sistema_remoto
        assert client.post("/api/agent/result", headers=AGENTE,
                           json={"ok": True}).status_code == 400


class TestModoLocalNaoExpoeOAgente:
    def test_rotas_do_agente_recusam_quando_o_modo_e_local(self, tmp_path):
        config = _config(tmp_path)   # modo local, sem token
        db = Database(config.db_path)
        hub = EventHub(db)
        manager = BotManager(config, db, hub)
        manager.whatsapp = FakeWhatsApp()
        app = create_app(config, db, hub, manager)
        with TestClient(app) as client:
            # Sem AGENT_TOKEN configurado, a porta nem abre.
            assert client.post("/api/agent/claim",
                               headers={"X-Agent-Token": "x"}).status_code == 503


class TestUnidadeDoRemoteSimulator:
    def test_status_antes_de_qualquer_agente(self):
        remoto = RemoteSimulator()
        estado = remoto.status()
        assert estado["mode"] == "remoto"
        assert estado["ready"] is False
        assert "nenhum agente" in estado["last_error"]

    def test_stop_solta_quem_estava_esperando(self):
        from app.models import IncomingMessage, ParsedRequest, SimulationJob

        remoto = RemoteSimulator()
        job = SimulationJob(
            request=ParsedRequest("Ryan", CPFS[0], "Santander", ""),
            message=IncomingMessage("m", "c", "n", "s", "S", "t"),
            request_id="REQ000001", simulation_id=1,
        )
        resultados = []
        t = threading.Thread(target=lambda: resultados.append(remoto.execute(job, None, 30)))
        t.start()
        time.sleep(0.4)
        remoto.stop()
        t.join(timeout=5)

        assert resultados and resultados[0].ok is False
        assert resultados[0].retryable is True


class TestReivindicacaoVencida:
    """Se o agente cai no meio de um job, o trabalho tem de voltar para a fila.

    Sem isso o job ficava preso até o timeout inteiro (300s por padrão) e o
    consultor esperava minutos por um erro — mesmo com o agente de volta em
    segundos.
    """

    def _pendurar(self, remoto, request_id="REQ000001"):
        from app.models import IncomingMessage, ParsedRequest, SimulationJob

        job = SimulationJob(
            request=ParsedRequest("Ryan", CPFS[0], "Santander", ""),
            message=IncomingMessage("m", "c", "n", "s", "S", "t"),
            request_id=request_id, simulation_id=1,
        )
        t = threading.Thread(target=lambda: remoto.execute(job, None, 60), daemon=True)
        t.start()
        time.sleep(0.3)
        return job, t

    def test_claim_vencido_volta_para_a_fila(self, monkeypatch):
        import app.remote as remoto_mod

        monkeypatch.setattr(remoto_mod, "CLAIM_TTL_SECONDS", 0.5)
        remoto = RemoteSimulator()
        self._pendurar(remoto)

        primeiro = remoto.claim("agente-que-vai-morrer")
        assert primeiro is not None
        # Enquanto vale, ninguém mais pega
        assert remoto.claim("outro") is None

        time.sleep(0.7)  # a reivindicação vence
        segundo = remoto.claim("agente-novo")
        assert segundo is not None, "job ficou preso com o agente morto"
        assert segundo["request_id"] == primeiro["request_id"]
        remoto.stop()

    def test_sinal_de_vida_impede_que_roubem_o_job(self, monkeypatch):
        """Simulação lenta, mas com agente vivo, não pode ser tomada dele."""
        import app.remote as remoto_mod

        monkeypatch.setattr(remoto_mod, "CLAIM_TTL_SECONDS", 0.5)
        remoto = RemoteSimulator()
        job, _t = self._pendurar(remoto)

        assert remoto.claim("agente-trabalhando") is not None
        for _ in range(4):           # ~1,2s de trabalho, batendo a cada 0,3s
            time.sleep(0.3)
            remoto.report_stage(job.request_id, Stage.CONSULTING)
            assert remoto.claim("intruso") is None, "tomaram o job de um agente vivo"

        remoto.stop()

    def test_resultado_do_agente_certo_ainda_e_aceito(self, monkeypatch):
        import app.remote as remoto_mod

        monkeypatch.setattr(remoto_mod, "CLAIM_TTL_SECONDS", 0.5)
        remoto = RemoteSimulator()
        job, t = self._pendurar(remoto)
        remoto.claim("a1")
        time.sleep(0.7)
        remoto.claim("a2")           # a2 assume o job vencido
        assert remoto.submit_result(job.request_id, {"ok": True, "status": "Sim"}) is True
        t.join(timeout=5)
        remoto.stop()


class TestRetryNaoTravaAFila:
    def test_falha_recuperavel_nao_congela_o_despachante(self, tmp_path, monkeypatch):
        """O backoff era um time.sleep no despachante: com um worker, a fila
        inteira parava 8s a cada falha recuperável."""
        import app.jobs as jobs_mod

        monkeypatch.setattr(jobs_mod, "RETRY_BACKOFF_SECONDS", 6.0)

        from app.db import Database
        from app.events import EventHub
        from app.manager import BotManager
        from tests.test_concurrency import FakeSimulator, _config

        config = _config(tmp_path, max_attempts=2)
        db = Database(config.db_path)
        hub = EventHub(db)
        manager = BotManager(config, db, hub)
        manager.whatsapp = FakeWhatsApp()
        # A 1ª solicitação falha de forma recuperável; a 2ª é normal.
        simulator = FakeSimulator(flaky_for=["REQ000001"])
        manager.simulators = [simulator]
        manager.queue = QueueService(
            db=db, hub=hub, simulators=[simulator], max_attempts=2, job_timeout=30.0,
            on_result=manager._deliver_result, on_log=manager._queue_log,
        )
        manager.queue.start()
        try:
            manager._handle_message(_mensagem(0))   # vai falhar e reagendar
            time.sleep(0.6)
            inicio = time.monotonic()
            manager._handle_message(_mensagem(1))   # esta não pode esperar o backoff

            concluiu_a_segunda = _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE status=? AND consultant_name=?",
                (Status.COMPLETED, CONSULTORES[1])) == 1, timeout=20)
            decorrido = time.monotonic() - inicio

            assert concluiu_a_segunda, "a 2ª solicitação nunca foi processada"
            assert decorrido < 4.0, (
                f"a fila ficou travada {decorrido:.1f}s esperando o backoff da 1ª")
        finally:
            manager.queue.stop()
