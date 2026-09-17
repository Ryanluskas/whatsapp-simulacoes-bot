"""Auditoria do fluxo real, de ponta a ponta.

Estes testes não exercitam funções isoladas: sobem o manager com a fila de
verdade e verificam o que o consultor efetivamente recebe. Cobrem os quatro
defeitos que motivaram todo o trabalho:

* a resposta não ficava ancorada na mensagem original;
* a imagem podia ser de outra solicitação;
* o grupo recebia três mensagens onde uma bastava;
* o bot lia as próprias respostas e entrava em laço.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from test_concurrency import (CPFS, FakeSimulator, FakeWhatsApp, _aguardar,
                              _config, _mensagem)

from app.db import Database
from app.events import EventHub
from app.jobs import QueueService
from app.manager import BotManager
from app.models import IncomingMessage, Status


def _montar(tmp_path, whatsapp=None, simulator=None, com_imagem=False):
    config = _config(tmp_path, send_image=com_imagem)
    db = Database(config.db_path)
    hub = EventHub(db)
    manager = BotManager(config, db, hub)
    whatsapp = whatsapp or FakeWhatsApp()
    simulator = simulator or FakeSimulator()
    manager.whatsapp = whatsapp
    manager.simulators = [simulator]
    manager.queue = QueueService(
        db=db, hub=hub, simulators=[simulator],
        max_attempts=config.max_attempts, job_timeout=config.job_timeout_seconds,
        on_result=manager._deliver_result, on_log=manager._queue_log,
    )
    manager.queue.start()
    return manager, whatsapp, simulator, db


def _saidas(whatsapp) -> list[dict]:
    """Tudo que o consultor recebeu: texto e imagem."""
    return list(whatsapp.sent) + list(whatsapp.images)


def _saidas_de(whatsapp, request_id: str) -> list[dict]:
    return ([m for m in whatsapp.sent if request_id in m["text"]]
            + [i for i in whatsapp.images if request_id in i["path"]])


class TestReplyAncoradoNaMensagemCerta:
    """§1 — o defeito original: resposta solta no grupo.

    O ``IncomingMessage`` viaja DENTRO do job pela fila, então não há consulta
    ao banco no meio nem janela para trocar de mensagem. Estes testes provam
    que a âncora sobrevive à espera e à concorrência.
    """

    def test_uma_solicitacao_responde_a_propria_mensagem(self, tmp_path):
        manager, whatsapp, _sim, db = _montar(tmp_path)
        try:
            mensagem = _mensagem(0)
            manager._handle_message(mensagem)
            assert _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE status=?",
                (Status.COMPLETED,)) == 1)
            assert _aguardar(lambda: len(_saidas(whatsapp)) >= 1)

            for saida in _saidas(whatsapp):
                assert saida["quote"] == mensagem.message_id, (
                    f"resposta sem âncora: {saida['quote']!r}")
        finally:
            manager.queue.stop()

    def test_demora_na_fila_nao_perde_a_ancora(self, tmp_path):
        """O caso que motivou tudo: a simulação demora e a resposta vem solta."""
        manager, whatsapp, _sim, db = _montar(
            tmp_path, simulator=FakeSimulator(delay_range=(0.6, 0.8)))
        try:
            mensagem = _mensagem(0)
            manager._handle_message(mensagem)
            assert _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE status=?",
                (Status.COMPLETED,)) == 1, timeout=20)
            assert _aguardar(lambda: len(_saidas(whatsapp)) >= 1)

            for saida in _saidas(whatsapp):
                assert saida["quote"] == mensagem.message_id
        finally:
            manager.queue.stop()

    @pytest.mark.parametrize("quantidade", [2, 3])
    def test_cada_solicitacao_responde_a_SUA_mensagem(self, tmp_path, quantidade):
        """REQ A → reply A e REQ B → reply B, com A e B próximos no grupo."""
        manager, whatsapp, _sim, db = _montar(
            tmp_path, simulator=FakeSimulator(delay_range=(0.3, 0.5)))
        try:
            mensagens = [_mensagem(i) for i in range(quantidade)]
            threads = [threading.Thread(target=manager._handle_message, args=(m,))
                       for m in mensagens]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE status=?",
                (Status.COMPLETED,)) == quantidade, timeout=40)
            assert _aguardar(lambda: len(_saidas(whatsapp)) >= quantidade, timeout=20)

            # Para cada solicitação, a âncora tem de ser a mensagem do SEU CPF.
            for linha in db.fetchall("SELECT * FROM simulations"):
                indice = CPFS.index(linha["cpf"])
                esperado = mensagens[indice].message_id
                minhas = _saidas_de(whatsapp, linha["request_id"])
                assert minhas, f"{linha['request_id']} sem resposta"
                for saida in minhas:
                    assert saida["quote"] == esperado, (
                        f"{linha['request_id']} respondeu à mensagem de outro")
        finally:
            manager.queue.stop()


class TestImagemPertenceAoPedidoCerto:
    """§2 — nunca enviar a imagem de outra solicitação."""

    def test_o_arquivo_leva_o_request_id(self, tmp_path):
        manager, whatsapp, _sim, db = _montar(tmp_path, com_imagem=True)
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: len(whatsapp.images) == 1, timeout=20)
            linha = db.fetchone("SELECT * FROM simulations")
            assert linha["request_id"] in whatsapp.images[0]["path"]
        finally:
            manager.queue.stop()

    def test_varias_imagens_nao_se_cruzam(self, tmp_path):
        manager, whatsapp, _sim, db = _montar(
            tmp_path, simulator=FakeSimulator(delay_range=(0.2, 0.4)),
            com_imagem=True)
        try:
            for i in range(3):
                threading.Thread(target=manager._handle_message,
                                 args=(_mensagem(i),)).start()
            assert _aguardar(lambda: len(whatsapp.images) == 3, timeout=40)

            arquivos = [Path(i["path"]).stem for i in whatsapp.images]
            assert len(set(arquivos)) == 3, f"imagem repetida: {arquivos}"
            ids = {l["request_id"] for l in db.fetchall("SELECT * FROM simulations")}
            assert set(arquivos) == ids, "imagem de solicitação que não existe"
        finally:
            manager.queue.stop()

    def test_falha_da_imagem_manda_texto_sem_duplicar(self, tmp_path):
        """A queda para texto não pode virar duas respostas."""
        manager, whatsapp, _sim, db = _montar(
            tmp_path, whatsapp=FakeWhatsApp(image_fails=True), com_imagem=True)
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: len(whatsapp.sent) >= 1, timeout=20)
            time.sleep(0.6)   # deixa qualquer envio extra aparecer

            assert whatsapp.images == [], "a imagem falhou, não pode constar"
            linha = db.fetchone("SELECT * FROM simulations")
            minhas = _saidas_de(whatsapp, linha["request_id"])
            assert len(minhas) == 1, "a queda para texto duplicou a resposta"
        finally:
            manager.queue.stop()


class TestQuantasMensagensOConsultorRecebe:
    """§3 — uma resposta útil, não a narrativa do processamento."""

    def _contar(self, tmp_path, **kw) -> int:
        manager, whatsapp, _sim, db = _montar(tmp_path, **kw)
        try:
            manager._handle_message(_mensagem(0))
            _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE status IN (?,?)",
                (Status.COMPLETED, Status.ERROR)) == 1, timeout=30)
            time.sleep(0.8)
            return len(_saidas(whatsapp))
        finally:
            manager.queue.stop()

    def test_caminho_normal_manda_uma(self, tmp_path):
        assert self._contar(tmp_path) == 1

    def test_com_imagem_ligada_manda_uma(self, tmp_path):
        assert self._contar(tmp_path, com_imagem=True) == 1

    def test_imagem_falhando_manda_uma(self, tmp_path):
        assert self._contar(tmp_path, whatsapp=FakeWhatsApp(image_fails=True),
                            com_imagem=True) == 1

    def test_render_falhando_manda_uma(self, tmp_path):
        assert self._contar(tmp_path, whatsapp=FakeWhatsApp(render_fails=True),
                            com_imagem=True) == 1

    def test_erro_permanente_manda_uma(self, tmp_path):
        assert self._contar(tmp_path,
                            simulator=FakeSimulator(fail_for=(CPFS[0],))) == 1

    def test_dados_faltando_manda_uma(self, tmp_path):
        manager, whatsapp, _sim, _db = _montar(tmp_path)
        try:
            manager._handle_message(IncomingMessage(
                message_id="2A_SEMCPF", chat_id="grupo@g.us",
                chat_name="Consultores", sender_id="5567900000000@c.us",
                sender_name="Ryan", text="Cliente 12345678901"))
            time.sleep(0.5)
            assert len(_saidas(whatsapp)) == 1
            assert "CPF" in whatsapp.sent[0]["text"]
        finally:
            manager.queue.stop()

    def test_conversa_comum_nao_gera_nada(self, tmp_path):
        """O bot fica quieto quando não é pedido."""
        manager, whatsapp, _sim, _db = _montar(tmp_path)
        try:
            for texto in ("bom dia", "sem matricula", "só segunda"):
                manager._handle_message(IncomingMessage(
                    message_id=f"2A_{texto[:4]}", chat_id="grupo@g.us",
                    chat_name="Consultores", sender_id="5567900000000@c.us",
                    sender_name="Ryan", text=texto))
            time.sleep(0.4)
            assert _saidas(whatsapp) == []
        finally:
            manager.queue.stop()


class TestAntiLacoNoFluxoCompleto:
    """§5 — o bot nunca pode tratar a própria resposta como pedido."""

    def test_a_resposta_enviada_e_reconhecida_como_nossa(self, tmp_path):
        manager, whatsapp, _sim, _db = _montar(tmp_path)
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: len(_saidas(whatsapp)) >= 1, timeout=20)

            for saida in whatsapp.sent:
                assert manager._e_mensagem_nossa(saida["text"]), (
                    f"não reconheceria a própria resposta: {saida['text']!r}")
            for imagem in whatsapp.images:
                assert manager._e_mensagem_nossa(imagem["caption"]), (
                    f"legenda não reconhecida: {imagem['caption']!r}")
        finally:
            manager.queue.stop()

    def test_reler_a_propria_resposta_nao_gera_nada(self, tmp_path):
        """A prova final: alimenta o bot com o que ele mesmo enviou."""
        manager, whatsapp, _sim, _db = _montar(tmp_path)
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: len(_saidas(whatsapp)) >= 1, timeout=20)
            antes = len(_saidas(whatsapp))

            propria = (whatsapp.sent[0]["text"] if whatsapp.sent
                       else whatsapp.images[0]["caption"])
            manager._handle_message(IncomingMessage(
                message_id="2A_ECO", chat_id="grupo@g.us", chat_name="Consultores",
                sender_id="5567900000000@c.us", sender_name="Ryan", text=propria))
            time.sleep(0.6)

            assert len(_saidas(whatsapp)) == antes, (
                "o bot respondeu à própria mensagem — o laço voltou")
        finally:
            manager.queue.stop()


class TestRetryNaoDuplica:
    """§4 — falha na entrega não pode virar mensagem repetida."""

    def test_o_retry_da_fila_e_silencioso(self, tmp_path):
        """Tentativa que falha e será repetida NÃO fala com o consultor.

        Só o desfecho final é enviado. Sem isso, uma simulação instável
        renderia "não consegui" seguido de "consegui" no grupo.
        """
        # `flaky_for` é indexado por request_id: a primeira solicitação
        # sempre recebe REQ000001.
        manager, whatsapp, _sim, db = _montar(
            tmp_path, simulator=FakeSimulator(flaky_for=("REQ000001",)))
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE status=?",
                (Status.COMPLETED,)) == 1, timeout=30)
            time.sleep(0.8)

            linha = db.fetchone("SELECT * FROM simulations")
            assert linha["attempts"] >= 2, "o teste precisa de uma repetição real"
            assert len(_saidas(whatsapp)) == 1, (
                "a tentativa falha também falou com o consultor")
        finally:
            manager.queue.stop()

    def test_reenvio_nao_atropela_entrega_recente(self, tmp_path):
        """A carência existe porque o status vira 'completed' ANTES do envio."""
        manager, whatsapp, _sim, db = _montar(tmp_path)
        try:
            manager._handle_message(_mensagem(0))
            assert _aguardar(lambda: db.scalar(
                "SELECT COUNT(*) FROM simulations WHERE replied_at IS NOT NULL") == 1,
                timeout=20)
            antes = len(_saidas(whatsapp))

            manager._reenviar_pendentes()   # varredura imediata
            time.sleep(0.3)
            assert len(_saidas(whatsapp)) == antes, "reenviou o que já tinha chegado"
        finally:
            manager.queue.stop()

    def test_confirmacao_falha_mas_imagem_saiu_nao_duplica(self):
        """§4 — o risco concreto: o sinal de envio falha, a imagem foi.

        Reenviar em texto entregaria a mesma resposta duas vezes. O código
        procura a legenda no chat antes de desistir.
        """
        import inspect

        from app.whatsapp import WhatsAppService
        # A confirmação mora em `_disparar_e_confirmar_imagem` (o trecho depois
        # do clique); `_enviar_imagem` repete a checagem para QUALQUER falha
        # depois do clique. As duas têm de procurar no chat antes de desistir.
        fonte = inspect.getsource(WhatsAppService._disparar_e_confirmar_imagem)
        assert "_ja_esta_no_chat" in fonte, (
            "sem essa checagem, uma confirmação falha duplica a resposta")
        assert fonte.index("_ja_esta_no_chat") < fonte.index("envio não confirmado"), (
            "a checagem tem de vir ANTES de desistir")
        envio = inspect.getsource(WhatsAppService._enviar_imagem)
        assert envio.index("_ja_esta_no_chat") < envio.index("raise EnvioSemProva"), (
            "falha depois do clique tem de procurar no chat antes de declarar incerta")
