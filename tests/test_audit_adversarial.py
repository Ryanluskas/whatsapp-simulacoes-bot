"""Regressões do audit adversarial: os bugs que só apareceriam às 3 da manhã.

Cada classe reproduz UM caminho concreto encontrado no código e trava a
invariante que ele quebrava. Nada aqui usa dado real nem rede.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app.actor import ActorTimeout, ThreadActor
from app.models import (Delivery, Desfecho, EnvioNaoSaiu, EnvioSemProva, IncomingMessage,
                        ParsedRequest, ResultadoEnvio, SimulationJob, SimulationResult)
from app.state_store import StateStore
from app.whatsapp import CONNECTED, WhatsAppService
from tests.test_concurrency import png_valido


# =================================================================== ator
class _AtorLento(ThreadActor):
    def __init__(self):
        super().__init__("ator-lento")

    def run(self):
        while not self.stopping:
            if not self._drain(0.05):
                return


class TestAtorNaoDeixaComandoFantasma:
    """No modo DOM, `send_image` estourava o tempo com o ator ocupado (leitura,
    reconexão) e o manager mandava o TEXTO. O comando continuava na fila e o
    ator mandava a IMAGEM minutos depois: duas respostas."""

    def test_comando_que_nem_comecou_e_cancelado(self):
        ator = _AtorLento()
        ator.start()
        liberar = threading.Event()
        executou: list[str] = []
        try:
            ator.post(lambda: liberar.wait(5))                  # ocupa a thread dona
            with pytest.raises(ActorTimeout) as erro:
                ator.call(lambda: executou.append("envio"), timeout=0.2)
            assert erro.value.iniciado is False and erro.value.sem_prova is False
            liberar.set()
            time.sleep(0.3)
            assert executou == [], "o comando abandonado rodou depois: envio fantasma"
        finally:
            liberar.set()
            ator.stop()

    def test_comando_que_ja_comecou_e_sem_prova(self):
        ator = _AtorLento()
        ator.start()
        terminou = threading.Event()
        try:
            def envio_lento():
                time.sleep(0.5)
                terminou.set()

            with pytest.raises(ActorTimeout) as erro:
                ator.call(envio_lento, timeout=0.1)
            assert erro.value.iniciado is True and erro.value.sem_prova is True
            assert terminou.wait(2), "o comando em execução continua (não há como interromper)"
        finally:
            ator.stop()


# ====================================================== DOM depois do clique
@pytest.fixture()
def servico(tmp_path):
    s = WhatsAppService(profile_dir=tmp_path / "perfil", group_name="Grupo Teste",
                        state=StateStore(tmp_path / "state.json"), headless=True)
    s._set_status(state=CONNECTED)
    s._page = SimpleNamespace(wait_for_selector=lambda *a, **k: None,
                              wait_for_timeout=lambda *a, **k: None,
                              keyboard=SimpleNamespace(insert_text=lambda *a: None,
                                                       press=lambda *a: None),
                              locator=lambda *a, **k: SimpleNamespace(
                                  last=SimpleNamespace(click=lambda **k: None)))
    s._log = lambda *a, **k: None
    s._garantir_conversa = lambda *a: True
    s._garantir_composer = lambda *a: True
    s._citar = lambda *a: True
    s._anexar_imagem = lambda *a: (True, "colar", {})
    s._digitar_legenda = lambda *a: True
    s._lembrar_do_que_enviamos = lambda *a: None
    return s


class TestDomFalhaDepoisDoDisparo:
    """Falha depois do clique em "enviar" não prova que nada saiu."""

    def _imagem(self, servico, tmp_path):
        caminho = tmp_path / "REQ000001.png"
        caminho.write_bytes(png_valido())
        return servico._enviar_imagem("g@g.us", "Grupo Teste", str(caminho),
                                      "legenda _REQ000001_", "3EB0PEDIDO")

    def test_imagem_excecao_depois_do_clique_e_sem_prova(self, servico, tmp_path, monkeypatch):
        def quebra(*_a, **_k):
            raise RuntimeError("Target page, context or browser has been closed")
        monkeypatch.setattr(servico, "_disparar_e_confirmar_imagem", quebra)
        monkeypatch.setattr(servico, "_ja_esta_no_chat", lambda *_: False)
        with pytest.raises(EnvioSemProva):
            self._imagem(servico, tmp_path)

    def test_imagem_excecao_mas_legenda_no_chat_e_entregue(self, servico, tmp_path, monkeypatch):
        def quebra(*_a, **_k):
            raise RuntimeError("detached")
        monkeypatch.setattr(servico, "_disparar_e_confirmar_imagem", quebra)
        monkeypatch.setattr(servico, "_ja_esta_no_chat", lambda *_: True)
        r = self._imagem(servico, tmp_path)
        assert r.ok and r.tipo_midia == "imagem"

    def test_preview_aberto_prova_que_nao_saiu(self, servico, tmp_path, monkeypatch):
        def aberto(*_a, **_k):
            raise EnvioNaoSaiu("a pré-visualização da imagem não fechou")
        monkeypatch.setattr(servico, "_disparar_e_confirmar_imagem", aberto)
        with pytest.raises(EnvioNaoSaiu):
            self._imagem(servico, tmp_path)

    def test_timeout_de_confirmacao_com_legenda_no_chat_devolve_resultado(self, servico, monkeypatch):
        """Antes devolvia `True` (bool), perdendo quote_status e a via do envio."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        def nunca_fecha(*_a, **_k):
            raise PlaywrightTimeout("send ainda na tela")
        servico._page.wait_for_selector = nunca_fecha
        monkeypatch.setattr(servico, "_disparar_envio", lambda: None)
        monkeypatch.setattr(servico, "_ja_esta_no_chat", lambda *_: True)
        esperado = ResultadoEnvio(ok=True, tipo_midia="imagem", provider="dom",
                                  quote_status="ok", quoted_ok=True)
        r = servico._disparar_e_confirmar_imagem("x.png", "legenda", esperado)
        assert r is esperado

    def test_texto_enter_quebra_e_sem_prova(self, servico, monkeypatch):
        def quebra(*_a):
            raise RuntimeError("page crashed")
        servico._page.keyboard.press = quebra
        monkeypatch.setattr(servico, "_ja_esta_no_chat", lambda *_: False)
        with pytest.raises(EnvioSemProva):
            servico._do_send("g@g.us", "Grupo Teste", "resposta _REQ000001_", "3EB0PEDIDO")


# ============================================ manager: nada por cima da dúvida
class _WhatsappQueFalha:
    def __init__(self, erro_da_imagem=None, erro_do_texto=None):
        self.erro_da_imagem = erro_da_imagem
        self.erro_do_texto = erro_do_texto
        self.imagens = 0
        self.textos = 0

    def render_png(self, html, path, width=900, timeout=60.0):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(png_valido())
        return str(path)

    def send_image(self, **_k):
        self.imagens += 1
        if self.erro_da_imagem:
            raise self.erro_da_imagem
        return ResultadoEnvio(ok=True, tipo_midia="imagem", evidencia={"key_id": "BAE5IMG"})

    def send(self, **_k):
        self.textos += 1
        if self.erro_do_texto:
            raise self.erro_do_texto
        return ResultadoEnvio(ok=True, via="texto", evidencia={"key_id": "BAE5TXT"})


def _manager(tmp_path, whatsapp, imagem=True):
    from app.db import Database
    from app.events import EventHub
    from app.manager import BotManager
    from tests.test_concurrency import _config

    config = _config(tmp_path, send_image=imagem, imagem_da_resposta="card")
    db = Database(config.db_path)
    manager = BotManager(config, db, EventHub(db))
    manager.comprovantes_dir = tmp_path / "comprovantes"
    manager.whatsapp = whatsapp
    return manager


def _resultado():
    msg = IncomingMessage(message_id="3EB0PEDIDO", chat_id="g@g.us", chat_name="Grupo Teste",
                          sender_id="5562900000001@s.whatsapp.net", sender_name="Consultor",
                          text="Cliente Teste\n52998224725")
    pedido = ParsedRequest(consultant_name="Consultor", cpf="52998224725", bank="Santander",
                           contract="", customer_name="Cliente Teste")
    job = SimulationJob(request_id="REQ000001", request=pedido, message=msg, simulation_id=1)
    return SimulationResult(job=job, ok=True, status="Sim", reduction_value=1000.0)


class TestManagerNaoMandaTextoPorCimaDaDuvida:
    @pytest.mark.parametrize("erro", [
        ActorTimeout("send_image em execução", iniciado=True),
        EnvioSemProva("falha depois do clique"),
    ])
    def test_imagem_talvez_enviada_nao_cai_para_texto(self, tmp_path, erro):
        wa = _WhatsappQueFalha(erro_da_imagem=erro)
        entrega = _manager(tmp_path, wa)._deliver_result(_resultado())
        assert wa.textos == 0, "texto por cima de uma imagem que pode ter saído"
        assert entrega.status == Delivery.UNCONFIRMED

    @pytest.mark.parametrize("erro", [
        ActorTimeout("send_image nem começou", iniciado=False),
        EnvioNaoSaiu("pré-visualização aberta"),
        RuntimeError("não consegui abrir a conversa"),
    ])
    def test_imagem_que_provadamente_nao_saiu_cai_para_texto(self, tmp_path, erro):
        wa = _WhatsappQueFalha(erro_da_imagem=erro)
        entrega = _manager(tmp_path, wa)._deliver_result(_resultado())
        assert wa.textos == 1 and entrega.status == Delivery.DELIVERED

    def test_texto_talvez_enviado_fica_incerto_e_nao_reenvia(self, tmp_path):
        wa = _WhatsappQueFalha(erro_do_texto=ActorTimeout("send em execução", iniciado=True))
        entrega = _manager(tmp_path, wa, imagem=False)._deliver_result(_resultado())
        assert entrega.status == Delivery.UNCONFIRMED, "incerteza virou reenvio automático"

    def test_texto_que_nem_comecou_vai_para_o_reenvio(self, tmp_path):
        wa = _WhatsappQueFalha(erro_do_texto=ActorTimeout("send na fila", iniciado=False))
        entrega = _manager(tmp_path, wa, imagem=False)._deliver_result(_resultado())
        assert entrega.status == Delivery.RETRYING


class _WhatsappIncerto:
    """A API devolveu um id E disse que nao da' para provar a entrega.

    E' o caso real do `status: ERROR` da Evolution: a mensagem foi criada na
    instancia (existe id), mas o WhatsApp nao a aceitou. Pode ter chegado.
    """

    def __init__(self, key_id="BAE5INCERTA"):
        self.key_id = key_id
        self.textos = 0

    def render_png(self, html, path, width=900, timeout=60.0):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(png_valido())
        return str(path)

    def send(self, **_k):
        self.textos += 1
        return ResultadoEnvio(
            ok=False, sem_prova=True, desfecho=Desfecho.INCERTA, provider="evolution",
            motivo=f"a Evolution devolveu o id {self.key_id} com status ERROR",
            evidencia={"key_id": self.key_id, "sem_prova": True,
                       "desfecho": Desfecho.INCERTA})


def _linha_da_simulacao(manager):
    """A solicitacao que o `_resultado()` representa, ja' esperando entrega."""
    agora = "2026-09-18T12:00:00Z"
    manager.db.insert("simulations", {
        "id": 1, "request_id": "REQ000001", "consultant_name": "Consultor",
        "chat_id": "g@g.us", "source_message_id": "3EB0PEDIDO",
        "status": "processing", "stage": "replying", "result_ok": 1,
        "delivery_status": Delivery.PENDING, "created_at": agora, "updated_at": agora})


class TestEntregaIncertaComIdConhecido:
    """Uma entrega incerta COM id nao e' a mesma coisa que uma sem id.

    Com o id gravado, o ACK que chegar depois acha a linha e fecha a entrega
    sozinho. Sem ele, alguem tem de olhar o grupo -- para uma mensagem que a
    propria API ja' identificou.
    """

    def test_o_id_da_mensagem_incerta_fica_gravado(self, tmp_path):
        wa = _WhatsappIncerto()
        manager = _manager(tmp_path, wa, imagem=False)
        _linha_da_simulacao(manager)
        entrega = manager._deliver_result(_resultado())

        assert entrega.status == Delivery.UNCONFIRMED
        saida = manager.db.fetchone(
            "SELECT wa_message_id, status FROM messages WHERE direction='out'")
        assert saida["status"] == Delivery.UNCONFIRMED
        assert saida["wa_message_id"] == "BAE5INCERTA", (
            "sem o id gravado, o ACK do webhook nunca acha esta mensagem")
        sim = manager.db.fetchone("SELECT sent_message_id FROM simulations WHERE id=1")
        assert sim["sent_message_id"] == "BAE5INCERTA"

    def test_ack_que_chegou_antes_da_resposta_do_post_fecha_a_entrega(self, tmp_path):
        """O webhook e' outra conexao: o ACK pode chegar antes do POST voltar."""
        wa = _WhatsappIncerto()
        manager = _manager(tmp_path, wa, imagem=False)
        _linha_da_simulacao(manager)
        manager.db.insert("evolution_acks", {
            "message_id": "BAE5INCERTA", "status": "DELIVERY_ACK", "rank": 2,
            "chat_id": "g@g.us", "created_at": "2026-09-18T12:00:00Z"})

        entrega = manager._deliver_result(_resultado())

        assert entrega.status == Delivery.DELIVERED, (
            "o ACK ja' provava a entrega; a solicitacao ficou esperando decisao manual")
        sim = manager.db.fetchone(
            "SELECT delivery_status, stage, sent_message_id FROM simulations WHERE id=1")
        assert sim["delivery_status"] == Delivery.DELIVERED
        assert sim["stage"] == "completed"
        assert sim["sent_message_id"] == "BAE5INCERTA"
        assert manager.db.fetchall(
            "SELECT id FROM logs WHERE message LIKE '%ACK%'"),             "promover sem deixar log esconde de onde veio a confirmacao"

    def test_ack_de_outro_chat_nao_fecha_a_entrega(self, tmp_path):
        wa = _WhatsappIncerto()
        manager = _manager(tmp_path, wa, imagem=False)
        _linha_da_simulacao(manager)
        manager.db.insert("evolution_acks", {
            "message_id": "BAE5INCERTA", "status": "DELIVERY_ACK", "rank": 2,
            "chat_id": "outro@g.us", "created_at": "2026-09-18T12:00:00Z"})

        entrega = manager._deliver_result(_resultado())
        assert entrega.status == Delivery.UNCONFIRMED


# ========================================================== um banco, um processo
class TestUmProcessoPorBanco:
    """Dois checkouts (ou duas portas) com o mesmo DB_PATH subiam juntos: a
    trava era por pasta de perfil, guardada DENTRO do checkout. O `recover()`
    do segundo roda antes do servidor e reclassificava as entregas do primeiro."""

    def _env(self, tmp_path, banco, perfil) -> str:
        valores = {
            "WEB_HOST": "127.0.0.1", "WEB_PORT": "8898",
            "DASHBOARD_PASSWORD": "senha-de-teste-bem-forte",
            "SESSION_SECRET": "segredo-de-sessao-de-teste-bem-longo",
            "SIMULATOR_MODE": "remote", "AGENT_TOKEN": "token-de-agente-bem-longo",
            "SIM_BOT_PATH": str(tmp_path / "sem-bot"), "DB_PATH": str(banco),
            "STATE_PATH": str(tmp_path / "s.json"), "WHATSAPP_PROFILE_DIR": str(perfil),
        }
        caminho = tmp_path / f"{perfil.name}.env"
        caminho.write_text("\n".join(f"{k}={v}" for k, v in valores.items()), encoding="utf-8")
        return str(caminho)

    def test_segundo_processo_no_mesmo_banco_nao_sobe(self, tmp_path, monkeypatch):
        import main as entrada
        from app.instancia import TravaDeInstancia

        for chave in ("WEB_HOST", "DASHBOARD_PASSWORD", "SIMULATOR_MODE", "SESSION_SECRET",
                      "AGENT_TOKEN", "DB_PATH", "WHATSAPP_PROFILE_DIR", "WHATSAPP_MODE"):
            monkeypatch.delenv(chave, raising=False)
        banco = (tmp_path / "compartilhado.db").resolve()
        comecou: list[int] = []
        monkeypatch.setattr(entrada.BotManager, "start", lambda self: comecou.append(1))
        monkeypatch.setattr(entrada, "servir", lambda *a, **k: 0)

        primeiro = TravaDeInstancia(banco, rotulo="outro checkout", pasta=banco.parent)
        primeiro.adquirir()
        try:
            env = self._env(tmp_path, banco, tmp_path / "perfil-de-outro-checkout")
            assert entrada.main(["--env", env]) == 1
            assert comecou == [], "o recover do segundo processo rodou sobre o banco do primeiro"
        finally:
            primeiro.liberar()
        assert entrada.main(["--env", env]) == 0 and comecou == [1]


# ================================================= leitura DOM não perde pedido
_GRUPO_JID = "120363000000000001@g.us"


def _data_id(mid: str) -> str:
    return f"false_{_GRUPO_JID}_{mid}_5562900000001@c.us"


class TestLeituraDomNaoPerdePedido:
    """Antes: marca como vista no state.json e só então põe na fila em RAM.
    Uma queda com o pedido na fila (o ator ocupado enviando) o perdia."""

    def _servico(self, tmp_path, recebe):
        from app.whatsapp import CHAT_INFO_JS, ESTADO_DA_TELA_JS, READ_MESSAGES_JS

        s = WhatsAppService(profile_dir=tmp_path / "perfil", group_name="Grupo Teste",
                            state=StateStore(tmp_path / "state.json"), headless=True,
                            on_incoming=recebe)
        s._log = lambda *a, **k: None
        s._conferir_nome_proprio = lambda: None
        s.linhas = [{"id": _data_id("3EB0ANTIGA"), "text": "conversa antiga", "meta": ""}]

        def evaluate(script, *_args):
            if script is READ_MESSAGES_JS:
                return list(s.linhas)
            if script is CHAT_INFO_JS:
                return {"titulos": ["Grupo Teste"], "jid": _GRUPO_JID, "temMain": True}
            if script is ESTADO_DA_TELA_JS:
                # O leitor so' roda com a conversa certa PROVADA na tela.
                return {"url": "https://web.whatsapp.com/", "search_visible": True,
                        "search_value": "", "target_in_list": True,
                        "header_visible": True, "header_titles": ["Grupo Teste"],
                        "main_visible": True, "message_rows": len(s.linhas)}
            return None

        s._page = SimpleNamespace(evaluate=evaluate)
        s._poll_messages()          # primeira leitura: só a linha de base
        return s

    def test_grava_antes_de_marcar_como_vista(self, tmp_path):
        vistas_na_hora: list[bool] = []
        s = self._servico(tmp_path, lambda m: vistas_na_hora.append(s.state.has_seen(m.message_id)))
        s.linhas.append({"id": _data_id("3EB0NOVA"), "text": "Cliente Teste\n52998224725", "meta": ""})
        s._poll_messages()
        assert vistas_na_hora == [False], "marcou como vista antes de gravar"
        assert s.state.has_seen(_data_id("3EB0NOVA"))

    def test_falha_ao_gravar_nao_marca_e_tenta_de_novo(self, tmp_path):
        import sqlite3

        tentativas: list[str] = []

        def recebe(mensagem):
            tentativas.append(mensagem.message_id)
            if len(tentativas) == 1:
                raise sqlite3.OperationalError("database is locked")

        s = self._servico(tmp_path, recebe)
        nova = _data_id("3EB0TRAVADA")
        s.linhas.append({"id": nova, "text": "Cliente Teste\n52998224725", "meta": ""})
        s._poll_messages()
        assert not s.state.has_seen(nova), "perdida: marcada como vista sem estar gravada"
        s._poll_messages()
        assert tentativas == [nova, nova] and s.state.has_seen(nova)

    def test_queda_com_o_pedido_na_fila_e_retomado_no_boot(self, tmp_path):
        import queue as fila

        wa = SimpleNamespace(inbox=fila.Queue())
        antes = _manager(tmp_path, wa, imagem=False)
        pedido = IncomingMessage(message_id=_data_id("3EB0NAFILA"), chat_id=_GRUPO_JID,
                                 chat_name="Grupo Teste", sender_id="5562900000001@c.us",
                                 sender_name="Consultor", text="Cliente Teste\n52998224725")
        antes._receber_do_navegador(pedido)
        assert wa.inbox.qsize() == 1
        # "Queda": a fila em RAM some. Um processo novo no mesmo banco retoma.
        wa_depois = SimpleNamespace(inbox=fila.Queue())
        depois = _manager(tmp_path, wa_depois, imagem=False)
        assert depois._retomar_entradas() == 1
        assert wa_depois.inbox.get_nowait().message_id == pedido.message_id


# ============================================================= vigia da fila
class TestVigiaNaoReavaliaEntregaQueAcabouDeTerminar:
    def test_job_que_termina_durante_a_consulta_nao_e_tomado_por_preso(self, tmp_path):
        from app.clock import iso_atras
        from app.db import Database
        from app.events import EventHub
        from app.jobs import QueueService
        from app.models import Stage, Status

        db = Database(tmp_path / "v.db")
        q = QueueService(db, EventHub(db), simulators=[], job_timeout=1.0)
        db.insert("simulations", {
            "request_id": "REQ000321", "status": Status.PROCESSING, "stage": Stage.REPLYING,
            "result_ok": 1, "delivery_status": Delivery.PENDING,
            "started_at": iso_atras(3600), "created_at": iso_atras(3600),
            "updated_at": iso_atras(3600)})
        q._active["REQ000321"] = {"stage": Stage.REPLYING}

        consulta = db.fetchall

        def termina_no_meio(*a, **k):
            linhas = consulta(*a, **k)          # a linha ainda veio `processing`...
            q._active.pop("REQ000321", None)     # ...e o job terminou logo depois
            return linhas

        db.fetchall = termina_no_meio
        assert q._resolver_presas() == 0, "reavaliou uma entrega que estava em curso na foto"


# ======================================================= timers de retentativa
class TestTimerDeRetentativaNaoAcumula:
    def test_timer_sai_do_conjunto_ao_disparar(self, tmp_path, monkeypatch):
        from app import jobs
        from app.db import Database
        from app.events import EventHub

        monkeypatch.setattr(jobs, "RETRY_BACKOFF_SECONDS", 0.05)
        db = Database(tmp_path / "t.db")
        q = jobs.QueueService(db, EventHub(db), simulators=[])
        resultado = _resultado()
        q._retry(resultado.job, SimulationResult(job=resultado.job, ok=False, error="x",
                                                 retryable=True), 0.1)
        assert q._pending.get(timeout=2).attempt == 2
        time.sleep(0.05)
        assert not q._timers, "o Timer disparado ficou guardado (vazamento por retentativa)"


# ====================================================== reenvio órfão em pending
class TestReenvioOrfaoNaoFicaPresoAteReiniciar:
    def _linha(self, manager):
        from app.clock import iso_atras
        from app.models import Stage, Status

        return manager.db.insert("simulations", {
            "request_id": "REQ000654", "status": Status.COMPLETED, "stage": Stage.COMPLETED,
            "result_ok": 1, "delivery_status": Delivery.PENDING, "chat_id": _GRUPO_JID,
            "source_message_id": "3EB0ORFAO", "consultant_name": "Consultor",
            "cpf": "52998224725", "customer_name": "Cliente Teste",
            "created_at": iso_atras(3600), "updated_at": iso_atras(3600)})

    def test_sem_envio_gravado_volta_para_o_reenvio(self, tmp_path):
        wa = _WhatsappQueFalha()
        manager = _manager(tmp_path, wa, imagem=False)
        sid = self._linha(manager)
        manager._reenviar_pendentes()
        estado = manager.db.fetchone("SELECT delivery_status FROM simulations WHERE id=?", (sid,))
        assert estado["delivery_status"] == Delivery.RETRYING

    def test_com_envio_em_curso_fica_incerta(self, tmp_path):
        from app.clock import iso_atras

        wa = _WhatsappQueFalha()
        manager = _manager(tmp_path, wa, imagem=False)
        sid = self._linha(manager)
        manager.db.insert("messages", {"simulation_id": sid, "request_id": "REQ000654",
                                       "direction": "out", "status": "sending",
                                       "created_at": iso_atras(3600)})
        manager._reenviar_pendentes()
        estado = manager.db.fetchone("SELECT delivery_status FROM simulations WHERE id=?", (sid,))
        assert estado["delivery_status"] == Delivery.UNCONFIRMED and wa.textos == 0


# ============================================================= log sem telefone
class TestLogDeEnvioSemTelefoneInteiro:
    def test_participante_mascarado(self, tmp_path):
        wa = _WhatsappQueFalha()
        manager = _manager(tmp_path, wa, imagem=False)
        mensagem = IncomingMessage(message_id="3EB0LOG", chat_id=_GRUPO_JID, chat_name="Grupo Teste",
                                   sender_id="5562900000777@s.whatsapp.net", sender_name="Consultor",
                                   text="x", participant="5562900000777@s.whatsapp.net")
        manager._send_reply(mensagem, "resposta", status="completed", request_id="REQ000777")
        linhas = [l["message"] for l in manager.db.fetchall(
            "SELECT message FROM logs WHERE message LIKE '%quote_participant=%'")]
        assert linhas and all("5562900000777" not in l for l in linhas), linhas
        assert any("5562*****0777@s.whatsapp.net" in l for l in linhas)


# ================================================ Evolution: falso "entregue"
class TestEvolutionStatusErrorNaoEEntregue:
    """2xx com key.id e ``status: "ERROR"``: a instância criou a mensagem, o
    WhatsApp não a aceitou. Contar como entregue deixava o consultor sem
    resposta e o painel dizendo "Entregue"."""

    def _cliente(self, corpo):
        import httpx

        from app.evolution import EvolutionClient

        return EvolutionClient(
            "http://e:8080", "k", "allana", _GRUPO_JID, renderer=object(),
            client=httpx.Client(transport=httpx.MockTransport(
                lambda r: httpx.Response(201, json=corpo)), base_url="http://e:8080"))

    def test_status_error_e_incerta(self):
        r = self._cliente({"key": {"id": "BAE5ERRO"}, "status": "ERROR"}).send(
            _GRUPO_JID, "g", "resposta", quote_message_id="3EB0PEDIDO")
        assert not r.ok and r.sem_prova is True and r.transitorio is False
        assert r.quoted_ok is False

    def test_status_pending_continua_entregue(self):
        r = self._cliente({"key": {"id": "BAE5OK"}, "status": "PENDING"}).send(
            _GRUPO_JID, "g", "resposta")
        assert r.ok and r.enviado_id == "BAE5OK"



# ================================================ autoria: a segunda camada
class TestSegundaCamadaDeAutoria:
    """Uma mensagem NOSSA nunca pode virar solicitacao, nem quando o filtro
    da tela falha.

    Em 20/09/2026 ele falhou: `BOT_SELF_NAME` dizia "Operacional Capital", a
    conta se chamava "Operacional" no grupo, e a comparacao de nome
    respondia sozinha -- desligando as outras duas provas. Tres solicitacoes
    nasceram do que o proprio bot tinha enviado. Todas com id "3EB0".
    """

    def _servico(self, tmp_path, linhas, recebe=None):
        from app.whatsapp import CHAT_INFO_JS, ESTADO_DA_TELA_JS, READ_MESSAGES_JS

        avisos: list[str] = []
        s = WhatsAppService(profile_dir=tmp_path / "perfil", group_name="Grupo Teste",
                            state=StateStore(tmp_path / "state.json"), headless=True,
                            on_incoming=recebe or (lambda m: None))
        s._log = lambda nivel, msg, *a, **k: avisos.append(f"{nivel}: {msg}")
        s._conferir_nome_proprio = lambda: None
        s.linhas = linhas

        def evaluate(script, *_args):
            if script is READ_MESSAGES_JS:
                return list(s.linhas)
            if script is CHAT_INFO_JS:
                return {"titulos": ["Grupo Teste"], "jid": _GRUPO_JID, "temMain": True}
            if script is ESTADO_DA_TELA_JS:
                return {"url": "https://web.whatsapp.com/", "search_visible": True,
                        "search_value": "", "target_in_list": True,
                        "header_visible": True, "header_titles": ["Grupo Teste"],
                        "main_visible": True, "message_rows": len(s.linhas)}
            return None

        s._page = SimpleNamespace(evaluate=evaluate)
        s.avisos = avisos
        return s

    #: Um pedido de verdade: nome, CPF valido e orgao.
    PEDIDO = "Cliente Teste\n52998224725\nAmapá"

    def test_id_pelado_de_mensagem_nossa_nao_vira_pedido(self, tmp_path):
        recebidas: list[str] = []
        s = self._servico(tmp_path, [{"id": "3EB0PRIMEIRA", "text": "antiga", "meta": ""}],
                          recebe=lambda m: recebidas.append(m.message_id))
        s._poll_messages()      # linha de base
        s.linhas.append({"id": "3EB0C1D2E3F4", "text": self.PEDIDO,
                         "meta": "[02:04, 20/09/2026] Operacional: "})
        s._poll_messages()

        assert recebidas == [], "o bot criou solicitacao a partir da propria mensagem"
        assert s.state.has_seen("3EB0C1D2E3F4"), "sem marcar, ela voltaria no proximo ciclo"
        assert any("id de mensagem NOSSA" in a for a in s.avisos), (
            "ignorou em silencio: ninguem descobriria que a autoria da tela falhou")

    def test_a_mensagem_do_consultor_continua_passando(self, tmp_path):
        recebidas: list[str] = []
        s = self._servico(tmp_path, [{"id": _data_id("2AF4ANTIGA"), "text": "oi", "meta": ""}],
                          recebe=lambda m: recebidas.append(m.message_id))
        s._poll_messages()
        nova = _data_id("2AF4NOVA")
        s.linhas.append({"id": nova, "text": self.PEDIDO,
                         "meta": "[02:05, 20/09/2026] Ryan: "})
        s._poll_messages()
        assert recebidas == [nova]


class TestIdDeMensagemNossa:
    """A regra, isolada. `false_` e `true_` vem do formato classico; o id
    pelado "3EB0" e' o que esta instalacao serve."""

    @pytest.mark.parametrize("data_id,nossa", [
        ("3EB0566994AE2D2798E44D", True),
        ("album-3EB0AA-3EB0BB", True),
        ("true_5562000@g.us_BBB", True),
        # `false_` manda no formato classico: o "3EB0" interno nao decide nada.
        ("false_120363@g.us_3EB0AAAA_5562111@c.us", False),
        ("2AF4AAAABBBBCCCC", False),
        ("ACC81234567890", False),
        ("", False),
    ])
    def test_tabela(self, data_id, nossa):
        from app.whatsapp import id_de_mensagem_nossa
        assert id_de_mensagem_nossa(data_id) is nossa
