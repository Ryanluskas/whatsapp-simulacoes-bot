"""Fixtures montadas a partir do DOM REAL, capturado na máquina do operador.

Todo retrabalho deste projeto veio de eu escrever seletor contra um HTML que
imaginei. Estes testes usam os fatos observados com `ferramentas/dump_dom.py`:

* ``.message-in`` / ``.message-out`` **não existem** — nenhuma linha tem;
* ``data-pre-plain-text`` traz ``"[20:14, 30/08/2026] Ryan: "``;
* o bot assina como **"Operacional Capital"**;
* o ``data-id`` vem pelado; as enviadas começam com ``3EB0``;
* o botão do menu não tem ``data-icon`` — só ``aria-label="Abrir opções de
  mensagem"``;
* o menu tem 9 itens, com "Encaminhar" e "Apagar" ao lado de "Responder";
* o menu de anexo tem "Documento" como PRIMEIRO item;
* com a conversa aberta há UM único ``input[type=file]``, ``accept="image/*"``,
  já no DOM sem abrir menu nenhum;
* ``document.querySelector('header')`` pega o header da LISTA LATERAL.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from app.whatsapp import (ACHAR_ITEM_MENU_JS, CHAT_INFO_JS, MARCA_ITEM,
                          READ_MESSAGES_JS)

def _quantas_setinhas(pagina, data_id: str) -> int:
    """Equivalente novo de "quantos gatilhos a bolha oferece".

    A implementação da citação deixou de varrer candidatos e passou a
    procurar UM botão, pelo aria-label real desta instalação
    ("Abrir opções de mensagem"). Estes testes protegem a mesma propriedade
    de sempre — sobretudo *não* confundir o botão de encaminhar com a
    setinha — só que contra o mecanismo que roda hoje.
    """
    return _setinha(pagina, data_id)[0]


def _setinha(pagina, data_id: str) -> tuple[int, list[str]]:
    """(quantas achou, rótulos). Zero ou uma: agora é UM botão nomeado."""
    from app.whatsapp import BOTAO_DE_OPCOES_JS, GEOMETRIA_DA_LINHA_JS

    pagina.evaluate(GEOMETRIA_DA_LINHA_JS, [data_id, ""])
    achado = pagina.evaluate(BOTAO_DE_OPCOES_JS) or {}
    if not achado.get("achou"):
        return 0, []
    return 1, [achado.get("rotulo") or "?"]

EU = "Operacional Capital"
GRUPO = "Santander Capital Simulações"

# Itens exatos do menu de contexto, na ordem observada.
MENU_MENSAGEM = ["Dados da mensagem", "Responder", "Copiar", "Reagir",
                 "Encaminhar", "Fixar", "Pergunte à Meta AI", "Favoritar",
                 "Apagar"]

# Itens exatos do menu de anexo, na ordem observada. "Documento" é o primeiro.
MENU_ANEXO = ["Documento", "Fotos e vídeos", "Câmera", "Áudio", "Contato",
              "Enquete", "Evento", "Nova figurinha"]


@pytest.fixture(scope="module")
def navegador():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


def _linha(data_id: str, autor: str, texto: str) -> str:
    """Uma linha como o WhatsApp desta instalação monta: SEM classes."""
    return f"""
    <div role="row">
      <div data-id="{data_id}">
        <div role="button" aria-label="Reagir">emoji</div>
        <div role="button" aria-label="Abrir opções de mensagem">v</div>
        <div class="copyable-text" data-pre-plain-text="[20:14, 30/08/2026] {autor}: ">
          <span class="selectable-text"><span>{texto}</span></span>
        </div>
      </div>
    </div>"""


class TestAutoriaSemClassesCSS:
    """Quem enviou, num DOM onde `.message-in` e `.message-out` não existem."""

    def _ler(self, navegador, corpo: str, eu: str = EU):
        pagina = navegador.new_page()
        try:
            pagina.set_content(
                f'<!doctype html><html><body><div id="main">{corpo}</div></body></html>')
            return pagina.evaluate(READ_MESSAGES_JS, [30, eu])
        finally:
            pagina.close()

    def test_le_a_mensagem_do_consultor(self, navegador):
        lidas = self._ler(navegador, _linha("2A48135636067914CA2B", "Ryan",
                                            "Ivone Teste 42888832453"))
        assert len(lidas) == 1
        assert "Ivone Teste" in lidas[0]["text"]

    def test_ignora_a_propria_pelo_nome_do_autor(self, navegador):
        """O sinal que funciona aqui: quem assinou no data-pre-plain-text."""
        assert self._ler(navegador,
                         _linha("3EB065E1F5CE2672313B9C", EU, "Nao libera")) == []

    def test_ignora_a_propria_pelo_prefixo_quando_falta_o_autor(self, navegador):
        """Sem data-pre-plain-text, o prefixo 3EB0 resolve."""
        corpo = ('<div role="row"><div data-id="3EB06B5613DB090DA22216">'
                 '<span class="selectable-text">Nao libera</span></div></div>')
        assert self._ler(navegador, corpo) == []

    def test_separa_as_duas_no_mesmo_chat(self, navegador):
        corpo = (_linha("2A48135636067914CA2B", "Ryan", "Ivone Teste")
                 + _linha("3EB065E1F5CE2672313B9C", EU, "Nao libera")
                 + _linha("2A77A2F7D2E4AE82B46B", "Tobias", "Maria Tabare"))
        lidas = self._ler(navegador, corpo)
        assert [m["text"] for m in lidas] == ["Ivone Teste", "Maria Tabare"]

    def test_o_nome_proprio_vem_de_fora(self, navegador):
        """Com outro nome configurado, a mesma mensagem passa a ser lida."""
        corpo = _linha("2A_X", EU, "Nao libera")
        assert self._ler(navegador, corpo, eu="Outro Nome") != []

    def test_nenhuma_linha_tem_as_classes_antigas(self, navegador):
        """Guarda: se alguém montar fixture com classes, o teste perde o valor."""
        pagina = navegador.new_page()
        try:
            pagina.set_content('<!doctype html><html><body><div id="main">'
                               + _linha("2A_X", "Ryan", "oi")
                               + "</div></body></html>")
            assert pagina.locator(".message-in, .message-out").count() == 0
        finally:
            pagina.close()


class TestMenuDeContextoReal:
    """9 itens, com Encaminhar e Apagar ao lado de Responder."""

    def _menu(self, itens=None) -> str:
        return "".join(f"<div><div><span>{i}</span></div></div>"
                       for i in (itens if itens is not None else MENU_MENSAGEM))

    def _achar(self, navegador, html, alvos=("responder", "reply")):
        pagina = navegador.new_page()
        try:
            pagina.set_content(f"<!doctype html><html><body>{html}</body></html>")
            achado = pagina.evaluate(ACHAR_ITEM_MENU_JS, list(alvos))
            marcados = pagina.locator(f"[{MARCA_ITEM}]")
            texto = marcados.first.inner_text().strip() if marcados.count() else ""
            return achado, marcados.count(), texto
        finally:
            pagina.close()

    def test_acha_responder_no_menu_de_9_itens(self, navegador):
        achado, n, texto = self._achar(navegador, self._menu())
        assert achado == "responder"
        assert n == 1 and texto == "Responder"

    @pytest.mark.parametrize("perigoso", ["Encaminhar", "Apagar",
                                          "Dados da mensagem", "Reagir",
                                          "Fixar", "Favoritar"])
    def test_nunca_marca_item_perigoso(self, navegador, perigoso):
        """Match frouxo aqui manda dados de cliente para outra conversa."""
        achado, n, _ = self._achar(navegador, self._menu([perigoso]))
        assert achado == "" and n == 0, f"marcou {perigoso!r}"

    def test_responder_nao_casa_por_substring(self, navegador):
        """Responder não pode casar com "Responder em particular"."""
        _achado, n, texto = self._achar(
            navegador, self._menu(["Responder em particular", "Responder"]))
        assert n == 1 and texto == "Responder"

    def test_menu_sem_responder_devolve_vazio(self, navegador):
        achado, n, _ = self._achar(
            navegador, self._menu(["Copiar", "Encaminhar", "Apagar"]))
        assert achado == "" and n == 0


class TestGatilhoPorAriaLabel:
    """O botão do menu não tem `data-icon` — só `aria-label`."""

    def _marcar(self, navegador, corpo):
        pagina = navegador.new_page()
        try:
            pagina.set_content(
                f'<!doctype html><html><body><div id="main">{corpo}</div></body></html>')
            return _setinha(pagina, "2A48135636067914CA2B")
        finally:
            pagina.close()

    def test_acha_o_botao_pelo_aria_label(self, navegador):
        n, rotulos = self._marcar(
            navegador, _linha("2A48135636067914CA2B", "Ryan", "Ivone"))
        assert n == 1
        assert rotulos == ["Abrir opções de mensagem"]

    def test_descarta_o_botao_de_reagir(self, navegador):
        """Reagir também aparece no hover e abre o seletor de emoji."""
        _n, rotulos = self._marcar(
            navegador, _linha("2A48135636067914CA2B", "Ryan", "Ivone"))
        assert "Reagir" not in rotulos

    def test_sem_botao_devolve_zero(self, navegador):
        corpo = ('<div role="row"><div data-id="2A48135636067914CA2B">'
                 "<span>sem botao</span></div></div>")
        n, _ = self._marcar(navegador, corpo)
        assert n == 0


class TestInputDeImagemJaExiste:
    """Com a conversa aberta há UM input, `accept="image/*"`, sem abrir menu."""

    def test_um_unico_input_de_imagem(self, navegador):
        pagina = navegador.new_page()
        try:
            pagina.set_content('<!doctype html><html><body>'
                               '<input type="file" accept="image/*">'
                               "</body></html>")
            entradas = pagina.locator('input[type="file"]')
            assert entradas.count() == 1
            assert "image" in (entradas.first.get_attribute("accept") or "")
        finally:
            pagina.close()

    def test_sem_conversa_o_input_nao_serve(self, navegador):
        """Sem `#main` o input é `accept="*"` — não é o do chat."""
        pagina = navegador.new_page()
        try:
            pagina.set_content('<!doctype html><html><body>'
                               '<input type="file" accept="*" multiple>'
                               "</body></html>")
            entradas = pagina.locator('input[type="file"]')
            assert "image" not in (entradas.first.get_attribute("accept") or "")
        finally:
            pagina.close()

    def test_documento_e_o_primeiro_item_do_menu(self):
        """Por isso o menu saiu do caminho feliz: clique por índice pega ele."""
        assert MENU_ANEXO[0] == "Documento"

    def test_nunca_marcamos_documento(self, navegador):
        from app.whatsapp import WhatsAppService
        pagina = navegador.new_page()
        try:
            corpo = "".join(f"<div><span>{i}</span></div>" for i in MENU_ANEXO)
            pagina.set_content(f"<!doctype html><html><body>{corpo}</body></html>")
            pagina.evaluate(ACHAR_ITEM_MENU_JS, list(WhatsAppService._TEXTOS_FOTOS))
            marcado = pagina.locator(f"[{MARCA_ITEM}]")
            assert marcado.count() == 1
            assert marcado.first.inner_text().strip() == "Fotos e vídeos"
        finally:
            pagina.close()


class TestHeaderDoChatNaoDaListaLateral:
    """`document.querySelector('header')` pega o header da lista lateral."""

    PAGINA = ('<!doctype html><html><body>'
              '<header><span title="3 Atualizações no status">3 Atualizações</span></header>'
              '<div id="main">'
              f'<header><span title="{GRUPO}">{GRUPO}</span>'
              '<span title="Allana, Glaucon, Joselia, Ryan">Allana...</span></header>'
              '<div role="row"><div data-id="2A48135636"></div></div>'
              "</div></body></html>")

    def test_pega_o_titulo_do_chat(self, navegador):
        pagina = navegador.new_page()
        try:
            pagina.set_content(self.PAGINA)
            info = pagina.evaluate(CHAT_INFO_JS)
            assert GRUPO in info["titulos"]
            assert "3 Atualizações no status" not in info["titulos"], (
                "pegou o header da lista lateral")
            assert info["temMain"] is True
        finally:
            pagina.close()

    def test_sem_conversa_nao_ha_titulo(self, navegador):
        pagina = navegador.new_page()
        try:
            pagina.set_content('<!doctype html><html><body><header>'
                               '<span title="3 Atualizações no status">x</span>'
                               "</header></body></html>")
            info = pagina.evaluate(CHAT_INFO_JS)
            assert info["titulos"] == []
            assert info["temMain"] is False
        finally:
            pagina.close()


class TestConferenciaDoNomeProprio:
    """Descobrir no boot que o `BOT_SELF_NAME` está errado.

    Se o nome configurado não aparecer entre os autores da tela, a detecção de
    autoria perde o sinal forte e cai para prefixo do id e recibo — que são
    fracos nesta instalação. O bot pode voltar a ler as próprias respostas, e
    foi isso que encheu o grupo com 53 mensagens numa noite. Uma linha de log
    no boot custa nada; descobrir em produção custa o grupo do cliente.
    """

    def _autores(self, navegador, corpo: str):
        from app.whatsapp import AUTORES_VISIVEIS_JS
        pagina = navegador.new_page()
        try:
            pagina.set_content(
                f'<!doctype html><html><body><div id="main">{corpo}</div></body></html>')
            return pagina.evaluate(AUTORES_VISIVEIS_JS)
        finally:
            pagina.close()

    CONVERSA = (
        '<div data-pre-plain-text="[20:14, 30/08/2026] Ryan: ">a</div>'
        f'<div data-pre-plain-text="[20:17, 30/08/2026] {EU}: ">b</div>'
        '<div data-pre-plain-text="[20:20, 30/08/2026] Tobias Testão: ">c</div>'
        '<div data-pre-plain-text="[20:22, 30/08/2026] Ryan: ">d</div>'
    )

    def test_extrai_os_autores_sem_repetir(self, navegador):
        autores = self._autores(navegador, self.CONVERSA)
        assert autores == ["Ryan", EU, "Tobias Testão"]

    def test_ignora_linha_sem_marca(self, navegador):
        autores = self._autores(navegador, self.CONVERSA + "<div>sem marca</div>")
        assert len(autores) == 3

    def test_conversa_vazia_devolve_lista_vazia(self, navegador):
        assert self._autores(navegador, "<div>nada</div>") == []

    def test_o_nome_configurado_e_encontrado(self, navegador):
        from app.whatsapp import _normalizar
        autores = self._autores(navegador, self.CONVERSA)
        assert any(_normalizar(a) == _normalizar(EU) for a in autores)

    def test_nome_errado_nao_e_encontrado(self, navegador):
        """O caso que a verificação existe para pegar."""
        from app.whatsapp import _normalizar
        autores = self._autores(navegador, self.CONVERSA)
        assert not any(_normalizar(a) == _normalizar("Allana") for a in autores)

    def test_a_comparacao_tolera_acento_e_espaco(self, navegador):
        """`Tobias  Testão` com espaço duplo é o mesmo nome."""
        from app.whatsapp import _normalizar
        autores = self._autores(navegador, self.CONVERSA)
        assert any(_normalizar(a) == _normalizar("tobias  testão") for a in autores)

    def test_a_conferencia_roda_uma_vez_so(self):
        """Erro repetido a cada 3 segundos vira ruído e esconde o resto."""
        import inspect

        from app.whatsapp import WhatsAppService
        fonte = inspect.getsource(WhatsAppService._conferir_nome_proprio)
        assert "_nome_proprio_conferido" in fonte
        assert fonte.index("if self._nome_proprio_conferido") < fonte.index("_log")

    def test_sem_autores_tenta_de_novo_depois(self):
        """Tela ainda carregando não pode marcar como conferido."""
        import inspect

        from app.whatsapp import WhatsAppService
        fonte = inspect.getsource(WhatsAppService._conferir_nome_proprio)
        i = fonte.index("if not autores:")
        assert "return" in fonte[i:i + 120]
        assert fonte.index("self._nome_proprio_conferido = True") > i, (
            "marcou como conferido antes de ter o que conferir")


class TestCabecalhoOndeSoOsParticipantesTemTitle:
    """O header real: o nome do grupo NÃO é um `span[title]`.

    Observado em produção depois de trocar para `#main header`: só a lista de
    participantes carrega o atributo `title`; o nome do grupo aparece apenas
    como texto. Lendo só os `span[title]`, o bot concluía que estava sempre na
    conversa errada e reabria o grupo A CADA 3 SEGUNDOS, sem nunca processar
    mensagem nenhuma. Parou de responder por isso.
    """

    HEADER_REAL = (
        '<!doctype html><html><body>'
        '<header><span title="3 Atualizações no status">3 Atualizações</span></header>'
        '<div id="main"><header>'
        f'<div><span>{GRUPO}</span></div>'
        '<div><span title="Allana, Glaucon, Joselia, Ryan, Tobias">Allana...</span></div>'
        '</header><div role="row"><div data-id="2A48"></div></div></div>'
        "</body></html>")

    def test_acha_o_grupo_mesmo_sem_atributo_title(self, navegador):
        pagina = navegador.new_page()
        try:
            pagina.set_content(self.HEADER_REAL)
            info = pagina.evaluate(CHAT_INFO_JS)
            assert GRUPO in info["titulos"], (
                f"não achou o nome do grupo em {info['titulos']}")
        finally:
            pagina.close()

    def test_continua_ignorando_a_lista_lateral(self, navegador):
        pagina = navegador.new_page()
        try:
            pagina.set_content(self.HEADER_REAL)
            info = pagina.evaluate(CHAT_INFO_JS)
            assert "3 Atualizações no status" not in info["titulos"]
        finally:
            pagina.close()

    def test_o_servico_escolhe_o_grupo_e_nao_os_participantes(self, navegador, tmp_path):
        """A prova do laço: `_escolher_titulo` tem de devolver o grupo."""
        from app.state_store import StateStore
        from app.whatsapp import WhatsAppService

        pagina = navegador.new_page()
        try:
            pagina.set_content(self.HEADER_REAL)
            titulos = pagina.evaluate(CHAT_INFO_JS)["titulos"]
        finally:
            pagina.close()

        s = WhatsAppService(profile_dir=tmp_path / "p", group_name=GRUPO,
                            state=StateStore(tmp_path / "s.json"), headless=True)
        assert s._escolher_titulo(titulos) == GRUPO, (
            "devolveu os participantes — é o que fazia o bot reabrir o grupo "
            "a cada 3 segundos")

    def test_sem_conversa_nao_inventa_titulo(self, navegador):
        pagina = navegador.new_page()
        try:
            pagina.set_content('<!doctype html><html><body><header>'
                               '<span title="3 Atualizações">x</span></header></body></html>')
            info = pagina.evaluate(CHAT_INFO_JS)
            assert info["titulos"] == [] and info["temMain"] is False
        finally:
            pagina.close()


class TestTodoJSCompilaNumNavegador:
    """Erro de sintaxe em JS não pode chegar a produção.

    Já aconteceu três vezes neste arquivo: um `\n` escrito numa string Python
    normal vira quebra de linha REAL dentro do JavaScript, e o trecho inteiro
    deixa de rodar. Nas três a falha era silenciosa — a função só devolvia
    nada, e o defeito parecia ser de seletor.
    """

    def _constantes(self):
        import app.whatsapp as w
        return [(n, getattr(w, n)) for n in dir(w)
                if n.endswith("_JS") and isinstance(getattr(w, n), str)]

    def test_ha_o_que_testar(self):
        assert len(self._constantes()) >= 8

    def test_cada_trecho_roda_numa_pagina_com_conteudo(self, navegador):
        """Página vazia esconde erros que só aparecem ao percorrer elementos."""
        pagina = navegador.new_page()
        try:
            pagina.set_content(
                '<!doctype html><html><body>'
                '<header><span title="lateral">lateral</span></header>'
                '<input type="file" accept="image/*">'
                f'<div id="main"><header><span>{GRUPO}</span></header>'
                + _linha("2A48135636067914CA2B", "Ryan", "Ivone Teste")
                + '</div><footer><div contenteditable="true" data-tab="10"></div>'
                '<span data-icon="plus-rounded" aria-label="Anexar"></span></footer>'
                "</body></html>")
            falhas = []
            for nome, js in self._constantes():
                try:
                    pagina.evaluate(js, "2A48135636067914CA2B")
                except Exception as exc:
                    if "SyntaxError" in str(exc):
                        falhas.append(f"{nome}: {str(exc)[:120]}")
            assert not falhas, "JS inválido: " + " | ".join(falhas)
        finally:
            pagina.close()

    def test_cada_trecho_devolve_valor_em_vez_de_explodir(self, navegador):
        """Rodar sem erro não basta: um trecho pode devolver `undefined`.

        Tentei antes checar aspas desbalanceadas por regex, e o resultado foi
        falso positivo em todo comentário em português ("e'", "so'"). A prova
        que vale é esta: executar e olhar o retorno.
        """
        pagina = navegador.new_page()
        try:
            pagina.set_content(
                '<!doctype html><html><body>'
                f'<div id="main"><header><span>{GRUPO}</span></header>'
                + _linha("2A48135636067914CA2B", "Ryan", "Ivone Teste")
                + "</div></body></html>")
            assert pagina.evaluate(CHAT_INFO_JS)["titulos"], "cabeçalho vazio"
            assert pagina.evaluate(READ_MESSAGES_JS, [30, EU]), "não leu a mensagem"
            assert _quantas_setinhas(pagina, "2A48135636067914CA2B") == 1
        finally:
            pagina.close()


class TestEscapeNaoPodeFecharAConversa:
    """No WhatsApp Web, Escape sem nada aberto FECHA A CONVERSA.

    Foi a causa de REQ000034 e REQ000035 saírem sem citação e sem imagem.
    A sequência no log, com sete segundos de diferença:

        22:46:21  citação falhou — linha_encontrada=True   (conversa ABERTA)
        22:46:28  anexo falhou   — itens=['Tudo','Não lidas'...]  (tela INICIAL)

    Entre as duas, a limpeza pressionou Escape três vezes. A conversa fechou,
    e o anexo passou a ver só o input de documento da tela inicial
    (`accept="*"`) — por isso a imagem nunca saía como foto.
    """

    def _tem_o_que_fechar(self, navegador, corpo: str) -> bool:
        from app.whatsapp import TEM_O_QUE_FECHAR_JS
        pagina = navegador.new_page()
        try:
            pagina.set_content(f"<!doctype html><html><body>{corpo}</body></html>")
            return pagina.evaluate(TEM_O_QUE_FECHAR_JS)
        finally:
            pagina.close()

    def test_conversa_normal_nao_tem_o_que_fechar(self, navegador):
        """O caso que causava o estrago: Escape aqui fecharia a conversa."""
        assert self._tem_o_que_fechar(navegador, (
            '<div id="main"><div role="row">mensagem</div></div>'
            '<footer><div contenteditable="true"></div></footer>')) is False

    @pytest.mark.parametrize("aberto", [
        '<div role="menu"><li>Responder</li></div>',
        '<div role="dialog">Selecionar conversas</div>',
        '<img src="blob:xyz">',
    ])
    def test_reconhece_o_que_de_fato_esta_aberto(self, navegador, aberto):
        assert self._tem_o_que_fechar(navegador, f'<div id="main"></div>{aberto}') is True

    def test_imagem_dentro_de_mensagem_nao_e_preview(self, navegador):
        """Uma foto já enviada na conversa não é um preview aberto."""
        assert self._tem_o_que_fechar(navegador, (
            '<div id="main"><div role="row"><img src="blob:xyz"></div></div>')) is False

    def test_o_escape_confere_antes_de_pressionar(self):
        import inspect

        from app.whatsapp import WhatsAppService
        for metodo in ("_escape", "_limpar_ui", "_limpar_preview"):
            fonte = inspect.getsource(getattr(WhatsAppService, metodo))
            assert "TEM_O_QUE_FECHAR_JS" in fonte, (
                f"{metodo} pressiona Escape sem conferir — fecha a conversa")

    def test_o_anexo_recusa_sem_conversa_aberta(self):
        """Sem `#main`, o único input é o de documento da tela inicial."""
        import inspect

        from app.whatsapp import WhatsAppService
        fonte = inspect.getsource(WhatsAppService._anexar_imagem)
        assert '"#main"' in fonte
        assert fonte.index('"#main"') < fonte.index("COLAR_IMAGEM_JS"), (
            "a checagem tem de vir antes de tentar anexar")

    def test_a_conversa_e_reconferida_antes_do_anexo(self):
        """A citação mexe na tela; entre ela e o anexo a conversa pode fechar."""
        import inspect

        from app.whatsapp import WhatsAppService
        fonte = inspect.getsource(WhatsAppService._enviar_imagem)
        assert fonte.count("_garantir_conversa") >= 2, (
            "só verifica no início; a citação acontece depois")


class TestOBotaoDeAcaoPrecisaDeTempoParaAparecer:
    """A causa da citação nunca funcionar: o bot chegava cedo demais.

    O `ferramentas/dump_dom.py` -- a única coisa que já encontrou o botão de
    opções NESTA instalação -- passava o mouse e dormia um segundo antes de
    olhar. O bot passava o mouse e procurava no mesmo instante.

    O WhatsApp renderiza a seta de contexto no `mouseenter`, depois que o
    React reage. Procurar imediatamente encontra a bolha sem botão nenhum --
    e o diagnóstico registrava exatamente isso, todas as vezes:
    ``ícones=['tail-in'] rótulos=[] botões=['Ryan']``.

    Estes testes usam DOM real com o mesmo atraso, porque o defeito é de
    TEMPO: um teste com o botão já presente passaria mesmo com o bug.
    """

    HTML = """
    <div id="main">
      <div role="row"><div data-id="2AAAA">
        <span class="selectable-text">Ivone Teste 42888832453</span>
      </div></div>
    </div>
    <script>
      // Reproduz o comportamento observado: o botão só existe depois do
      // mouseenter, e com atraso -- como o React monta.
      const linha = document.querySelector('[role="row"]');
      linha.addEventListener('mouseenter', () => {
        setTimeout(() => {
          const b = document.createElement('button');
          b.setAttribute('aria-label', 'Abrir opções de mensagem');
          b.textContent = 'v';
          linha.appendChild(b);
        }, 600);
      });
    </script>
    """

    def test_procurar_na_hora_nao_encontra_nada(self, navegador):
        """O comportamento ANTIGO, provando que o defeito era real."""
        pagina = navegador.new_page()
        pagina.set_content(self.HTML)
        pagina.locator('[role="row"]').first.hover()
        # Sem espera nenhuma, como o bot fazia.
        assert pagina.locator('[aria-label="Abrir opções de mensagem"]').count() == 0

    def test_esperando_o_botao_ele_aparece(self, navegador):
        pagina = navegador.new_page()
        pagina.set_content(self.HTML)
        pagina.locator('[role="row"]').first.hover()
        pagina.wait_for_selector('[aria-label="Abrir opções de mensagem"]', timeout=2500)
        assert pagina.locator('[aria-label="Abrir opções de mensagem"]').count() == 1

    def test_o_laco_de_espera_do_bot_encontra(self, navegador):
        """A espera real do bot, com o JS real, contra este DOM."""
        import time as _t

        pagina = navegador.new_page()
        pagina.set_content(self.HTML)
        pagina.locator('[role="row"]').first.hover()

        limite = _t.monotonic() + 2.5
        quantos = 0
        while True:
            quantos = int(_quantas_setinhas(pagina, "2AAAA") or 0)
            if quantos or _t.monotonic() >= limite:
                break
            pagina.wait_for_timeout(120)
        assert quantos >= 1, "a espera do bot tem de achar o botão que só aparece depois"

    def test_a_espera_termina_cedo_quando_o_botao_ja_existe(self, navegador):
        """Não pode virar um sleep fixo: 2,5 s por mensagem seria caro."""
        import time as _t

        pagina = navegador.new_page()
        pagina.set_content("""
        <div id="main"><div role="row"><div data-id="2AAAA">
          <span class="selectable-text">oi</span>
          <button aria-label="Abrir opções de mensagem">v</button>
        </div></div></div>
        """)
        comeco = _t.monotonic()
        assert int(_quantas_setinhas(pagina, "2AAAA") or 0) >= 1
        assert _t.monotonic() - comeco < 1.0, "achou na hora, não pode esperar"

    def test_sem_botao_nenhum_a_espera_desiste(self, navegador):
        """E aí a evidência vale: o botão não existe mesmo."""
        import time as _t

        pagina = navegador.new_page()
        pagina.set_content("""
        <div id="main"><div role="row"><div data-id="2AAAA">
          <span class="selectable-text">oi</span>
        </div></div></div>
        """)
        limite = _t.monotonic() + 1.0
        while _t.monotonic() < limite:
            if int(_quantas_setinhas(pagina, "2AAAA") or 0):
                raise AssertionError("não devia achar botão onde não há")
            pagina.wait_for_timeout(120)


class TestOCampoDaLegendaNaoEhOCompositor:
    """O card saía mudo: 40 falhas em 52 imagens, desde 30/08.

    Com o preview aberto há DOIS campos de texto na tela, e o código escolhia
    por posição (`.last`), acertando sempre o compositor da conversa — que
    fica atrás do preview. Digitava lá, o texto não chegava ao card, e o log
    dizia "imagem ok".

    O que o laboratório observou no WhatsApp real:

        [0] aria="Digite uma mensagem"                  <- A LEGENDA
            sem data-tab, fora do footer, fora do #main, com foco
        [1] aria="Digite uma mensagem para o grupo X"   <- o compositor
            data-tab="10", dentro do footer, dentro do #main

    A diferença entre os dois `aria-label` é o SUFIXO. Estes testes montam a
    mesma armadilha — o compositor PRIMEIRO no DOM — para garantir que a
    escolha é por característica e nunca por ordem.
    """

    GRUPO = "Santander Capital Simulações"

    # O compositor vem antes de propósito: se a função voltar a escolher por
    # posição, `.first` pega o compositor e `.last` pega a legenda — os dois
    # por acaso, e este teste continua sendo o que denuncia.
    DOIS_CAMPOS = f"""
    <div id="main">
      <footer>
        <div contenteditable="true" data-tab="10" role="textbox"
             aria-label="Digite uma mensagem para o grupo {GRUPO}"></div>
      </footer>
    </div>
    <div class="preview">
      <div contenteditable="true" role="textbox" data-tab="undefined"
           aria-label="Digite uma mensagem"></div>
    </div>
    """

    def _escolher(self, navegador, corpo: str):
        from app.whatsapp import CAMPO_DA_LEGENDA_JS, WhatsAppService

        pagina = navegador.new_page()
        try:
            pagina.set_content(
                "<!doctype html><html><body>"
                "<style>[contenteditable]{display:block;min-width:200px;min-height:20px}</style>"
                f"{corpo}</body></html>")
            return pagina.evaluate(CAMPO_DA_LEGENDA_JS,
                                   list(WhatsAppService._ROTULOS_DA_LEGENDA))
        finally:
            pagina.close()

    def test_escolhe_a_legenda_e_nao_o_compositor(self, navegador):
        r = self._escolher(navegador, self.DOIS_CAMPOS)
        assert r["achou"] is True
        assert r["aria"] == "Digite uma mensagem", (
            "escolheu o compositor da conversa — o card sairia mudo")
        assert r["indice"] == 1, "o compositor é o índice 0 neste DOM"

    def test_o_sufixo_e_o_que_separa_os_dois(self, navegador):
        """`includes` casaria com os dois; só igualdade separa."""
        r = self._escolher(navegador, self.DOIS_CAMPOS)
        arias = [c["aria"] for c in r["inventario"]]
        assert f"Digite uma mensagem para o grupo {self.GRUPO}" in arias
        assert "Digite uma mensagem" in arias
        assert r["aria"] == "Digite uma mensagem"

    def test_sem_preview_nao_ha_legenda(self, navegador):
        """Só o compositor na tela: abortar é melhor que digitar no lugar errado."""
        so_compositor = f"""
        <div id="main"><footer>
          <div contenteditable="true" data-tab="10"
               aria-label="Digite uma mensagem para o grupo {self.GRUPO}"></div>
        </footer></div>"""
        r = self._escolher(navegador, so_compositor)
        assert r["achou"] is False
        assert r["quantos"] == 0
        assert r["inventario"], "o inventário tem de vir junto para explicar"

    def test_dois_candidatos_iguais_abortam(self, navegador):
        """Ambíguo é o mesmo que não encontrado: não se escolhe no palpite."""
        duplicado = """
        <div class="a"><div contenteditable="true" aria-label="Digite uma mensagem"></div></div>
        <div class="b"><div contenteditable="true" aria-label="Digite uma mensagem"></div></div>"""
        r = self._escolher(navegador, duplicado)
        assert r["achou"] is False
        assert r["quantos"] == 2

    def test_campo_dentro_do_footer_nunca_e_legenda(self, navegador):
        """Mesmo com o aria-label certo: a legenda vive fora do footer."""
        dentro = """
        <div id="main"><footer>
          <div contenteditable="true" aria-label="Digite uma mensagem"></div>
        </footer></div>"""
        assert self._escolher(navegador, dentro)["achou"] is False

    def test_data_tab_numerico_nunca_e_legenda(self, navegador):
        """O compositor tem `data-tab="10"`; a legenda tem `"undefined"`."""
        dentro = """
        <div class="preview">
          <div contenteditable="true" data-tab="10" aria-label="Digite uma mensagem"></div>
        </div>"""
        assert self._escolher(navegador, dentro)["achou"] is False

    def test_data_tab_undefined_continua_sendo_legenda(self, navegador):
        """O valor REAL observado. Exigir o atributo ausente recusava tudo.

        A primeira versão desta regra pedia `data-tab === null` e nenhum dos
        dois campos passava — o envio de imagem abortaria sempre. O
        laboratório pegou antes de ir para produção.
        """
        real = """
        <div class="preview">
          <div contenteditable="true" data-tab="undefined"
               aria-label="Digite uma mensagem"></div>
        </div>"""
        r = self._escolher(navegador, real)
        assert r["achou"] is True, "o data-tab='undefined' real foi recusado"

    def test_sem_data_tab_nenhum_tambem_serve(self, navegador):
        sem = """
        <div class="preview">
          <div contenteditable="true" aria-label="Digite uma mensagem"></div>
        </div>"""
        assert self._escolher(navegador, sem)["achou"] is True

    def test_campo_invisivel_nao_conta(self, navegador):
        escondido = """
        <div class="preview">
          <div contenteditable="true" aria-label="Digite uma mensagem"
               style="display:none"></div>
        </div>"""
        assert self._escolher(navegador, escondido)["achou"] is False

    def test_o_seletor_morto_nao_volta_para_achar_o_campo(self):
        """`media-caption-input-container` NÃO serve para achar a legenda.

        O contêiner existe (uma sonda posterior o encontrou na lista de
        `data-testid` do preview), então proibir a string no arquivo inteiro
        era exagero — ele é um sinal legítimo de "o preview está aberto", e é
        assim que `PREVIEW_ABERTO_JS` o usa.

        O que não pode voltar é procurar o CAMPO por ele: o contêiner não tem
        `contenteditable` dentro, e era isso que fazia a cascata cair no
        compositor da conversa e o card sair mudo.
        """
        import inspect

        from app.whatsapp import CAMPO_DA_LEGENDA_JS, WhatsAppService

        assert "media-caption-input-container" not in CAMPO_DA_LEGENDA_JS
        fonte = inspect.getsource(WhatsAppService._digitar_legenda)
        assert "media-caption-input-container" not in fonte

    def test_a_escolha_nao_usa_posicao(self):
        """`.last` sobre contenteditable foi a causa raiz."""
        import inspect

        from app.whatsapp import WhatsAppService

        # A docstring do método CITA ".last" ao explicar o defeito — varrer o
        # texto inteiro acusaria a própria explicação. Só o código conta.
        import ast
        import textwrap

        fonte = textwrap.dedent(inspect.getsource(WhatsAppService._digitar_legenda))
        arvore = ast.parse(fonte).body[0]
        if (arvore.body and isinstance(arvore.body[0], ast.Expr)
                and isinstance(arvore.body[0].value, ast.Constant)):
            arvore.body.pop(0)          # fora a docstring
        codigo = ast.unparse(arvore)

        assert ".last" not in codigo, "voltou a escolher por posição"
        assert "CAMPO_DA_LEGENDA_JS" in codigo


class TestODiagnosticoNaoPodeMexerNaTela:
    """O diagnóstico apagava a prova que ia colher.

    Antes de olhar a tela, ele apertava **Escape**. Com nada aberto, Escape
    fecha a conversa no WhatsApp Web — então ele destruía o estado e descrevia
    os escombros. O log de produção dizia::

        linha_encontrada=False ícones=[] rótulos=[] botões=[] menus=[]

    e eu li isso por várias rodadas como "a mensagem sumiu da tela", quando
    era o próprio diagnóstico que tinha acabado de fechar a conversa. Em
    produção isso acontecia no meio do envio, obrigando o passo seguinte a
    reabrir a conversa.

    A regra que sai daí, e vale para qualquer instrumentação: **medir não
    pode alterar o que se mede.**
    """

    #: Tudo que altera a página. Nenhum destes pode aparecer no diagnóstico.
    ACOES = ("keyboard.press", "keyboard.insert_text", ".click(",
             "mouse.click", "mouse.move", "scrollIntoView", ".fill(",
             ".type(", ".hover(")

    def _codigo_sem_docstring(self, funcao) -> str:
        import ast
        import inspect
        import textwrap

        arvore = ast.parse(textwrap.dedent(inspect.getsource(funcao))).body[0]
        if (arvore.body and isinstance(arvore.body[0], ast.Expr)
                and isinstance(arvore.body[0].value, ast.Constant)):
            arvore.body.pop(0)
        return ast.unparse(arvore)

    @pytest.mark.parametrize("nome", ["_diagnosticar_citacao", "_capturar_estado",
                                      "_diagnosticar_citacao_uma_vez"])
    def test_e_somente_leitura(self, nome):
        from app.whatsapp import WhatsAppService

        codigo = self._codigo_sem_docstring(getattr(WhatsAppService, nome))
        culpados = [a for a in self.ACOES if a in codigo]
        assert not culpados, (
            f"{nome} altera a tela ({culpados}) — o diagnóstico voltaria a "
            "apagar a prova que vai colher")

    def test_a_captura_acontece_antes_da_limpeza(self):
        """Fotografar depois de limpar é fotografar outra coisa."""
        codigo = self._codigo_sem_docstring(
            __import__("app.whatsapp", fromlist=["x"]).WhatsAppService._passos_da_citacao)

        onde_captura = codigo.find("_capturar_estado")
        onde_limpa = codigo.find("_limpar_ui")
        assert onde_captura != -1, "os pontos de falha não capturam nada"
        if onde_limpa != -1:
            assert onde_captura < onde_limpa, (
                "a limpeza vem antes da captura — a foto sai da tela já limpa")

    def test_todo_ponto_de_falha_captura(self):
        """Um caminho sem captura vira "falhou" sem nenhuma explicação.

        São três pontos hoje — PASSO 1 (achar a linha), PASSO 4 (nenhuma via
        abriu o menu com "Responder") e PASSO 5 (a barra não apareceu). Eram
        quatro antes de as duas falhas de menu virarem uma só, quando a
        segunda via passou a ser tentada sempre.
        """
        codigo = self._codigo_sem_docstring(
            __import__("app.whatsapp", fromlist=["x"]).WhatsAppService._passos_da_citacao)
        quantos = codigo.count("_capturar_estado")
        assert quantos >= 3, f"só {quantos} pontos de falha capturam a tela"

        for passo in ("PASSO 1", "PASSO 4", "PASSO 5"):
            assert passo in codigo, f"{passo} não captura a tela ao falhar"

    def test_a_foto_e_limpa_a_cada_tentativa(self):
        """Senão a segunda falha mostraria a tela da primeira."""
        codigo = self._codigo_sem_docstring(
            __import__("app.whatsapp", fromlist=["x"]).WhatsAppService._try_quote)
        assert "_estado_da_citacao = None" in codigo


class TestMenuDeVerdadeTemTamanhoDeMenu:
    """"menu aberto: True" e "itens na tela: []" ao mesmo tempo.

    `MENU_ABERTO_JS` aceitava qualquer `[role="menu"]` com largura e altura
    não-nulas. O seletor de reações do WhatsApp também tem `role="menu"`,
    aparece no mesmo hover, e mede **4 por 1 pixel**. O código dava o menu
    por aberto, procurava "Responder" dentro dele e não achava nada — e o
    log levava a investigação para "o item mudou de nome", quando o menu da
    mensagem nem tinha aberto.

    Um menu com onze itens não cabe em 4x1.
    """

    def _menu(self, navegador, corpo: str) -> bool:
        from app.whatsapp import MENU_ABERTO_JS

        pagina = navegador.new_page()
        try:
            pagina.set_content(f"<!doctype html><html><body>{corpo}</body></html>")
            return bool(pagina.evaluate(MENU_ABERTO_JS))
        finally:
            pagina.close()

    def test_o_seletor_de_reacoes_nao_conta_como_menu(self, navegador):
        """4x1 pixel, com role=menu — o caso real observado."""
        assert self._menu(navegador, """
            <div role="menu" style="width:4px;height:1px">ic-add</div>""") is False

    def test_menu_de_verdade_conta(self, navegador):
        assert self._menu(navegador, """
            <div role="menu" style="width:260px;height:420px">
              <div role="menuitem">Responder</div>
            </div>""") is True

    def test_menu_da_lateral_nunca_conta(self, navegador):
        """A lista de conversas fica sempre na tela."""
        assert self._menu(navegador, """
            <div id="pane-side">
              <div role="menu" style="width:400px;height:600px">conversas</div>
            </div>""") is False

    @pytest.mark.parametrize("w,h", [(119, 300), (300, 59), (0, 0), (4, 1)])
    def test_abaixo_do_piso_nao_conta(self, navegador, w, h):
        assert self._menu(navegador, f"""
            <div role="menu" style="width:{w}px;height:{h}px">x</div>""") is False


class TestABarraDeCitacaoEhADoCompositor:
    """Uma citação do HISTÓRICO foi lida como a barra armada.

    `[data-testid="quoted-message"]` existe em dois lugares: na barra acima do
    campo de digitação (a citação armada) e dentro de **mensagens da conversa
    que citam outras**. Pegar a primeira do documento lia uma citação de dias
    atrás e a tratava como estado atual — o log dizia "havia uma citação
    pendurada" apontando uma mensagem antiga, e não havia botão de cancelar
    porque não havia barra nenhuma.

    O que separa as duas: a barra do compositor não fica dentro de
    `div[role="row"]`.
    """

    HISTORICO = """
    <div id="main">
      <div role="row">
        <div data-testid="quoted-message" style="width:200px;height:40px">
          Victor Medeiros FLAVIO AUGUSTO
        </div>
        <span class="selectable-text">mensagem antiga</span>
      </div>
    </div>"""

    COMPOSITOR = """
    <footer>
      <div data-testid="quoted-message" style="width:400px;height:60px">
        Ryan LUCIANGELA TESTADO 72845554753
      </div>
      <button aria-label="Cancelar" style="width:20px;height:20px"></button>
      <div contenteditable="true" aria-label="Digite uma mensagem para o grupo X"></div>
    </footer>"""

    def _ativa(self, navegador, corpo: str, trecho: str) -> dict:
        from app.whatsapp import CITACAO_ATIVA_JS

        pagina = navegador.new_page()
        try:
            pagina.set_content(f"<!doctype html><html><body>{corpo}</body></html>")
            return pagina.evaluate(CITACAO_ATIVA_JS, trecho)
        finally:
            pagina.close()

    def test_citacao_do_historico_nao_e_a_barra(self, navegador):
        r = self._ativa(navegador, self.HISTORICO, "Victor Medeiros")
        assert r["ativa"] is False, "leu uma citação da conversa como barra armada"
        assert r["temBarra"] is False

    def test_a_barra_do_compositor_e_reconhecida(self, navegador):
        r = self._ativa(navegador, self.COMPOSITOR, "Ryan LUCIANGELA TESTADO")
        assert r["ativa"] is True
        assert r["local"] == "barra"

    def test_barra_de_outra_mensagem_e_recusada(self, navegador):
        """Citação armada na mensagem errada é pior que nenhuma."""
        r = self._ativa(navegador, self.COMPOSITOR, "Marcia Testadora 80390002704")
        assert r["ativa"] is False

    def test_historico_e_compositor_juntos(self, navegador):
        """O caso real: os dois na tela ao mesmo tempo."""
        r = self._ativa(navegador, self.HISTORICO + self.COMPOSITOR,
                        "Ryan LUCIANGELA TESTADO")
        assert r["ativa"] is True, "o histórico atrapalhou a leitura da barra"


class TestCancelarCitacaoOrfa:
    """Uma citação pendurada gruda a resposta seguinte na mensagem errada.

    Aconteceu no laboratório: a barra de uma volta sobreviveu à limpeza e as
    voltas seguintes leram "citação ativa" sem terem citado nada. Escape não
    fecha a barra — e apertá-lo com nada aberto fecha a conversa. O botão de
    cancelar é o único caminho.
    """

    def _pendente(self, navegador, corpo: str) -> dict:
        from app.whatsapp import CITACAO_PENDENTE_JS

        pagina = navegador.new_page()
        try:
            pagina.set_content(f"<!doctype html><html><body>{corpo}</body></html>")
            return pagina.evaluate(CITACAO_PENDENTE_JS)
        finally:
            pagina.close()

    def test_acha_a_barra_e_o_botao(self, navegador):
        r = self._pendente(navegador, TestABarraDeCitacaoEhADoCompositor.COMPOSITOR)
        assert r["pendente"] is True
        assert r["temBotao"] is True
        assert r["x"] > 0 and r["y"] > 0

    def test_sem_barra_nao_ha_pendencia(self, navegador):
        r = self._pendente(navegador, """
            <footer><div contenteditable="true"></div></footer>""")
        assert r["pendente"] is False

    def test_citacao_do_historico_nao_vira_pendencia(self, navegador):
        r = self._pendente(navegador, TestABarraDeCitacaoEhADoCompositor.HISTORICO)
        assert r["pendente"] is False

    def test_o_botao_e_procurado_perto_da_barra(self, navegador):
        """Com o preview aberto a barra sai do footer, e o botão vai junto."""
        fora_do_footer = """
        <div class="preview">
          <div class="painel">
            <div data-testid="quoted-message" style="width:300px;height:50px">Ryan</div>
            <button aria-label="Cancelar" style="width:20px;height:20px"></button>
          </div>
        </div>"""
        r = self._pendente(navegador, fora_do_footer)
        assert r["pendente"] is True
        assert r["temBotao"] is True, (
            "procurou só dentro do footer — com o preview aberto a barra não está lá")


class TestOPreviewDeMidiaTemSinalProprio:
    """`img[src^="blob:"]` NAO indica preview aberto.

    Toda imagem já enviada na conversa é servida como blob. Uma sonda chegou
    a reportar "preview ainda aberto: True" com a tela completamente limpa
    por causa disso — e o diagnóstico errado levou a conclusões erradas sobre
    a limpeza funcionar ou não.
    """

    def _aberto(self, navegador, corpo: str) -> bool:
        from app.whatsapp import PREVIEW_ABERTO_JS

        pagina = navegador.new_page()
        try:
            pagina.set_content(f"<!doctype html><html><body>{corpo}</body></html>")
            return bool(pagina.evaluate(PREVIEW_ABERTO_JS))
        finally:
            pagina.close()

    def test_imagem_enviada_na_conversa_nao_e_preview(self, navegador):
        assert self._aberto(navegador, """
            <div role="row"><img src="blob:https://web.whatsapp.com/abc"></div>""") is False

    def test_drawer_permanente_nao_e_preview(self, navegador):
        assert self._aberto(navegador, """
            <div data-testid="drawer-fullscreen"></div>""") is False

    @pytest.mark.parametrize("sinal", [
        '<div data-testid="media-editor-canvas"></div>',
        '<div data-testid="media-caption-input-container"></div>',
        '<div aria-label="Enviar 1 item selecionado"></div>',
    ])
    def test_os_sinais_reais_contam(self, navegador, sinal):
        assert self._aberto(navegador, sinal) is True


class TestEscapeNaoFechaOPreviewSozinho:
    """Escape abre "Deseja descartar a seleção?" — não fecha o preview.

    `_limpar_preview` apertava Escape DUAS vezes: a primeira abria o diálogo,
    a segunda o **cancelava**, e o preview voltava intacto. O método parecia
    funcionar e não fechava nada — e um preview sobrevivente contamina o
    envio seguinte.
    """

    DIALOGO = """
    <div role="dialog" style="width:300px;height:150px">
      Deseja descartar a seleção?
      <button style="width:80px;height:30px">Cancelar</button>
      <button style="width:80px;height:30px">Descartar</button>
    </div>"""

    def test_acha_o_botao_descartar(self, navegador):
        from app.whatsapp import DIALOGO_DESCARTAR_JS

        pagina = navegador.new_page()
        try:
            pagina.set_content(f"<!doctype html><html><body>{self.DIALOGO}</body></html>")
            r = pagina.evaluate(DIALOGO_DESCARTAR_JS)
            assert r["achou"] is True
            assert r["texto"] == "descartar"
            marcados = pagina.locator("[data-allana-descartar]")
            assert marcados.count() == 1
            assert marcados.first.inner_text().strip() == "Descartar"
        finally:
            pagina.close()

    def test_nunca_marca_o_cancelar(self, navegador):
        """Cancelar devolve o preview — é o oposto do que se quer."""
        from app.whatsapp import DIALOGO_DESCARTAR_JS

        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body>
              <div role="dialog" style="width:300px;height:150px">
                <button style="width:80px;height:30px">Cancelar</button>
              </div></body></html>""")
            assert pagina.evaluate(DIALOGO_DESCARTAR_JS)["achou"] is False
        finally:
            pagina.close()

    def test_limpar_preview_confirma_o_descarte(self):
        import ast
        import inspect
        import textwrap

        from app.whatsapp import WhatsAppService

        arvore = ast.parse(textwrap.dedent(
            inspect.getsource(WhatsAppService._limpar_preview))).body[0]
        if (arvore.body and isinstance(arvore.body[0], ast.Expr)
                and isinstance(arvore.body[0].value, ast.Constant)):
            arvore.body.pop(0)
        codigo = ast.unparse(arvore)
        assert "_confirmar_descarte" in codigo, (
            "voltou a apertar Escape sem confirmar o descarte")


class TestUmSoPontoDeDisparo:
    """`_disparar_envio` é o único lugar que faz a mensagem sair.

    Não é cerimônia: é o que permite ao laboratório rodar o caminho inteiro
    de produção — citar, colar, digitar a legenda — e parar exatamente ali,
    sem escrever no grupo do cliente. Antes disso, testar o fluxo completo
    significava mandar mensagem de teste para consultores de verdade.
    """

    def test_o_envio_de_imagem_dispara_por_ali(self):
        import inspect

        from app.whatsapp import WhatsAppService

        fonte = inspect.getsource(WhatsAppService._enviar_imagem)
        assert "_disparar_envio" in fonte
        assert 'keyboard.press("Enter")' not in fonte, (
            "voltou a apertar Enter direto — o laboratório perde o freio")

    def test_o_disparo_tem_o_botao_e_a_tecla(self):
        import inspect

        from app.whatsapp import WhatsAppService

        fonte = inspect.getsource(WhatsAppService._disparar_envio)
        assert "Enviar" in fonte
        assert "Enter" in fonte


class TestDuasViasParaAbrirOMenu:
    """A setinha é intermitente; o botão direito é estável.

    Em produção o clique na setinha abria o menu de forma inconsistente — o
    elemento entra animado e o WhatsApp às vezes ignora. Antes, o botão
    direito só era tentado quando a setinha **não era encontrada**; quando
    ela era encontrada e o clique falhava, a citação desistia ali.

    Com as duas vias julgadas pelo mesmo critério (o menu tem os ITENS?), o
    serviço real passou de 0/3 para 10/10.
    """

    def test_existem_as_duas_vias(self):
        from app.whatsapp import WhatsAppService

        assert hasattr(WhatsAppService, "_abrir_menu_pela_setinha")
        assert hasattr(WhatsAppService, "_abrir_menu_pelo_botao_direito")

    def test_a_segunda_via_e_tentada_quando_a_primeira_nao_entrega(self):
        import inspect

        from app.whatsapp import WhatsAppService

        fonte = inspect.getsource(WhatsAppService._passos_da_citacao)
        pos_setinha = fonte.find("_abrir_menu_pela_setinha")
        pos_direito = fonte.find("_abrir_menu_pelo_botao_direito")
        assert pos_setinha != -1 and pos_direito != -1
        assert pos_setinha < pos_direito, "a ordem das vias inverteu"
        assert 'if not item.get("achou")' in fonte

    def test_as_duas_julgam_pelos_itens(self):
        """"O menu abriu" não basta — já contamos um seletor de reações."""
        import inspect

        from app.whatsapp import WhatsAppService

        for nome in ("_abrir_menu_pela_setinha", "_abrir_menu_pelo_botao_direito"):
            fonte = inspect.getsource(getattr(WhatsAppService, nome))
            assert "_esperar_os_itens" in fonte, f"{nome} não confere os itens"

    def test_a_setinha_e_clicada_no_elemento(self):
        """Clicar na coordenada falhava: a setinha entra animada e se move."""
        import inspect

        from app.whatsapp import WhatsAppService

        fonte = inspect.getsource(WhatsAppService._abrir_menu_pela_setinha)
        assert "MARCA_SETINHA" in fonte
        assert "mouse.click" not in fonte, "voltou a clicar em coordenada"

    def test_o_botao_direito_mira_no_balao(self):
        """O centro de `role=row` cai no fundo vazio e abre o menu do GRUPO.

        A primeira versão deste teste exigia `linha["x"], linha["y"]` no
        código — o alvo certo quando a mira era por coordenada. Depois da
        enxurrada de 01/09 a coordenada deixou de servir (ela envelhece
        enquanto a conversa rola), e o clique passou a ser no BALÃO MARCADO.
        O alvo continua o mesmo; o jeito de acertá-lo é que mudou.
        """
        import inspect

        from app.whatsapp import WhatsAppService

        fonte = inspect.getsource(WhatsAppService._abrir_menu_pelo_botao_direito)
        assert "MARCA_BALAO" in fonte, "o alvo tem de ser o balão"
        assert 'button="right"' in fonte
        assert "_reancorar" in fonte, "a geometria tem de ser a do instante"


class TestReancorarAntesDeCadaClique:
    """59 mensagens em 2 minutos: a conversa rola debaixo dos pés do bot.

    O log de 01/09 mostrou os dois sintomas dessa condição:

        Rodapé sem a citação esperada: 'Allana testando 2.274'
        Citação falhou em: PASSO 4 ... linha_no_dom=False
        menus=['Adicionar membro  Dados do grupo  ...  Sair do grupo']

    A primeira é a citação armando na mensagem ERRADA; a segunda é o botão
    direito caindo no fundo vazio, onde o WhatsApp abre o menu do **grupo**.

    A causa era a mesma: `_abrir_menu_pelo_botao_direito` usava as coordenadas
    medidas antes do hover. Numa tela parada — todos os laboratórios — elas
    valiam; numa enxurrada, apontavam para lugar nenhum.
    """

    def _codigo(self, funcao) -> str:
        import ast
        import inspect
        import textwrap

        arvore = ast.parse(textwrap.dedent(inspect.getsource(funcao))).body[0]
        if (arvore.body and isinstance(arvore.body[0], ast.Expr)
                and isinstance(arvore.body[0].value, ast.Constant)):
            arvore.body.pop(0)
        return ast.unparse(arvore)

    def test_o_botao_direito_reancora_antes(self):
        from app.whatsapp import WhatsAppService

        codigo = self._codigo(WhatsAppService._abrir_menu_pelo_botao_direito)
        assert "_reancorar" in codigo
        assert codigo.index("_reancorar") < codigo.index("click"), (
            "reancorar depois do clique não serve para nada")

    def test_a_setinha_tambem_reancora(self):
        from app.whatsapp import WhatsAppService

        codigo = self._codigo(WhatsAppService._abrir_menu_pela_setinha)
        assert "_reancorar" in codigo

    def test_o_elemento_vem_antes_da_coordenada(self):
        """Coordenada é último recurso, e sempre remedida no instante."""
        from app.whatsapp import WhatsAppService

        codigo = self._codigo(WhatsAppService._abrir_menu_pelo_botao_direito)
        assert "MARCA_BALAO" in codigo, "voltou a clicar só por coordenada"
        assert codigo.index("MARCA_BALAO") < codigo.index("mouse.click")

    def test_a_coordenada_de_reserva_e_a_nova(self):
        """Cair para a coordenada ANTIGA seria repetir o defeito."""
        from app.whatsapp import WhatsAppService

        codigo = self._codigo(WhatsAppService._abrir_menu_pelo_botao_direito)
        assert "atual['x']" in codigo or 'atual["x"]' in codigo
        assert "linha['x']" not in codigo and 'linha["x"]' not in codigo, (
            "a coordenada de reserva tem de ser a remedida, não a de antes")

    def test_o_balao_e_marcado_para_o_clique(self, navegador):
        from app.whatsapp import GEOMETRIA_DA_LINHA_JS, MARCA_BALAO

        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body><div id="main">
              <div role="row" style="width:900px">
                <div class="copyable-text" data-id="2A1"
                     data-pre-plain-text="[10:00, 01/09/2026] Ryan: "
                     style="display:block;width:300px;height:60px">
                  <span class="selectable-text">Maria Tabaré</span>
                </div>
              </div></div></body></html>""")
            r = pagina.evaluate(GEOMETRIA_DA_LINHA_JS, ["2A1", "Maria Tabaré"])
            assert r["achou"] is True
            assert pagina.locator(f"[{MARCA_BALAO}]").count() == 1
        finally:
            pagina.close()

    def test_a_marca_do_balao_nao_sobra_da_mensagem_anterior(self, navegador):
        """Senão o clique iria para a mensagem citada na volta passada."""
        from app.whatsapp import GEOMETRIA_DA_LINHA_JS, MARCA_BALAO

        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body><div id="main">
              <div role="row"><div data-id="2A1" style="display:block;width:200px;height:40px">
                <span class="selectable-text">primeira</span></div></div>
              <div role="row"><div data-id="2A2" style="display:block;width:200px;height:40px">
                <span class="selectable-text">segunda</span></div></div>
              </div></body></html>""")
            pagina.evaluate(GEOMETRIA_DA_LINHA_JS, ["2A1", "primeira"])
            pagina.evaluate(GEOMETRIA_DA_LINHA_JS, ["2A2", "segunda"])
            marcados = pagina.locator(f"[{MARCA_BALAO}]")
            assert marcados.count() == 1, "sobrou a marca da mensagem anterior"
            assert "segunda" in marcados.first.inner_text()
        finally:
            pagina.close()

    def test_texto_parecido_em_duas_mensagens_nao_vira_citacao(self, navegador):
        """Citar "a última parecida" respondia o pedido de OUTRO consultor.

        Sem o data-id, a reserva pelo texto só vale se apontar UMA linha.
        """
        from app.whatsapp import GEOMETRIA_DA_LINHA_JS

        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body><div id="main">
              <div role="row"><div data-id="2AOUTRO" style="display:block;width:200px;height:40px">
                <span class="selectable-text">Maria Tabaré 11144477735</span></div></div>
              <div role="row"><div data-id="2ANOVO" style="display:block;width:200px;height:40px">
                <span class="selectable-text">Maria Tabaré 52998224725</span></div></div>
              </div></body></html>""")
            r = pagina.evaluate(GEOMETRIA_DA_LINHA_JS, ["id-que-mudou", "Maria Tabaré"])
            assert r["achou"] is False, "citou por aproximação"
            assert "2" in r["motivo"]

            unica = pagina.evaluate(GEOMETRIA_DA_LINHA_JS,
                                    ["id-que-mudou", "Maria Tabaré 52998224725"])
            assert unica["achou"] is True and unica["via"] == "texto"
        finally:
            pagina.close()

    def test_mensagem_que_sumiu_nao_vira_clique_no_vazio(self, navegador):
        """O menu do GRUPO abrindo foi o sintoma disso."""
        from app.whatsapp import GEOMETRIA_DA_LINHA_JS

        pagina = navegador.new_page()
        try:
            pagina.set_content('<!doctype html><html><body><div id="main"></div></body></html>')
            r = pagina.evaluate(GEOMETRIA_DA_LINHA_JS, ["2A1", "sumiu"])
            assert r["achou"] is False, (
                "sem a linha, clicar em coordenada abriria o menu do grupo")
        finally:
            pagina.close()


class TestOLogDizQualVia:
    """"Nenhuma via funcionou" fazia a investigação começar do zero."""

    def test_cada_via_se_identifica(self):
        import inspect

        from app.whatsapp import WhatsAppService

        for nome in ("_abrir_menu_pela_setinha", "_abrir_menu_pelo_botao_direito"):
            fonte = inspect.getsource(getattr(WhatsAppService, nome))
            assert '"via"' in fonte, f"{nome} não diz quem é no retorno"

    def test_o_erro_lista_o_que_cada_via_respondeu(self):
        import inspect

        from app.whatsapp import WhatsAppService

        fonte = inspect.getsource(WhatsAppService._passos_da_citacao)
        assert "tentativas" in fonte
        assert "Vias:" in fonte

    def test_a_entrega_registra_por_onde_citou(self):
        import inspect

        from app.whatsapp import WhatsAppService

        fonte = inspect.getsource(WhatsAppService._do_send_image)
        assert "_ultima_via_de_citacao" in fonte, (
            "o log de entrega tem de dizer qual via funcionou, não só 'ok'")
