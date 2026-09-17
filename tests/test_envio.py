"""A entrega da resposta, com prova de cada etapa.

SPEC §5: nenhuma etapa de envio é considerada concluída sem uma verificação
lida do DOM depois dela. Antes, o código executava uma ação na interface e
assumia que deu certo — quando o WhatsApp mudava, o passo falhava, ninguém
percebia, e o fluxo seguia como se tivesse funcionado. Foi assim que a imagem
virou documento e a citação sumiu sem rastro.

Estes testes usam dublês no lugar do navegador: o que se verifica aqui é a
DECISÃO (seguir, cair para o próximo, desistir), não o DOM — esse é o papel
do `test_leitura_dom.py`, que roda num Chromium de verdade.
"""

from __future__ import annotations

import pytest

from app.models import ResultadoEnvio
from app.state_store import StateStore
from app.whatsapp import WhatsAppService


@pytest.fixture()
def servico(tmp_path):
    return WhatsAppService(
        profile_dir=tmp_path / "perfil", group_name="Santander Capital Simulações",
        state=StateStore(tmp_path / "state.json"), headless=True)


class TestResultadoDeEnvio:
    """Trocar o `bool` por um resultado com motivo e evidência."""

    def test_sucesso_e_verdadeiro(self):
        r = ResultadoEnvio(ok=True, via="colar", quoted_ok=True, tipo_midia="imagem")
        assert bool(r) is True
        assert r.motivo == ""

    def test_falha_carrega_o_motivo(self):
        r = ResultadoEnvio(ok=False, via="texto",
                           motivo="preview não era imagem",
                           evidencia={"accept_vistos": ["*"]})
        assert bool(r) is False
        assert "preview" in r.motivo
        assert r.evidencia["accept_vistos"] == ["*"]

    def test_evidencia_nao_e_compartilhada_entre_instancias(self):
        """`field(default_factory=dict)` — um dict de classe vazaria dados."""
        a, b = ResultadoEnvio(ok=True), ResultadoEnvio(ok=True)
        a.evidencia["x"] = 1
        assert b.evidencia == {}


class TestCitacaoExigeProva:
    """§3.3 — clicar em "Responder" não é prova de que a citação pegou."""

    def _preparar(self, servico, monkeypatch, clique_ok, barra_ok):
        registros: list[tuple[str, str]] = []
        monkeypatch.setattr(servico, "_log",
                            lambda n, m: registros.append((n, m)))
        monkeypatch.setattr(servico, "_try_quote", lambda _id: clique_ok)
        monkeypatch.setattr(servico, "_citacao_confirmada",
                            lambda _id, espera=2.0: barra_ok)
        monkeypatch.setattr(servico, "_fechar_encaminhamento", lambda: False)
        monkeypatch.setattr(servico, "_limpar_preview", lambda: None)
        monkeypatch.setattr(servico, "_limpar_ui", lambda: None)
        monkeypatch.setattr(servico, "_diagnosticar_citacao_uma_vez",
                            lambda *a, **k: None)
        return registros

    def test_clique_e_barra_ok_e_citacao_valida(self, servico, monkeypatch):
        registros = self._preparar(servico, monkeypatch, True, True)
        assert servico._citar("2A_MSG", "texto") is True
        assert servico._ultima_citacao_ok is True
        assert registros == [], f"log poluído: {registros}"

    def test_clicou_mas_a_barra_nao_apareceu(self, servico, monkeypatch):
        """O caso silencioso: o menu respondeu, a citação não pegou."""
        registros = self._preparar(servico, monkeypatch, True, False)
        assert servico._citar("2A_MSG", "texto") is False
        assert servico._ultima_citacao_ok is False
        assert any("barra de citação não" in m for _n, m in registros), registros

    def test_nem_o_menu_respondeu(self, servico, monkeypatch):
        registros = self._preparar(servico, monkeypatch, False, False)
        assert servico._citar("2A_MSG", "texto") is False
        assert any("Não consegui citar" in m for _n, m in registros), registros

    def test_sem_id_nao_tenta_nem_avisa(self, servico, monkeypatch):
        registros = self._preparar(servico, monkeypatch, True, True)
        assert servico._citar("", "texto") is False
        assert registros == []

    def test_falha_na_citacao_nao_impede_a_resposta(self, servico):
        """§3.4 — resposta sem citação é degradação aceitável.

        Resposta NÃO entregue, não é. `_citar` devolve False e quem chama
        segue com o envio, sem interromper.
        """
        # Este teste checava que o retorno de `_citar` era DESCARTADO. Ele
        # deixou de ser: agora escolhe qual versão do texto sai (com ou sem o
        # nome do consultor). Mas a propriedade que importa é outra, e é esta
        # que se verifica aqui — o envio acontece do mesmo jeito.
        enviado = {}

        class PaginaFalsa:
            class _Teclado:
                def insert_text(self, texto): enviado["texto"] = texto
                def press(self, tecla): enviado.setdefault("teclas", []).append(tecla)

            keyboard = _Teclado()

            def wait_for_timeout(self, *a, **k): pass

            def locator(self, *a, **k):
                class _Alvo:
                    def click(self, **kw): pass
                    @property
                    def last(self): return self
                return _Alvo()

        servico._page = PaginaFalsa()
        servico._set_status(state="connected")
        servico._garantir_conversa = lambda *_a: True
        servico._garantir_composer = lambda *_a: True
        servico._lembrar_do_que_enviamos = lambda *_a: None
        servico._citar = lambda *_a: False        # a citação FALHA

        resultado = servico._do_send("g@g.us", "G", "com citação", "2A1",
                                     "solta\n↩ Ryan")

        assert bool(resultado) is True, "a citação falhou e a resposta não saiu"
        assert resultado.quoted_ok is False
        assert "Enter" in enviado.get("teclas", []), "não chegou a enviar"
        assert enviado["texto"] == "solta\n↩ Ryan", (
            "sem citação tem de sair a versão com o nome do consultor")


class TestImagemNuncaViraDocumento:
    """§4 — o consultor precisa ver o valor na conversa, não baixar arquivo."""

    def test_so_input_de_documento_faz_desistir(self, servico, monkeypatch):
        """Melhor cair para texto do que entregar um card de download."""
        monkeypatch.setattr(servico, "_log", lambda *a: None)
        monkeypatch.setattr(servico, "_limpar_preview", lambda: None)
        monkeypatch.setattr(servico, "_abrir_menu_de_anexo", lambda: False)
        monkeypatch.setattr(servico, "_diagnosticar_anexo", lambda *a: None)
        monkeypatch.setattr(servico, "_input_de_imagem", lambda: (None, ["*"]))

        class LocalizadorFalso:
            """`#main` existe: o teste é sobre o input, não sobre a conversa."""

            def count(self):
                return 1

        class PaginaFalsa:
            """Só o que o caminho de anexo realmente chama."""

            def locator(self, _seletor):
                return LocalizadorFalso()

            def evaluate(self, js, *_a, **_k):
                # COLAR falha; a busca do item de menu não acha nada.
                return ({"ok": False, "motivo": "sem compositor"}
                        if "ClipboardEvent" in js else "")

            def wait_for_timeout(self, _ms):
                pass

        servico._page = PaginaFalsa()
        ok, via, evidencia = servico._anexar_imagem(__file__)
        assert ok is False
        assert via == ""
        assert evidencia["accept_vistos"] == ["*"]

    def test_a_ordem_das_estrategias_deixa_o_menu_por_ultimo(self):
        """O menu é o passo mais frágil, e tem "Documento" como primeiro item.

        A ordem observada no DOM real mudou a estratégia: colar primeiro, e o
        `input` com `accept="image/*"` logo depois — ele já existe SEM abrir
        menu nenhum. O menu virou último recurso.
        """
        import inspect
        fonte = inspect.getsource(WhatsAppService._anexar_imagem)
        assert fonte.index("COLAR_IMAGEM_JS") < fonte.index("_input_de_imagem")
        assert fonte.index("_input_de_imagem") < fonte.index("_abrir_menu_de_anexo"), (
            "o menu de anexo voltou para antes do input — ele tem 'Documento' "
            "como primeiro item")

    def test_o_preview_e_conferido_antes_de_enviar(self):
        import inspect
        fonte = inspect.getsource(WhatsAppService._anexar_imagem)
        assert fonte.count("_esperar_preview") >= 3, (
            "toda estratégia tem de provar que abriu um preview de IMAGEM")

    def test_nao_ha_seletor_generico_de_input(self):
        """`input[type=file]` sem filtro pega o de documento — é o defeito B."""
        import inspect
        fonte = inspect.getsource(WhatsAppService._input_de_imagem)
        assert '"image" in aceita' in fonte or "'image' in aceita" in fonte


class TestEstadoNaoVazaEntreEnvios:
    """§5.3 — preview pendente contamina o próximo envio.

    É o candidato mais forte ao "às vezes não manda nada": a tentativa
    anterior deixa um overlay aberto e o seguinte digita no lugar errado.
    """

    def test_limpar_preview_nao_levanta_sem_navegador(self, servico):
        assert servico._page is None
        servico._limpar_preview()   # não pode levantar

    def test_toda_estrategia_limpa_antes_da_proxima(self):
        import inspect
        fonte = inspect.getsource(WhatsAppService._anexar_imagem)
        assert fonte.count("_limpar_preview") >= 3

    def test_o_envio_tem_orcamento_de_tempo(self):
        assert 20 <= WhatsAppService._ORCAMENTO_DE_ENVIO <= 120


class TestGuardaDeChat:
    """§5.2 — enviar no chat errado vaza CPF de cliente para outra conversa.

    É o pior defeito possível deste sistema, e por isso a verificação é de
    leitura do DOM, não de estado guardado.
    """

    def test_sem_navegador_nao_envia(self, servico):
        assert servico._garantir_conversa("Santander Capital Simulações") is False

    def test_a_verificacao_le_o_dom(self):
        import inspect
        fonte = inspect.getsource(WhatsAppService._garantir_conversa)
        assert '"#main"' in fonte
        assert "_nome_no_cabecalho" in fonte

    def test_o_envio_de_texto_verifica_antes(self):
        import inspect
        fonte = inspect.getsource(WhatsAppService._do_send)
        assert fonte.index("_garantir_conversa") < fonte.index("insert_text")

    def test_o_envio_de_imagem_verifica_antes(self):
        import inspect
        fonte = inspect.getsource(WhatsAppService._enviar_imagem)
        assert fonte.index("_garantir_conversa") < fonte.index("_anexar_imagem")


class TestSinaisSemDependerDeClasseCSS:
    """Auditoria: `.message-in` / `.message-out` não podem ser o único sinal.

    Se essa classe não existir nesta instalação — e o `data-id` pelado sugere
    que o HTML aqui é outro —, qualquer verificação que dependa só dela falha
    SEMPRE. A ordem passou a ser: prefixo do `data-id`, recibo de entrega,
    e só então a classe.
    """

    def _fonte(self, nome: str) -> str:
        import app.whatsapp as w
        return getattr(w, nome)

    def test_a_verificacao_pos_envio_tenta_o_data_id_primeiro(self):
        js = self._fonte("ULTIMA_SAIDA_JS")
        assert js.index("startsWith('true_')") < js.index("data-icon^=\"msg-\"")
        assert js.index("data-icon^=\"msg-\"") < js.index("div.message-out")

    def test_o_anti_laco_tenta_o_data_id_primeiro(self):
        js = self._fonte("READ_MESSAGES_JS")
        trecho = js[js.index("const ehNossa"):]
        assert trecho.index("startsWith('true_')") < trecho.index("data-icon^=\"msg-\"")
        assert trecho.index("data-icon^=\"msg-\"") < trecho.index("message-out")

    def test_a_trava_de_reenvio_nao_usa_so_a_classe(self):
        js = self._fonte("JA_ENVIADO_JS")
        assert "startsWith('true_')" in js
        assert 'data-icon^="msg-"' in js


class TestTravaContraReenvioDuplicado:
    """Cinco cópias no grupo do cliente é pior que uma entrega não confirmada.

    Se a verificação de entrega falhar por qualquer motivo, o reenvio mandaria
    a mesma resposta até cinco vezes. A trava pergunta ao próprio chat se o
    `REQ` já está lá — e a pergunta não depende de classe CSS.
    """

    def test_o_manager_pergunta_antes_de_reenviar(self):
        import inspect

        from app.manager import BotManager
        fonte = inspect.getsource(BotManager._reenviar_um)
        assert "ja_enviado" in fonte
        assert fonte.index("ja_enviado") < fonte.index("_send_reply"), (
            "a trava tem de vir ANTES do envio")

    def test_falso_negativo_marca_entregue_e_avisa(self):
        import inspect

        from app.manager import BotManager
        fonte = inspect.getsource(BotManager._reenviar_um)
        trecho = fonte[fonte.index("ja_enviado"):fonte.index("_send_reply")]
        assert "replied_at" in trecho, "achou no chat mas não marcou como entregue"
        assert "falso negativo" in trecho.lower(), "precisa registrar o motivo"

    def test_nao_conseguir_perguntar_nao_impede_o_reenvio(self):
        """Ficar sem resposta é o defeito que este laço existe para corrigir."""
        import inspect

        from app.manager import BotManager
        fonte = inspect.getsource(BotManager._reenviar_um)
        assert "Vou reenviar mesmo assim" in fonte

    def test_a_consulta_passa_pelo_ator(self, servico, monkeypatch):
        """Playwright nunca sai da thread dona."""
        import inspect

        from app.whatsapp import WhatsAppService
        fonte = inspect.getsource(WhatsAppService.ja_enviado)
        assert "self.call(" in fonte, "acessou a página fora da thread dona"

    def test_sem_conexao_responde_que_nao_enviou(self, servico):
        """Na dúvida, não afirmar que já foi — senão o consultor fica sem nada."""
        assert servico.ja_enviado("REQ000182") is False


class TestOCitouRealChegaNaMensagem:
    """A mensagem muda conforme a citação pegou ou não.

    Sem isto o `manager` passava `citou=True` chumbado: quando a citação
    falhava, a resposta saía solta no grupo E sem o nome do consultor — o
    consultor via um resultado sem nenhuma pista de que era o dele.

    A escolha só pode ser feita DEPOIS de tentar citar, e quem tenta é a
    camada de WhatsApp. Por isso `mensagens.duas_versoes` monta as duas e a
    camada escolhe: o `manager` não precisa adivinhar antes do envio.
    """

    def _resultado(self):
        from app.models import (IncomingMessage, ParsedRequest, SimulationJob,
                                SimulationResult)

        pedido = ParsedRequest(consultant_name="Ryan", cpf="42888832453",
                               bank="Santander", contract="",
                               customer_name="Ivone Teste", origin="Amapá")
        msg = IncomingMessage(message_id="2A1", chat_id="g@g.us", chat_name="G",
                              sender_id="55@c.us", sender_name="Ryan", text="x")
        job = SimulationJob(request=pedido, message=msg,
                            request_id="REQ000182", simulation_id=1)
        return SimulationResult(job=job, ok=True, status="Sim",
                                reduction_value=4913.52,
                                contracts=({"p": 1},), installment_sum=740.77)

    def test_duas_versoes_diferem_so_pelo_nome(self):
        from app import mensagens

        com, sem = mensagens.duas_versoes(
            mensagens.texto, self._resultado(), "REQ000182", consultor="Ryan")
        assert "↩ Ryan" not in com
        assert "↩ Ryan" in sem
        # E só isso muda: mesma informação, mesma ordem.
        assert sem.replace("↩ Ryan\n", "") == com

    def test_a_legenda_tambem_tem_as_duas(self):
        from app import mensagens

        com, sem = mensagens.duas_versoes(
            mensagens.legenda, self._resultado(), "REQ000182", consultor="Ryan")
        assert "↩ Ryan" in sem and "↩ Ryan" not in com
        assert sem.splitlines()[-1] == "_REQ000182_", "o REQ continua por último"

    def test_sem_consultor_nao_inventa_a_linha(self):
        from app import mensagens

        com, sem = mensagens.duas_versoes(
            mensagens.texto, self._resultado(), "REQ000182", consultor="")
        assert "↩" not in sem and sem == com

    def test_a_camada_escolhe_quando_a_citacao_falha(self):
        """O comportamento que liga tudo: citou=False -> sai a outra versão."""
        from app.evolution import EvolutionClient

        import httpx

        enviados = []

        def servidor(request):
            enviados.append(__import__("json").loads(request.content))
            return httpx.Response(200, json={"key": {"id": "3EB0X"}})

        cliente = EvolutionClient(
            "http://e:8080", "k", "allana", "120@g.us",
            client=httpx.Client(transport=httpx.MockTransport(servidor),
                                base_url="http://e:8080"),
            renderer=None)

        # Sem id para citar -> a Evolution não monta `quoted` -> versão solta.
        r = cliente.send("120@g.us", "G", "com citação",
                         texto_sem_citacao="solta\n↩ Ryan")
        assert r.ok and not r.quoted_ok
        assert enviados[-1]["text"] == "solta\n↩ Ryan"

    def test_com_citacao_sai_a_versao_curta(self):
        from app.evolution import EvolutionClient

        import httpx

        enviados = []

        def servidor(request):
            enviados.append(__import__("json").loads(request.content))
            return httpx.Response(200, json={"key": {"id": "3EB0X"}})

        cliente = EvolutionClient(
            "http://e:8080", "k", "allana", "120@g.us",
            client=httpx.Client(transport=httpx.MockTransport(servidor),
                                base_url="http://e:8080"),
            renderer=None)
        r = cliente.send("120@g.us", "G", "com citação",
                         quote_message_id="3EB0PEDIDO",
                         texto_sem_citacao="solta\n↩ Ryan",
                         quote_text="Ivone 42888832453")
        assert r.ok and r.quoted_ok
        assert enviados[-1]["text"] == "com citação"

    def test_as_duas_camadas_aceitam_o_mesmo_parametro(self):
        """Senão trocar WHATSAPP_MODE quebraria o manager em produção."""
        import inspect

        from app.evolution import EvolutionClient
        from app.whatsapp import WhatsAppService

        for nome, extra in (("send", "texto_sem_citacao"),
                            ("send_image", "caption_sem_citacao")):
            for classe in (WhatsAppService, EvolutionClient):
                params = inspect.signature(getattr(classe, nome)).parameters
                assert extra in params, f"{classe.__name__}.{nome} sem {extra}"

    def test_o_manager_avisa_quando_sai_sem_citacao(self):
        """Falha de citação não segura a resposta — mas não pode ser muda."""
        import inspect

        from app.manager import BotManager

        fonte = inspect.getsource(BotManager._anotar_citacao)
        assert "quoted_ok" in fonte
        assert "WARNING" in fonte


class TestOsDublesAcompanhamAsAssinaturas:
    """Um dublê defasado esconde o defeito atrás de outro sintoma.

    Quando `FakeWhatsApp.send` ficou sem `texto_sem_citacao`, vinte e poucos
    testes falharam com "resultado pronto mas não entregue" — e a causa real
    (`unexpected keyword argument`) só aparecia dentro de um log de WARNING,
    engolida pelo `except Exception` do envio.

    O contrato entre as duas implementações já é verificado; faltava verificar
    o dublê, que é por onde quase toda a suíte passa.
    """

    @pytest.mark.parametrize("metodo", ["send", "send_image"])
    def test_o_duble_aceita_tudo_que_a_camada_real_aceita(self, metodo):
        import inspect

        from app.whatsapp import WhatsAppService
        from tests.test_concurrency import FakeWhatsApp

        real = set(inspect.signature(getattr(WhatsAppService, metodo)).parameters)
        duble = set(inspect.signature(getattr(FakeWhatsApp, metodo)).parameters)
        faltando = real - duble
        assert not faltando, (
            f"FakeWhatsApp.{metodo} não aceita {sorted(faltando)} — "
            "os testes vão falhar por um motivo que não é o real")

    def test_dubles_que_sobrescrevem_metodos_internos_tambem(self):
        """`ServicoInstrumentado` herda de WhatsAppService e troca `_do_send`.

        Aqui a deriva é ainda mais traiçoeira: o TypeError sobe pelo ator e
        chega aos testes de concorrência como "erro numa thread" — que é
        exatamente o que eles existem para detectar. O defeito real fica
        disfarçado do defeito procurado.
        """
        import inspect

        from app.whatsapp import WhatsAppService
        from tests.test_whatsapp_service import ServicoInstrumentado

        for metodo in ("_do_send", "_do_qr"):
            base = getattr(WhatsAppService, metodo, None)
            filho = getattr(ServicoInstrumentado, metodo, None)
            if base is None or filho is None or base is filho:
                continue
            assert (list(inspect.signature(base).parameters)
                    == list(inspect.signature(filho).parameters)), (
                f"ServicoInstrumentado.{metodo} divergiu da assinatura real")

    def test_o_duble_tambem_escolhe_a_versao_sem_citacao(self):
        """Senão ele provaria o caminho feliz e nada mais."""
        from tests.test_concurrency import FakeWhatsApp

        sem_citar = FakeWhatsApp(cita=False)
        sem_citar.send("g", "G", "com citação", texto_sem_citacao="solta ↩ Ryan")
        assert sem_citar.sent[-1]["text"] == "solta ↩ Ryan"

        citando = FakeWhatsApp(cita=True)
        citando.send("g", "G", "com citação", texto_sem_citacao="solta ↩ Ryan")
        assert citando.sent[-1]["text"] == "com citação"
