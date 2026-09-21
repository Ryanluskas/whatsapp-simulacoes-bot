"""Regressão do defeito que derrubava o sistema inteiro.

O erro real gravado 138 vezes no banco da versão anterior::

    Falha ao iniciar WhatsApp: It looks like you are using Playwright Sync API
    inside the asyncio loop. Please use the Async API instead.

A causa era o navegador do WhatsApp ser criado na thread ``wa-loop`` e ser
usado a partir de outras: o worker mandava mensagem, a thread principal e a do
servidor HTTP fechavam o navegador. Depois da primeira violação, o event loop
daquela thread ficava marcado como *running* e nenhum ``sync_playwright()``
seguinte voltava a funcionar.

Estes testes travam a invariante: **nada do Playwright pode ser tocado fora da
thread dona**. Eles não abrem navegador — verificam o roteamento, que é onde o
defeito morava.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.actor import ActorStopped
from app.models import IncomingMessage
from app.state_store import StateStore
from app.models import ResultadoEnvio
from app.whatsapp import CONNECTED, WhatsAppService


class ServicoInstrumentado(WhatsAppService):
    """Substitui só o que abriria um navegador de verdade."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.threads_do_launch: set[int] = set()
        self.threads_do_send: set[int] = set()
        self.threads_do_teardown: set[int] = set()
        self.threads_do_qr: set[int] = set()
        self.enviados: list[dict] = []
        self.pronto = threading.Event()

    def _launch(self) -> None:
        self.threads_do_launch.add(threading.get_ident())

    def _teardown(self) -> None:
        self.threads_do_teardown.add(threading.get_ident())

    def _serve(self) -> None:
        self._set_status(state=CONNECTED, phone="+55 67 90000-0000", chat_name="Consultores")
        self.pronto.set()
        while not self.stopping:
            if not self._drain(0.05):
                return

    def _do_send(self, chat_id, chat_name, text, quote_message_id,
                 texto_sem_citacao=""):
        # A assinatura acompanha a real: este dublê SOBRESCREVE `_do_send`,
        # então um parâmetro a mais lá vira TypeError aqui — e o erro chega
        # como "corrida entre threads", que é o que estes testes procuram.
        self.threads_do_send.add(threading.get_ident())
        time.sleep(0.01)  # janela para uma corrida se manifestar
        self.enviados.append({"chat": chat_name, "text": text, "quote": quote_message_id})
        return ResultadoEnvio(ok=True, via="texto", quoted_ok=bool(quote_message_id))

    def _do_qr(self) -> str:
        self.threads_do_qr.add(threading.get_ident())
        return "data:image/png;base64,FAKE"


@pytest.fixture()
def servico(tmp_path):
    s = ServicoInstrumentado(
        profile_dir=tmp_path / "perfil",
        group_name="Consultores",
        state=StateStore(tmp_path / "state.json"),
        poll_seconds=1.0,
        headless=True,
    )
    s.start()
    assert s.pronto.wait(timeout=5), "o serviço não ficou pronto"
    try:
        yield s
    finally:
        s.stop(timeout=5)


class TestIsolamentoDeThread:
    def test_envio_de_varias_threads_roda_numa_so(self, servico):
        """Era exatamente isto que quebrava: o worker enviando pela página do wa-loop."""
        erros: list[BaseException] = []

        def enviar(i: int):
            try:
                servico.send("grupo@g.us", "Consultores", f"mensagem {i}", f"MSG{i}")
            except BaseException as exc:  # noqa: BLE001
                erros.append(exc)

        threads = [threading.Thread(target=enviar, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not erros, erros
        assert len(servico.enviados) == 12
        assert len(servico.threads_do_send) == 1, "mais de uma thread tocou a página"
        assert servico.threads_do_send == servico.threads_do_launch, \
            "quem enviou não é a mesma thread que abriu o navegador"
        assert threading.get_ident() not in servico.threads_do_send

    def test_qr_pedido_de_fora_tambem_e_roteado(self, servico):
        assert servico.qr_data_url() == "data:image/png;base64,FAKE"
        assert servico.threads_do_qr == servico.threads_do_launch

    def test_encerramento_acontece_na_thread_dona(self, tmp_path):
        """`manager.stop()` roda na thread principal e fechava o navegador de lá."""
        s = ServicoInstrumentado(
            profile_dir=tmp_path / "perfil", group_name="Consultores",
            state=StateStore(tmp_path / "state.json"), headless=True,
        )
        s.start()
        assert s.pronto.wait(timeout=5)
        principal = threading.get_ident()

        s.stop(timeout=5)

        assert s.threads_do_teardown, "o teardown não rodou"
        assert principal not in s.threads_do_teardown, \
            "o navegador foi fechado a partir da thread errada"
        assert s.threads_do_teardown == s.threads_do_launch

    def test_reconexao_nao_deixa_o_servico_morto(self, servico):
        """Antes, uma reconexão inutilizava a thread e nada mais voltava."""
        servico.request_reconnect()
        time.sleep(0.6)
        assert servico.pronto.wait(timeout=6)
        assert servico.running

        servico.send("grupo@g.us", "Consultores", "depois de reconectar", "")
        assert servico.enviados[-1]["text"] == "depois de reconectar"
        # Continua sendo uma thread só, mesmo após reiniciar o ciclo
        assert len(servico.threads_do_send) == 1

    def test_envio_com_servico_parado_falha_claramente(self, tmp_path):
        s = ServicoInstrumentado(
            profile_dir=tmp_path / "perfil", group_name="Consultores",
            state=StateStore(tmp_path / "state.json"), headless=True,
        )
        s.start()
        assert s.pronto.wait(timeout=5)
        s.stop(timeout=5)
        with pytest.raises(ActorStopped):
            s.send("grupo@g.us", "Consultores", "não deve sair", "")


class TestLeituraDeMensagens:
    def test_linha_de_base_impede_responder_mensagens_antigas(self, tmp_path):
        """Sem state.json, o bot lia as 30 últimas do grupo e respondia todas."""
        store = StateStore(tmp_path / "state.json")
        antigas = [f"false_grupo@g.us_ANTIGA{i}_5567988887777@c.us" for i in range(30)]

        assert store.baseline_if_new("grupo@g.us", antigas) is True
        assert all(store.has_seen(m) for m in antigas)

        nova = "false_grupo@g.us_NOVA_5567988887777@c.us"
        assert store.baseline_if_new("grupo@g.us", antigas + [nova]) is False
        assert not store.has_seen(nova)

    def test_mensagem_carrega_o_telefone_do_remetente(self):
        msg = IncomingMessage(
            message_id="false_grupo@g.us_MSG1_5567988887777@c.us",
            chat_id="grupo@g.us", chat_name="Consultores",
            sender_id="5567988887777@c.us", sender_name="Ryan",
            text="Fazer simulação",
        )
        assert msg.sender_phone == "5567988887777"


class TestStatus:
    def test_status_reflete_a_conexao(self, servico):
        status = servico.status
        assert status.connected is True
        assert status.state == CONNECTED
        assert status.phone == "+55 67 90000-0000"
        assert status.as_dict()["connected"] is True

    def test_callback_de_status_e_disparado(self, tmp_path):
        recebidos = []
        s = ServicoInstrumentado(
            profile_dir=tmp_path / "perfil", group_name="Consultores",
            state=StateStore(tmp_path / "state.json"), headless=True,
            on_status=lambda st: recebidos.append(st.state),
        )
        s.start()
        try:
            assert s.pronto.wait(timeout=5)
            time.sleep(0.3)
            assert CONNECTED in recebidos
        finally:
            s.stop(timeout=5)


class TestEventosDeConexao:
    """O monitor enchia de "WhatsApp conectado" a cada poucos segundos.

    Causa: ``_poll_messages`` grava ``last_poll`` a cada ciclo de leitura; isso
    contava como mudança de status e o manager publicava um evento de conexão
    novo toda vez. O evento gravado é uma TRANSIÇÃO, não um batimento.
    """

    def test_batimento_de_leitura_nao_conta_como_mudanca(self, tmp_path):
        vistos: list[str] = []
        s = ServicoInstrumentado(
            profile_dir=tmp_path / "perfil", group_name="Consultores",
            state=StateStore(tmp_path / "state.json"), headless=True,
            on_status=lambda st: vistos.append(st.state),
        )
        s.start()
        try:
            assert s.pronto.wait(timeout=5)
            time.sleep(0.2)
            antes = len(vistos)

            # 20 ciclos de leitura, sem nada mudar de fato
            for _ in range(20):
                s.call(s._set_status, last_poll="2026-08-28T03:00:00Z", timeout=5)

            assert len(vistos) == antes, (
                f"{len(vistos) - antes} notificação(ões) de status por batimento")
            # ...mas o valor continua sendo guardado, para a tela de Status
            assert s.status.last_poll == "2026-08-28T03:00:00Z"
        finally:
            s.stop(timeout=5)

    def test_mudanca_real_ainda_notifica(self, tmp_path):
        vistos: list[str] = []
        s = ServicoInstrumentado(
            profile_dir=tmp_path / "perfil", group_name="Consultores",
            state=StateStore(tmp_path / "state.json"), headless=True,
            on_status=lambda st: vistos.append(st.state),
        )
        s.start()
        try:
            assert s.pronto.wait(timeout=5)
            time.sleep(0.2)
            antes = len(vistos)
            s.call(s._set_status, state="disconnected", last_error="caiu", timeout=5)
            assert len(vistos) == antes + 1
            assert vistos[-1] == "disconnected"
        finally:
            s.stop(timeout=5)


class TestTransicoesNoManager:
    """O manager só grava evento quando a conexão realmente vira."""

    def _manager(self, tmp_path):
        from app.db import Database
        from app.events import EventHub
        from app.manager import BotManager
        from tests.test_concurrency import _config

        config = _config(tmp_path)
        db = Database(config.db_path)
        hub = EventHub(db)
        return BotManager(config, db, hub), hub, db

    def test_status_repetido_nao_vira_linha_no_historico(self, tmp_path):
        from app.whatsapp import CONNECTED, WhatsAppStatus

        manager, _hub, db = self._manager(tmp_path)
        conectado = WhatsAppStatus(state=CONNECTED, phone="+55 62 90001008",
                                   chat_name="Operacional", since="2026-08-28T03:00:00Z")

        for _ in range(15):
            manager._on_whatsapp_status(conectado)

        gravados = db.scalar(
            "SELECT COUNT(*) FROM events WHERE type='whatsapp_connected'")
        assert gravados == 1, f"{gravados} eventos de conexão para uma única conexão"

    def test_queda_e_volta_geram_uma_linha_cada(self, tmp_path):
        from app.whatsapp import CONNECTED, DISCONNECTED, WhatsAppStatus

        manager, _hub, db = self._manager(tmp_path)
        ligado = WhatsAppStatus(state=CONNECTED, phone="+55 62 90001008")
        caido = WhatsAppStatus(state=DISCONNECTED, last_error="navegador fechado")

        for estado in (ligado, ligado, caido, caido, ligado, ligado):
            manager._on_whatsapp_status(estado)

        assert db.scalar("SELECT COUNT(*) FROM events WHERE type='whatsapp_connected'") == 2
        assert db.scalar("SELECT COUNT(*) FROM events WHERE type='whatsapp_disconnected'") == 1

    def test_nao_anuncia_queda_antes_da_primeira_conexao(self, tmp_path):
        from app.whatsapp import DISCONNECTED, STARTING, WhatsAppStatus

        manager, _hub, db = self._manager(tmp_path)
        manager._on_whatsapp_status(WhatsAppStatus(state=STARTING))
        manager._on_whatsapp_status(WhatsAppStatus(state=DISCONNECTED, last_error="tentando"))

        assert db.scalar("SELECT COUNT(*) FROM events WHERE type='whatsapp_disconnected'") == 0


class TestBacklogEntreReinicios:
    """Mensagens que chegaram com o bot DESLIGADO.

    É diferente da linha de base: aquela protege a primeira instalação, esta
    garante que desligar o bot não faz o consultor ficar sem resposta.
    """

    def test_responde_o_que_chegou_com_o_bot_fora_do_ar(self, tmp_path):
        caminho = tmp_path / "state.json"
        antigas = [f"false_grupo@g.us_ANTIGA{i}_5567988887777@c.us" for i in range(5)]

        primeira = StateStore(caminho)
        assert primeira.baseline_if_new("grupo@g.us", antigas) is True

        # bot cai; dois consultores mandam CPF enquanto ele está fora
        offline = [f"false_grupo@g.us_OFFLINE{i}_5567988887777@c.us" for i in range(2)]

        # reinício: outro StateStore lendo o MESMO arquivo
        depois = StateStore(caminho)
        assert depois.baseline_if_new("grupo@g.us", antigas + offline) is False, (
            "recriar a linha de base no reinício faria o bot ignorar tudo que "
            "chegou enquanto esteve desligado")
        assert all(depois.has_seen(m) for m in antigas), "não pode reprocessar as antigas"
        assert all(not depois.has_seen(m) for m in offline), (
            "as mensagens recebidas com o bot fora do ar têm de ser respondidas")

    def test_a_linha_de_base_sobrevive_ao_reinicio(self, tmp_path):
        caminho = tmp_path / "state.json"
        StateStore(caminho).baseline_if_new("grupo@g.us", ["false_grupo@g.us_A_x@c.us"])
        assert StateStore(caminho).is_baselined("grupo@g.us")


class TestCitacaoDaMensagem:
    """A citação pode falhar; o que ela não pode é falhar calada."""

    def _servico(self, tmp_path, monkeypatch, consegue_citar: bool):
        s = WhatsAppService(
            profile_dir=tmp_path / "perfil", group_name="Consultores",
            state=StateStore(tmp_path / "state.json"), headless=True,
        )
        registros: list[tuple[str, str]] = []
        monkeypatch.setattr(s, "_log", lambda nivel, msg: registros.append((nivel, msg)))
        monkeypatch.setattr(s, "_try_quote", lambda _id: consegue_citar)
        # A citação só vale com PROVA: clicar em "Responder" não basta, a
        # barra tem de aparecer no rodapé. O dublê precisa cobrir os dois.
        # `_citar` devolve o que foi PROVADO (`ok`/`unverified`/vazio), nao um
        # booleano: quem responde pela prova e' `_conferir_citacao`.
        monkeypatch.setattr(s, "_conferir_citacao",
                            lambda _id, espera=2.0: "ok" if consegue_citar else "")
        monkeypatch.setattr(s, "_fechar_encaminhamento", lambda: False)
        monkeypatch.setattr(s, "_limpar_preview", lambda: None)
        return s, registros

    def test_falha_ao_citar_vira_aviso(self, tmp_path, monkeypatch):
        s, registros = self._servico(tmp_path, monkeypatch, consegue_citar=False)
        assert s._citar("false_g@g.us_M_x@c.us", "texto") == ""
        assert [n for n, _ in registros] == ["WARNING"], (
            "sem este aviso, ninguém descobre que o menu 'Responder' mudou")

    def test_citacao_bem_sucedida_nao_polui_o_log(self, tmp_path, monkeypatch):
        s, registros = self._servico(tmp_path, monkeypatch, consegue_citar=True)
        assert s._citar("false_g@g.us_M_x@c.us", "texto") == "ok"
        assert registros == []

    def test_sem_id_para_citar_nao_tenta_nem_avisa(self, tmp_path, monkeypatch):
        s, registros = self._servico(tmp_path, monkeypatch, consegue_citar=False)
        assert s._citar("", "texto") == ""
        assert registros == []


class TestErroDePerfilEmUso:
    """"Target page, context or browser has been closed" não diz nada.

    É o que o Chromium devolve quando outro processo já segura o mesmo
    user_data_dir. Apareceu repetidamente enquanto navegadores órfãos de
    execuções anteriores continuavam vivos, e ninguém tinha como saber disso
    pela mensagem.
    """

    def _explicar(self, mensagem: str) -> str:
        from pathlib import Path

        from app.whatsapp import _explicar_falha_de_perfil
        return _explicar_falha_de_perfil(mensagem, Path(r"C:\x\.whatsapp-profile"))

    @pytest.mark.parametrize("cru", [
        "Page.goto: Target page, context or browser has been closed",
        "BrowserType.launch_persistent_context: Target page, context or browser has been closed",
        "Failed to create a ProcessSingleton for your profile directory",
    ])
    def test_explica_perfil_em_uso(self, cru):
        explicado = self._explicar(cru)
        assert cru in explicado, "a mensagem original não pode ser perdida"
        assert ".whatsapp-profile" in explicado
        assert "brave.exe" in explicado

    def test_nao_mexe_em_erro_de_outra_natureza(self):
        outro = "net::ERR_INTERNET_DISCONNECTED"
        assert self._explicar(outro) == outro


class TestRedeDeSegurancaNaoQuebra:
    """A proteção contra encaminhar roda nos caminhos de FALHA.

    Ou seja: exatamente quando o navegador pode já estar derrubado. Se ela
    levantar exceção própria nesse cenário, deixa de proteger justamente na
    hora em que é necessária.
    """

    def test_sem_navegador_nao_levanta(self, tmp_path):
        from app.state_store import StateStore
        from app.whatsapp import WhatsAppService

        s = WhatsAppService(profile_dir=tmp_path / "p", group_name="G",
                            state=StateStore(tmp_path / "s.json"), headless=True)
        assert s._page is None
        assert s._fechar_encaminhamento() is False
        s._limpar_ui()   # não pode levantar


class TestRecuperarOCampoDeDigitacao:
    """"Timeout 15000ms waiting for footer div[contenteditable]".

    Foi o erro que mais apareceu em produção — e o pior deles: a simulação
    tinha terminado, o resultado estava pronto, e a resposta não saía porque
    algum menu aberto pela tentativa de citação ficava por cima do campo.
    Citar é desejável; responder é obrigatório.
    """

    def _servico(self, tmp_path):
        from app.state_store import StateStore
        from app.whatsapp import WhatsAppService
        return WhatsAppService(
            profile_dir=tmp_path / "p", group_name="Grupo",
            state=StateStore(tmp_path / "s.json"), headless=True)

    def test_sem_navegador_falha_sem_levantar(self, tmp_path):
        s = self._servico(tmp_path)
        assert s._page is None
        assert s._garantir_composer("Grupo") is False

    def test_a_falha_da_citacao_limpa_a_tela(self, tmp_path):
        """Sem esta limpeza, a resposta seguinte morre no campo de digitação.

        Antes este teste procurava a frase "as três vias falharam" dentro do
        código. Quando a citação foi reescrita para os seis passos do caminho
        manual, a frase sumiu e o teste quebrou — sem que a propriedade que
        ele protege tivesse mudado. Agora ele exercita o caminho de falha e
        confere o efeito.
        """
        from app.whatsapp import (BOTAO_DE_OPCOES_JS, CITACAO_PENDENTE_JS,
                                  GEOMETRIA_DA_LINHA_JS, ITEM_RESPONDER_JS,
                                  MENU_ABERTO_JS, WhatsAppService)

        limpezas: list[str] = []

        class PaginaFalsa:
            """Menu abre, mas sem nenhum item 'Responder'."""

            url = "https://web.whatsapp.com/"

            class _Mouse:
                def move(self, *a, **k): pass
                def click(self, *a, **k): pass

            mouse = _Mouse()

            def wait_for_timeout(self, *a, **k): pass

            def locator(self, seletor):
                # A citação passou a clicar no ELEMENTO (a setinha marcada) em
                # vez de na coordenada, e a limpeza de citação órfã também usa
                # locator. O dublê acompanha.
                class _Alvo:
                    @property
                    def first(self): return self
                    def click(self, **kw): pass
                    def count(self): return 0
                return _Alvo()

            def evaluate(self, js, arg=None):
                if js is GEOMETRIA_DA_LINHA_JS:
                    return {"achou": True, "via": "data-id", "x": 100, "y": 200,
                            "texto": "Maria Tabaré"}
                if js is BOTAO_DE_OPCOES_JS:
                    return {"achou": True, "x": 110, "y": 190, "rotulo": "Menu de contexto"}
                if js is MENU_ABERTO_JS:
                    return True
                if js is CITACAO_PENDENTE_JS:
                    return {"pendente": False}
                if js is ITEM_RESPONDER_JS:
                    return {"achou": False, "rotulos": ["copiar", "encaminhar"]}
                return None

        servico = WhatsAppService(
            profile_dir=tmp_path / "perfil", group_name="G", state=None,
            poll_seconds=1.0, headless=True, reply_quote=True,
            browser_executable="", bot_self_name="Bot")
        servico._page = PaginaFalsa()
        servico._limpar_ui = lambda: limpezas.append("limpou")
        servico._diagnosticar_citacao_uma_vez = lambda *a, **k: None

        assert servico._passos_da_citacao("2A1") is False
        assert limpezas == ["limpou"], (
            "a saída de falha da citação precisa devolver a tela ao normal")

    def test_o_envio_recupera_antes_de_digitar(self, tmp_path):
        import inspect

        from app.whatsapp import WhatsAppService
        fonte = inspect.getsource(WhatsAppService._do_send)
        assert fonte.index("_garantir_composer") < fonte.index("box.click"), (
            "a recuperação tem de vir ANTES do clique que estava expirando")

    def test_a_recuperacao_tem_tres_degraus(self):
        """Conferir menus, fechar o que está por cima, reabrir a conversa."""
        import inspect

        from app.whatsapp import WhatsAppService
        fonte = inspect.getsource(WhatsAppService._garantir_composer)
        assert "_limpar_ui" in fonte
        assert "_open_chat" in fonte
        assert fonte.index("_limpar_ui") < fonte.index("_open_chat"), (
            "reabrir a conversa é o último recurso, não o primeiro")


class TestNuncaEnviarDaTelaErrada:
    """O bot enviava de fora da conversa, e isso estragava tudo.

    O diagnóstico do operador mostrou a tela real no momento do envio:

        itens=['Tudo', 'Não lidas 3', 'Favoritas', 'Grupos', ...,
               'Enviar documento', 'Adicionar contato', 'Nova ligação']
        ícones=['wa-wordmark', 'message-fail', 'key', 'lock-outline']

    Isso é a tela INICIAL do WhatsApp, sem conversa aberta. Duas consequências
    que pareciam defeitos separados:

    * o anexo caía no input de DOCUMENTO da tela inicial ("Enviar documento"),
      e o resultado chegava como arquivo em vez de foto;
    * a citação não achava a mensagem — não havia conversa na tela.

    A causa era `self._current_chat` desatualizado: ele guarda a última
    conversa que abrimos, e o WhatsApp pode ter voltado à tela inicial no meio
    do caminho. O bot pulava a reabertura por acreditar nele.
    """

    def _fonte(self, nome: str) -> str:
        import inspect

        from app.whatsapp import WhatsAppService
        return inspect.getsource(getattr(WhatsAppService, nome))

    def test_o_envio_verifica_a_conversa_em_vez_de_confiar_no_estado(self):
        for metodo in ("_do_send", "_enviar_imagem"):
            fonte = self._fonte(metodo)
            assert "_garantir_conversa" in fonte, f"{metodo} não verifica a conversa"
            assert "self._current_chat != target" not in fonte, (
                f"{metodo} voltou a confiar no estado guardado")

    def test_a_verificacao_olha_o_main_e_o_cabecalho(self):
        """`#main` só existe com conversa aberta; o cabeçalho diz qual é."""
        fonte = self._fonte("_garantir_conversa")
        assert '"#main"' in fonte
        assert "_nome_no_cabecalho" in fonte
        assert "_normalizar" in fonte, "comparação exata quebraria com acento decomposto"

    def test_conversa_errada_forca_reabertura(self):
        fonte = self._fonte("_garantir_conversa")
        assert 'self._current_chat = ""' in fonte, (
            "sem limpar o estado, `_open_chat` retorna cedo achando que já está lá")
        assert "_open_chat" in fonte

    def test_sem_navegador_nao_levanta(self, tmp_path):
        from app.state_store import StateStore
        from app.whatsapp import WhatsAppService

        s = WhatsAppService(profile_dir=tmp_path / "p", group_name="G",
                            state=StateStore(tmp_path / "s.json"), headless=True)
        assert s._garantir_conversa("G") is False

    def test_falha_ao_abrir_impede_o_envio(self):
        """Enviar da tela errada é pior que não enviar: vai para outra conversa."""
        fonte = self._fonte("_do_send")
        assert "raise RuntimeError" in fonte[fonte.index("_garantir_conversa"):
                                             fonte.index("_garantir_conversa") + 260]
