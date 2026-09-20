"""A leitura de mensagens, contra HTML de verdade num navegador de verdade.

Motivo destes testes: o bot ficou uma noite inteira "conectado", com a fila
zerada, sem responder nada e sem UMA linha de log. O banco confirmou o
tamanho do buraco: ``messages`` com 0 linhas, ou seja, nenhuma mensagem lida
desde sempre. A leitura dependia de um unico seletor (``div.message-in``) e
saia em silencio quando ele nao casava.

Aqui o JS roda no Chromium sobre as duas gerações de HTML que o WhatsApp Web
ja' serviu, para que a proxima mudanca de classe quebre um teste em vez de
uma noite de trabalho.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from app.whatsapp import DIAGNOSTICO_LEITURA_JS, READ_MESSAGES_JS

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

GRUPO = "false_5562000@g.us"
EU = "true_5562000@g.us"


def _msg(data_id: str, classe: str, autor: str, texto: str) -> str:
    return f"""
    <div role="row">
      <div class="{classe}" data-id="{data_id}">
        <div class="copyable-text" data-pre-plain-text="[19:47, 29/08/2026] {autor}: ">
          <span class="selectable-text"><span>{texto}</span></span>
        </div>
      </div>
    </div>"""


def _pagina(corpo: str) -> str:
    return f"""<!doctype html><html><body>
    <header><span title="Santander Capital Simulações">Santander Capital Simulações</span></header>
    <div id="main">{corpo}</div></body></html>"""


# Geracao atual: classe .message-in presente E data-id no mesmo elemento.
COM_CLASSE = _pagina(
    _msg(f"{GRUPO}_AAA_5562111@c.us", "message-in", "Ryan", "Ivone Teste")
    + _msg(f"{EU}_BBB", "message-out", "Operacional Capital", "resposta do bot")
    + _msg(f"{GRUPO}_CCC_5562222@c.us", "message-in", "Tobias", "428.888.324-53")
)

# Geracao sem as classes message-in/out: so' o data-id distingue.
SEM_CLASSE = _pagina(
    _msg(f"{GRUPO}_AAA_5562111@c.us", "x1c4 focusable-list-item", "Ryan", "Ivone Teste")
    + _msg(f"{EU}_BBB", "x1c4 focusable-list-item", "Operacional Capital",
         "resposta do bot")
    + _msg(f"{GRUPO}_CCC_5562222@c.us", "x1c4 focusable-list-item", "Tobias", "428.888.324-53")
)

SO_ENVIADAS = _pagina(_msg(f"{EU}_BBB", "message-out", "Operacional Capital", "so eu falei"))

SEM_CONVERSA = """<!doctype html><html><body><div id="side"></div></body></html>"""


@pytest.fixture(scope="module")
def navegador():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


def _ler(navegador, html: str, limite: int = 30):
    pagina = navegador.new_page()
    try:
        pagina.set_content(html)
        return pagina.evaluate(READ_MESSAGES_JS, [limite, "Operacional Capital"])
    finally:
        pagina.close()


class TestLeitura:
    @pytest.mark.parametrize("html,rotulo", [(COM_CLASSE, "com classe"), (SEM_CLASSE, "sem classe")])
    def test_le_as_recebidas_nas_duas_geracoes(self, navegador, html, rotulo):
        lidas = _ler(navegador, html)
        assert [m["text"] for m in lidas] == ["Ivone Teste", "428.888.324-53"], rotulo

    @pytest.mark.parametrize("html", [COM_CLASSE, SEM_CLASSE])
    def test_nunca_le_o_que_nos_mesmos_enviamos(self, navegador, html):
        """Responder a propria resposta e' laco infinito."""
        assert all(not m["id"].startswith("true_") for m in _ler(navegador, html))
        assert all("resposta do bot" != m["text"] for m in _ler(navegador, html))

    def test_traz_o_data_pre_plain_text(self, navegador):
        primeira = _ler(navegador, COM_CLASSE)[0]
        assert primeira["meta"] == "[19:47, 29/08/2026] Ryan: "
        assert primeira["id"] == f"{GRUPO}_AAA_5562111@c.us"

    def test_nao_duplica_quando_o_data_id_aparece_aninhado(self, navegador):
        aninhado = _pagina(
            f'<div role="row" data-id="{GRUPO}_AAA_5562111@c.us">'
            + _msg(f"{GRUPO}_AAA_5562111@c.us", "message-in", "Ryan", "uma vez so")
            + "</div>"
        )
        assert len(_ler(navegador, aninhado)) == 1

    def test_respeita_o_limite(self, navegador):
        muitas = _pagina("".join(
            _msg(f"{GRUPO}_M{i}_5562111@c.us", "message-in", "Ryan", f"msg {i}")
            for i in range(40)))
        lidas = _ler(navegador, muitas, limite=30)
        assert len(lidas) == 30
        assert lidas[-1]["text"] == "msg 39", "tem de manter as MAIS RECENTES"

    def test_conversa_so_com_mensagens_nossas_devolve_vazio(self, navegador):
        assert _ler(navegador, SO_ENVIADAS) == []


class TestDiagnostico:
    """O diagnóstico existe para separar três causas com correções diferentes."""

    def _diag(self, navegador, html):
        pagina = navegador.new_page()
        try:
            pagina.set_content(html)
            return pagina.evaluate(DIAGNOSTICO_LEITURA_JS)
        finally:
            pagina.close()

    def test_sem_conversa_aberta(self, navegador):
        assert self._diag(navegador, SEM_CONVERSA)["main"] is False

    def test_conversa_so_nossa_e_distinguivel(self, navegador):
        d = self._diag(navegador, SO_ENVIADAS)
        assert d["main"] is True
        assert d["recebidas_dataid"] == 0 and d["enviadas_dataid"] == 1

    def test_relata_o_que_a_pagina_tem(self, navegador):
        d = self._diag(navegador, COM_CLASSE)
        assert d["conversa"] == "Santander Capital Simulações"
        assert d["recebidas_dataid"] == 2
        assert d["recebidas_classe"] == 2
        assert d["pre_plain"] == 3


# --------------------------------------------------------------- abrir conversa
GRUPO_NOME = "Santander Capital Simulações"


def _barra_lateral(titulos: list[str]) -> str:
    itens = "".join(
        f'<div role="listitem" tabindex="0"><span title="{t}">{t}</span></div>'
        for t in titulos
    )
    return f"""<!doctype html><html><body>
    <div id="side"><input type="text" aria-label="Pesquisar"></div>
    <div id="pane-side">{itens}</div></body></html>"""


class TestAcharConversaNaLista:
    """Achar o grupo na barra lateral — o passo que falhou cinco vezes seguidas.

    A abertura passou a comparar NORMALIZADO em vez de usar [title="..."],
    porque o WhatsApp pode servir 'ç' decomposto (c + cedilha combinante)
    enquanto o .env tem a forma composta: strings diferentes byte a byte,
    seletor que nunca casa, e nenhum sintoma além de "não achei".
    """

    def _achar(self, navegador, html: str, nome: str):
        from app.whatsapp import ACHAR_CONVERSA_JS, MARCA_ALVO
        pagina = navegador.new_page()
        try:
            pagina.set_content(html)
            achou = pagina.evaluate(ACHAR_CONVERSA_JS, nome)
            marcados = pagina.locator(f"[{MARCA_ALVO}]").count()
            return achou, marcados
        finally:
            pagina.close()

    def test_acha_pelo_nome_exato(self, navegador):
        html = _barra_lateral(["Joselia Teste", GRUPO_NOME, "Tobias Testão"])
        assert self._achar(navegador, html, GRUPO_NOME) == (True, 1)

    def test_acha_com_acento_decomposto_no_dom(self, navegador):
        """O DOM em NFD, o .env em NFC. Antes isto era um 'não achei' mudo."""
        import unicodedata
        decomposto = unicodedata.normalize("NFD", GRUPO_NOME)
        assert decomposto != GRUPO_NOME, "o caso precisa ser realmente diferente"
        html = _barra_lateral(["Joselia Teste", decomposto])
        assert self._achar(navegador, html, GRUPO_NOME) == (True, 1)

    def test_tolera_espaco_duplo_e_maiuscula(self, navegador):
        html = _barra_lateral(["SANTANDER  CAPITAL  SIMULAÇÕES"])
        assert self._achar(navegador, html, GRUPO_NOME) == (True, 1)

    def test_marca_o_item_clicavel_e_nao_o_span(self, navegador):
        from app.whatsapp import ACHAR_CONVERSA_JS, MARCA_ALVO
        pagina = navegador.new_page()
        try:
            pagina.set_content(_barra_lateral([GRUPO_NOME]))
            pagina.evaluate(ACHAR_CONVERSA_JS, GRUPO_NOME)
            marcado = pagina.locator(f"[{MARCA_ALVO}]").first
            assert marcado.evaluate("el => el.getAttribute('role')") == "listitem"
        finally:
            pagina.close()

    def test_grupo_ausente_devolve_falso(self, navegador):
        html = _barra_lateral(["Joselia Teste", "Tobias Testão"])
        assert self._achar(navegador, html, GRUPO_NOME) == (False, 0)

    def test_nao_deixa_marca_velha_para_tras(self, navegador):
        """Duas buscas seguidas não podem deixar dois alvos marcados."""
        from app.whatsapp import ACHAR_CONVERSA_JS, MARCA_ALVO
        pagina = navegador.new_page()
        try:
            pagina.set_content(_barra_lateral(["Joselia Teste", GRUPO_NOME]))
            pagina.evaluate(ACHAR_CONVERSA_JS, GRUPO_NOME)
            pagina.evaluate(ACHAR_CONVERSA_JS, "Joselia Teste")
            assert pagina.locator(f"[{MARCA_ALVO}]").count() == 1
        finally:
            pagina.close()


PARTICIPANTES = ("Allana, Glaucon, Joselia, Ryan, Tobias, +55 62 8000-1004, "
                 "+55 62 9000-1005, +55 62 9000-1006, +55 62 9000-1007")


def _cabecalho(titulos: list[str]) -> str:
    """O header do CHAT vive dentro de `#main` — o de fora é o da lista lateral.

    Cada título numa `div` própria, como no DOM real: sem isso o `innerText`
    junta tudo numa linha só e a leitura por texto devolve a concatenação.
    """
    spans = "".join(f'<div><span title="{t}">{t}</span></div>' for t in titulos)
    return f"""<!doctype html><html><body>
    <div id="main"><header>{spans}</header></div>
    <div id="main"><div role="row" data-id="false_5562000@g.us_AAA_5562111@c.us">
      <div class="copyable-text" data-pre-plain-text="[20:13, 29/08/2026] Ryan: ">
        <span class="selectable-text"><span>Ivone Teste</span></span>
      </div></div></div></body></html>"""


class TestTituloDoCabecalho:
    """Num grupo o cabeçalho tem DUAS linhas com title: o nome e os participantes.

    Pegar "o primeiro span[title]" trazia a lista de participantes; o bot
    concluía que a conversa aberta não era o grupo configurado, recusava ler,
    e repetia o aviso a cada 3 segundos. Log inundado, zero mensagem lida.
    """

    def _titulos(self, navegador, html: str) -> list[str]:
        from app.whatsapp import CHAT_INFO_JS
        pagina = navegador.new_page()
        try:
            pagina.set_content(html)
            return (pagina.evaluate(CHAT_INFO_JS) or {}).get("titulos") or []
        finally:
            pagina.close()

    def test_devolve_os_dois_titulos(self, navegador):
        titulos = self._titulos(navegador, _cabecalho([GRUPO_NOME, PARTICIPANTES]))
        assert GRUPO_NOME in titulos
        assert PARTICIPANTES in titulos

    def test_escolhe_o_grupo_e_nao_os_participantes(self, navegador, tmp_path):
        """O caso real: participantes vinham primeiro no DOM."""
        from app.state_store import StateStore
        from app.whatsapp import WhatsAppService

        s = WhatsAppService(profile_dir=tmp_path / "p", group_name=GRUPO_NOME,
                            state=StateStore(tmp_path / "s.json"), headless=True)
        # ordem invertida de proposito: o participante aparece antes
        titulos = self._titulos(navegador, _cabecalho([PARTICIPANTES, GRUPO_NOME]))
        assert s._escolher_titulo(titulos) == GRUPO_NOME

    def test_escolhe_o_grupo_mesmo_com_acento_decomposto(self, navegador, tmp_path):
        import unicodedata
        from app.state_store import StateStore
        from app.whatsapp import WhatsAppService

        s = WhatsAppService(profile_dir=tmp_path / "p", group_name=GRUPO_NOME,
                            state=StateStore(tmp_path / "s.json"), headless=True)
        decomposto = unicodedata.normalize("NFD", GRUPO_NOME)
        assert s._escolher_titulo([PARTICIPANTES, decomposto]) == GRUPO_NOME

    def test_conversa_individual_usa_o_primeiro(self, tmp_path):
        from app.state_store import StateStore
        from app.whatsapp import WhatsAppService

        s = WhatsAppService(profile_dir=tmp_path / "p", group_name=GRUPO_NOME,
                            state=StateStore(tmp_path / "s.json"), headless=True)
        assert s._escolher_titulo(["Joselia Teste", "online"]) == "Joselia Teste"


class TestEscolhaDoInputDeAnexo:
    """Imagem tem de ir como IMAGEM, não como arquivo para baixar.

    O WhatsApp mantém o <input> de DOCUMENTO montado o tempo todo; o de
    imagem só aparece depois de abrir o menu do clipe. A versão anterior
    testava o seletor genérico `input[type=file]` antes de abrir o menu,
    casava com o de documento, e o consultor recebia "REQ000001.png · 58 KB"
    com um botão de download em vez da imagem aberta na conversa.
    """

    DOC = '<input type="file" accept="*">'
    IMG = '<input type="file" accept="image/*,video/mp4,video/3gpp">'

    def _casa(self, navegador, html: str, seletor: str) -> int:
        pagina = navegador.new_page()
        try:
            pagina.set_content(f"<!doctype html><html><body>{html}</body></html>")
            return pagina.locator(seletor).count()
        finally:
            pagina.close()

    # A escolha deixou de ser por SELETOR e passou a ser pelo `accept` lido de
    # cada input (`_input_de_imagem`): o WhatsApp mantém vários inputs na
    # página e o seletor genérico pegava o de documento.
    SELETOR_IMAGEM = 'input[type="file"][accept*="image"]'

    def test_o_seletor_de_imagem_ignora_o_de_documento(self, navegador):
        assert self._casa(navegador, self.DOC, self.SELETOR_IMAGEM) == 0

    def test_o_seletor_de_imagem_acha_o_de_imagem(self, navegador):
        assert self._casa(navegador, self.IMG, self.SELETOR_IMAGEM) == 1

    def test_o_generico_casaria_com_o_documento(self, navegador):
        """A prova do defeito: por isso o genérico não pode vir primeiro."""
        assert self._casa(navegador, self.DOC, 'input[type="file"]') == 1

    def test_com_os_dois_montados_o_de_imagem_vence(self, navegador):
        """Com o de DOCUMENTO primeiro no DOM — a armadilha real."""
        pagina = navegador.new_page()
        try:
            pagina.set_content(f"<!doctype html><html><body>{self.DOC}{self.IMG}</body></html>")
            entradas = pagina.locator('input[type="file"]')
            # A mesma varredura de `_input_de_imagem`: escolhe pelo accept.
            escolhido = next(
                (entradas.nth(i).get_attribute("accept") or ""
                 for i in range(entradas.count())
                 if "image" in (entradas.nth(i).get_attribute("accept") or "")), None)
            assert escolhido and "image" in escolhido
            assert entradas.nth(0).get_attribute("accept") == "*", (
                "o de documento tem de vir primeiro para o teste valer")
        finally:
            pagina.close()


# ------------------------------------------------- bolha real de grupo
def _bolha_de_grupo(data_id: str, autor: str, corpo: str) -> str:
    """A estrutura que o WhatsApp usa MESMO num grupo.

    O rótulo com o nome de quem escreveu vem ANTES do corpo no DOM. Os
    primeiros testes usavam uma bolha simplificada, sem esse rótulo — e foi
    exatamente por isso que passaram enquanto o bot gravava "Ryan" como
    sendo o texto de toda mensagem recebida.
    """
    linhas = corpo.replace("\n", "<br>")
    return f"""
    <div role="row">
      <div class="message-in" data-id="{data_id}">
        <div class="copyable-text" data-pre-plain-text="[22:10, 29/08/2026] {autor}: ">
          <div><span dir="auto" class="_ahxt">{autor}</span></div>
          <div class="_akbu">
            <span class="selectable-text copyable-text"><span>{linhas}</span></span>
          </div>
        </div>
      </div>
    </div>"""


class TestCorpoDaMensagemEmGrupo:
    def _ler(self, navegador, html: str):
        pagina = navegador.new_page()
        try:
            pagina.set_content(f"""<!doctype html><html><body>
                <header><span title="{GRUPO_NOME}">{GRUPO_NOME}</span></header>
                <div id="main">{html}</div></body></html>""")
            return pagina.evaluate(READ_MESSAGES_JS, [30, "Operacional Capital"])
        finally:
            pagina.close()

    def test_le_o_corpo_e_nao_o_nome_do_autor(self, navegador):
        """A regressão real: gravou 'Ryan' em vez do pedido da Clarisa."""
        html = _bolha_de_grupo(
            "false_5562000@g.us_AAA_5562111@c.us", "Ryan",
            "Clarisa de Tássia\n82674159804\nAmapá")
        lidas = self._ler(navegador, html)
        assert len(lidas) == 1
        texto = lidas[0]["text"]
        assert texto.strip() != "Ryan", "voltou a capturar o nome do remetente"
        assert "Clarisa de Tássia" in texto
        assert "82674159804" in texto
        assert "Amapá" in texto

    def test_o_autor_continua_disponivel_no_meta(self, navegador):
        """O nome não se perde: ele vem do data-pre-plain-text, que é o lugar dele."""
        html = _bolha_de_grupo("false_5562000@g.us_AAA_5562111@c.us", "Ryan", "teste")
        assert "Ryan" in self._ler(navegador, html)[0]["meta"]

    def test_o_texto_lido_vira_uma_simulacao(self, navegador):
        """Ponta a ponta: o que sai do DOM tem de atravessar o parser."""
        from app.parser import parse_request

        html = _bolha_de_grupo(
            "false_5562000@g.us_AAA_5562111@c.us", "Ryan",
            "Clarisa de Tássia\n82674159804\nAmapá")
        texto = self._ler(navegador, html)[0]["text"]
        pedido, erros = parse_request(texto)
        assert erros == []
        assert pedido is not None, "o texto lido não virou pedido"
        assert pedido.cpf == "82674159804"
        assert pedido.customer_name == "Clarisa de Tássia"


class TestTodoJSCompila:
    """Todo trecho de JS do módulo tem de ser sintaticamente válido.

    Um deles chegou ao operador com `SyntaxError: Invalid or unexpected token`:
    a string Python interpretou o `\n` e injetou uma quebra de linha real
    dentro de uma string JavaScript. O diagnóstico que existia justamente para
    explicar uma falha morreu com um erro próprio — e ninguém viu o motivo.
    Este teste roda cada trecho num Chromium de verdade.
    """

    def _constantes(self):
        import app.whatsapp as w
        return [(nome, getattr(w, nome)) for nome in dir(w)
                if nome.endswith("_JS") and isinstance(getattr(w, nome), str)]

    def test_ha_constantes_para_testar(self):
        assert len(self._constantes()) >= 4

    def test_cada_trecho_avalia_sem_erro_de_sintaxe(self, navegador):
        pagina = navegador.new_page()
        try:
            pagina.set_content("<!doctype html><html><body><div id='main'></div></body></html>")
            falhas = []
            for nome, js in self._constantes():
                try:
                    # Um argumento serve para as arrow functions que recebem
                    # parâmetro; as que não recebem simplesmente o ignoram.
                    pagina.evaluate(js, "x")
                except Exception as exc:
                    if "SyntaxError" in str(exc):
                        falhas.append(f"{nome}: {str(exc)[:120]}")
            assert not falhas, "JS inválido: " + " | ".join(falhas)
        finally:
            pagina.close()


class TestLocalizarMensagemParaCitar:
    """Achar a mensagem é o passo 0 da citação — e era onde ela morria.

    O código usava `[data-id="..."]` como seletor CSS. O id do WhatsApp
    carrega '@', '.', '-' e '=', e quando o escape não dava conta o
    resultado era `count() == 0` — indistinguível de "a mensagem saiu da
    tela". A citação nunca funcionava e o log nunca dizia por quê, porque
    esse caminho retornava antes do diagnóstico.
    """

    ID_REAL = "false_556291234567-1499@g.us_3EB0A1B2C3_5562999888777@c.us"

    def _pagina(self, navegador, ids: list[str]):
        linhas = "".join(
            f'<div role="row" data-id="{i}"><span class="selectable-text">{i[-8:]}</span></div>'
            for i in ids)
        pagina = navegador.new_page()
        pagina.set_content(f'<!doctype html><html><body><div id="main">{linhas}</div></body></html>')
        return pagina

    def test_acha_id_com_arroba_ponto_e_hifen(self, navegador):
        from app.whatsapp import ACHAR_MENSAGEM_JS, MARCA_MENSAGEM
        pagina = self._pagina(navegador, ["outro", self.ID_REAL])
        try:
            assert pagina.evaluate(ACHAR_MENSAGEM_JS, self.ID_REAL) is True
            assert pagina.locator(f"[{MARCA_MENSAGEM}]").count() == 1
        finally:
            pagina.close()

    def test_marca_exatamente_a_mensagem_pedida(self, navegador):
        from app.whatsapp import ACHAR_MENSAGEM_JS, MARCA_MENSAGEM
        outro = "false_556291234567-1499@g.us_OUTRO_5562999888777@c.us"
        pagina = self._pagina(navegador, [outro, self.ID_REAL])
        try:
            pagina.evaluate(ACHAR_MENSAGEM_JS, self.ID_REAL)
            marcado = pagina.locator(f"[{MARCA_MENSAGEM}]").first
            assert marcado.get_attribute("data-id") == self.ID_REAL
        finally:
            pagina.close()

    def test_id_ausente_devolve_falso(self, navegador):
        from app.whatsapp import ACHAR_MENSAGEM_JS
        pagina = self._pagina(navegador, ["outro"])
        try:
            assert pagina.evaluate(ACHAR_MENSAGEM_JS, self.ID_REAL) is False
        finally:
            pagina.close()

    def test_nao_acumula_marca_entre_chamadas(self, navegador):
        from app.whatsapp import ACHAR_MENSAGEM_JS, MARCA_MENSAGEM
        outro = "false_556291234567-1499@g.us_OUTRO_5562999888777@c.us"
        pagina = self._pagina(navegador, [outro, self.ID_REAL])
        try:
            pagina.evaluate(ACHAR_MENSAGEM_JS, self.ID_REAL)
            pagina.evaluate(ACHAR_MENSAGEM_JS, outro)
            assert pagina.locator(f"[{MARCA_MENSAGEM}]").count() == 1
        finally:
            pagina.close()


class TestNuncaEncaminhar:
    """O bot abriu "Selecionar conversas" e encaminhou o resultado.

    O menu de contexto tem "Responder" e "Encaminhar" lado a lado, e o botão
    de encaminhar aparece no hover junto do menu. Um seletor frouxo
    (`button[aria-haspopup="true"]`) casava com o de encaminhar. Encaminhar
    manda CPF e nome do cliente para outra conversa — é o pior erro possível
    aqui, e por isso vira teste.
    """

    def test_rotulos_perigosos_sao_recusados(self):
        from app.whatsapp import _PROIBIDO_CLICAR
        for rotulo in ("Encaminhar", "encaminhar", "Forward", "Apagar",
                       "Excluir", "Delete", "Encaminhar mensagem"):
            assert _PROIBIDO_CLICAR.search(rotulo), f"{rotulo!r} tinha de ser recusado"

    def test_responder_continua_permitido(self):
        from app.whatsapp import _PROIBIDO_CLICAR
        for rotulo in ("Responder", "Reply", "responder"):
            assert not _PROIBIDO_CLICAR.search(rotulo), f"{rotulo!r} não pode ser bloqueado"

    def test_o_botao_de_encaminhar_nunca_vira_gatilho(self, navegador):
        """A causa exata: `aria-haspopup` genérico casava com encaminhar.

        A escolha do gatilho deixou de ser um seletor CSS e virou uma
        varredura com lista de proibidos — a garantia mudou de lugar, então
        o teste verifica o mecanismo novo.
        """
        from app.whatsapp import BOTAO_DE_OPCOES_JS, GEOMETRIA_DA_LINHA_JS
        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body><div id="main">
                <div role="row" data-id="false_g@g.us_A_x@c.us">
                  <div role="button" aria-haspopup="true" aria-label="Encaminhar">x</div>
                </div></div></body></html>""")
            pagina.evaluate(GEOMETRIA_DA_LINHA_JS, ["false_g@g.us_A_x@c.us", ""])
            achado = pagina.evaluate(BOTAO_DE_OPCOES_JS)
            assert not achado["achou"], "o botão de encaminhar virou a setinha"
        finally:
            pagina.close()

    def test_dialogo_de_encaminhar_e_reconhecido(self, navegador):
        """A rede de segurança precisa enxergar o diálogo para poder fechá-lo."""
        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body>
                <div role="dialog"><header>Selecionar conversas</header>
                <input placeholder="Pesquisar nome, número"></div></body></html>""")
            achou = pagina.locator(
                'div[role="dialog"]:has-text("Selecionar conversas"), '
                'div[role="dialog"]:has-text("Forward to"), '
                'header:has-text("Selecionar conversas")').first
            assert achou.count() > 0
        finally:
            pagina.close()

    def test_tela_normal_nao_dispara_a_rede_de_seguranca(self, navegador):
        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body>
                <div id="main"><div role="row">mensagem</div></div></body></html>""")
            achou = pagina.locator(
                'div[role="dialog"]:has-text("Selecionar conversas"), '
                'header:has-text("Selecionar conversas")').first
            assert achou.count() == 0
        finally:
            pagina.close()


class TestDiagnosticoDaCitacaoNaoMente:
    """O diagnóstico chegou a REPORTAR o oposto do que acontecia.

    Ele dizia `linha_encontrada=False` enquanto a citação tinha achado a
    mensagem: usava `CSS.escape()` dentro de `[data-id="..."]`. CSS.escape
    serve para IDENTIFICADORES, não para valores entre aspas — transforma
    `a@b` em `a\@b`, que é outra string. Um diagnóstico que mente é pior que
    nenhum, porque manda a investigação para o lado errado.
    """

    ID = "false_556291234567-1499@g.us_3EB0A1B2C3_5562999888777@c.us"

    def _diag(self, navegador, corpo: str):
        from app.whatsapp import DIAGNOSTICO_CITACAO_JS
        pagina = navegador.new_page()
        try:
            pagina.set_content(f'<!doctype html><html><body>{corpo}</body></html>')
            return pagina.evaluate(DIAGNOSTICO_CITACAO_JS, self.ID)
        finally:
            pagina.close()

    def test_acha_a_linha_com_id_cheio_de_simbolos(self, navegador):
        d = self._diag(navegador, f'<div role="row" data-id="{self.ID}">oi</div>')
        assert d["achou_linha"] is True, "voltou a usar seletor CSS no data-id"

    def test_relata_icones_e_rotulos_da_bolha(self, navegador):
        d = self._diag(navegador, f"""
            <div role="row" data-id="{self.ID}">
              <span data-icon="down-context"></span>
              <div role="button" aria-label="Menu de contexto">v</div>
            </div>""")
        assert "down-context" in d["icones"]
        assert "Menu de contexto" in d["rotulos"]

    def test_relata_os_itens_de_menu_abertos(self, navegador):
        d = self._diag(navegador, f"""
            <div role="row" data-id="{self.ID}">oi</div>
            <ul role="menu"><li>Responder</li><li>Encaminhar</li></ul>""")
        assert "Responder" in d["parecidos"]

    def test_as_chaves_batem_com_o_que_o_log_le(self):
        """O log lia 'itens_parecidos' e o JS devolvia 'parecidos' -> None.

        Ancorado na FUNÇÃO, não numa frase do log: a versão anterior procurava
        a string "Não consegui citar por nenhuma via" dentro do arquivo, e
        quebrou quando a mensagem mudou — sem que a chave errada tivesse
        voltado. O que importa é o log ler exatamente as chaves que o JS
        devolve.
        """
        import inspect

        from app.whatsapp import WhatsAppService

        fonte = inspect.getsource(WhatsAppService._diagnosticar_citacao)
        for chave in ("icones", "rotulos", "botoes", "parecidos", "menus"):
            assert f"d.get('{chave}')" in fonte, f"o log não mostra {chave}"
        assert "itens_parecidos" not in fonte, "chave inexistente voltou ao log"

    def test_o_log_diz_ONDE_a_citacao_parou(self):
        """"Falhou" sem dizer em que passo custou várias rodadas de palpite."""
        import inspect

        from app.whatsapp import WhatsAppService

        fonte = inspect.getsource(WhatsAppService._diagnosticar_citacao)
        assert "d.get('onde')" in fonte


class TestAcharItemDeMenuPorTexto:
    """Itens de menu achados pelo TEXTO, não por seletor CSS chutado.

    Os seletores `li:has-text(...)` / `[aria-label=...]` nunca casaram com o
    menu real do WhatsApp — nem para "Responder", nem para "Fotos e vídeos".
    Comparar texto em JS não depende de classe, aria-label nem estrutura.
    """

    def _achar(self, navegador, corpo: str, alvos: list[str]):
        from app.whatsapp import ACHAR_ITEM_MENU_JS, MARCA_ITEM
        pagina = navegador.new_page()
        try:
            pagina.set_content(f"<!doctype html><html><body>{corpo}</body></html>")
            achado = pagina.evaluate(ACHAR_ITEM_MENU_JS, alvos)
            marcados = pagina.locator(f"[{MARCA_ITEM}]")
            texto = marcados.first.inner_text().strip() if marcados.count() else ""
            return achado, marcados.count(), texto
        finally:
            pagina.close()

    MENU_LI = '<ul role="menu"><li>Responder</li><li>Encaminhar</li><li>Apagar</li></ul>'
    MENU_DIVS = ('<div role="application">'
                 '<div role="button"><span>Responder</span></div>'
                 '<div role="button"><span>Encaminhar</span></div></div>')
    MENU_ITEM = ('<div role="menuitem">Responder</div>'
                 '<div role="menuitem">Encaminhar</div>')
    MENU_EN = '<ul role="menu"><li>Reply</li><li>Forward</li></ul>'

    @pytest.mark.parametrize("corpo,esperado", [
        (MENU_LI, "Responder"), (MENU_DIVS, "Responder"),
        (MENU_ITEM, "Responder"), (MENU_EN, "Reply"),
    ])
    def test_acha_responder_em_qualquer_estrutura(self, navegador, corpo, esperado):
        achado, n, texto = self._achar(navegador, corpo, ["responder", "reply"])
        assert n == 1 and texto == esperado

    @pytest.mark.parametrize("proibido", [
        '<ul role="menu"><li>Encaminhar</li></ul>',
        '<ul role="menu"><li>Apagar</li></ul>',
        '<ul role="menu"><li>Excluir</li></ul>',
        '<ul role="menu"><li>Forward</li></ul>',
    ])
    def test_nunca_marca_item_perigoso(self, navegador, proibido):
        """Encaminhar já mandou dados de cliente para a conversa errada."""
        achado, n, _ = self._achar(navegador, proibido, ["responder", "reply"])
        assert achado == "" and n == 0

    def test_escolhe_fotos_e_nao_documento(self, navegador):
        """A causa de o resultado chegar como arquivo para baixar."""
        from app.whatsapp import WhatsAppService
        menu = ('<div role="application">'
                '<div role="button"><span>Documento</span></div>'
                '<div role="button"><span>Fotos e vídeos</span></div></div>')
        _achado, n, texto = self._achar(
            navegador, menu, list(WhatsAppService._TEXTOS_FOTOS))
        assert n == 1
        assert texto == "Fotos e vídeos", f"marcou {texto!r} em vez de Fotos e vídeos"

    def test_nao_acumula_marca(self, navegador):
        from app.whatsapp import ACHAR_ITEM_MENU_JS, MARCA_ITEM
        pagina = navegador.new_page()
        try:
            pagina.set_content(f"<!doctype html><html><body>{self.MENU_LI}</body></html>")
            pagina.evaluate(ACHAR_ITEM_MENU_JS, ["responder"])
            pagina.evaluate(ACHAR_ITEM_MENU_JS, ["responder"])
            assert pagina.locator(f"[{MARCA_ITEM}]").count() == 1
        finally:
            pagina.close()


class TestTodoJSDeDiagnosticoRoda:
    """Diagnóstico que quebra é pior que diagnóstico nenhum.

    Já aconteceu duas vezes: um `\n` interpretado pelo Python virou quebra de
    linha dentro de uma string JS, e um `.map(limpar)` recebeu o ELEMENTO em
    vez do texto (map chama com item, índice, array). Nos dois casos a
    ferramenta feita para explicar a falha morreu com erro próprio, e a
    investigação foi para o lado errado.
    """

    def _constantes_de_diagnostico(self):
        import app.whatsapp as w
        return [(n, getattr(w, n)) for n in dir(w)
                if n.startswith("DIAGNOSTICO_") and isinstance(getattr(w, n), str)]

    def test_existem(self):
        assert len(self._constantes_de_diagnostico()) >= 3

    def test_rodam_numa_pagina_com_conteudo(self, navegador):
        """Página vazia esconde erros que só aparecem ao percorrer elementos."""
        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body>
                <header><span title="Grupo">Grupo</span></header>
                <input type="file" accept="*">
                <span data-icon="plus-rounded"></span>
                <div id="main">
                  <div role="row" data-id="false_g@g.us_A_x@c.us">
                    <div class="copyable-text" data-pre-plain-text="[00:00, 30/08/2026] Ryan: ">
                      <span class="selectable-text"><span>texto</span></span>
                    </div>
                  </div>
                </div>
                <ul role="menu"><li>Responder</li><li>Encaminhar</li></ul>
                </body></html>""")
            falhas = []
            for nome, js in self._constantes_de_diagnostico():
                try:
                    pagina.evaluate(js, "false_g@g.us_A_x@c.us")
                except Exception as exc:
                    falhas.append(f"{nome}: {str(exc)[:130]}")
            assert not falhas, "diagnóstico quebrado: " + " | ".join(falhas)
        finally:
            pagina.close()

    def test_o_do_anexo_lista_os_itens_do_menu(self, navegador):
        from app.whatsapp import DIAGNOSTICO_ANEXO_JS
        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body>
                <input type="file" accept="image/*">
                <div role="button"><span>Fotos e vídeos</span></div>
                </body></html>""")
            d = pagina.evaluate(DIAGNOSTICO_ANEXO_JS)
            assert "image/*" in d["inputs"]
            assert "Fotos e vídeos" in d["itens"]
        finally:
            pagina.close()


class TestGatilhosDaBolha:
    """A setinha que abre o menu da mensagem.

    O mecanismo mudou depois que o laboratório (`ferramentas/testar_citacao.py`)
    mostrou o rótulo REAL desta instalação::

        aria-label="Menu de contexto para a mensagem de Ryan"

    Não é "Abrir opções de mensagem" — esse era palpite meu, e por isso o
    código caía sempre no plano B do botão direito. O rótulo carrega o NOME
    de quem mandou, então muda a cada mensagem: a comparação é por prefixo.

    O que continua valendo, e é o mais importante: rótulo perigoso nunca vira
    a setinha. Encaminhar manda os dados do cliente para outra conversa.
    """

    ID = "false_g@g.us_A_x@c.us"

    def _marcar(self, navegador, corpo_da_bolha: str):
        pagina = navegador.new_page()
        try:
            # `min-height` porque o JS exige geometria: um elemento de tamanho
            # zero não é clicável, e aceitar um seria clicar no nada.
            pagina.set_content(f"""<!doctype html><html><body>
                <style>[data-icon],[role=button]{{display:block;min-width:20px;min-height:20px}}</style>
                <div id="main">
                <div role="row" data-id="{self.ID}">
                  <span class="selectable-text">Maria Tabaré</span>
                  {corpo_da_bolha}
                </div></div></body></html>""")
            return _setinha(pagina, self.ID)
        finally:
            pagina.close()

    @pytest.mark.parametrize("rotulo", ["Encaminhar", "Forward", "Reagir",
                                        "React", "Apagar", "Baixar", "Download"])
    def test_rotulo_perigoso_nunca_vira_a_setinha(self, navegador, rotulo):
        n, _ = self._marcar(navegador, f'<div role="button" aria-label="{rotulo}">x</div>')
        assert n == 0, f"{rotulo!r} virou a setinha"

    def test_acha_o_rotulo_real_desta_instalacao(self, navegador):
        """O que o laboratório observou no WhatsApp de verdade."""
        n, rotulos = self._marcar(
            navegador,
            '<div role="button" aria-label="Menu de contexto para a mensagem de Ryan">v</div>')
        assert n == 1
        assert rotulos[0].startswith("Menu de contexto")

    def test_o_nome_no_rotulo_nao_atrapalha(self, navegador):
        """Ele muda a cada mensagem — por isso a comparação é por prefixo."""
        for quem in ("Ryan", "Allana testando", "+55 62 8000-1002"):
            n, _ = self._marcar(
                navegador,
                f'<div role="button" aria-label="Menu de contexto para a mensagem de {quem}">v</div>')
            assert n == 1, f"não achou com o nome {quem!r}"

    def test_o_icone_tambem_serve(self, navegador):
        n, rotulos = self._marcar(navegador, '<span data-icon="down-context"></span>')
        assert n == 1
        assert rotulos[0] == "down-context"

    def test_encaminhar_ao_lado_da_setinha_nao_confunde(self, navegador):
        """Os dois aparecem juntos no hover. Só um pode ser clicado."""
        n, rotulos = self._marcar(navegador, """
            <div role="button" aria-label="Encaminhar">seta</div>
            <div role="button" aria-label="Menu de contexto para a mensagem de Ryan">v</div>""")
        assert n == 1
        assert "Encaminhar" not in rotulos[0]

    def test_bolha_inexistente_devolve_zero(self, navegador):
        pagina = navegador.new_page()
        try:
            pagina.set_content('<!doctype html><html><body><div id="main"></div></body></html>')
            assert _quantas_setinhas(pagina, self.ID) == 0
        finally:
            pagina.close()

    def test_botao_de_tamanho_zero_nao_conta(self, navegador):
        """Clicar num elemento invisível é clicar no nada."""
        pagina = navegador.new_page()
        try:
            pagina.set_content(f"""<!doctype html><html><body><div id="main">
                <div role="row" data-id="{self.ID}">
                  <span class="selectable-text">Maria</span>
                  <div role="button" aria-label="Menu de contexto para a mensagem de Ryan"
                       style="display:none">v</div>
                </div></div></body></html>""")
            assert _quantas_setinhas(pagina, self.ID) == 0
        finally:
            pagina.close()


class TestNuncaLerAsProprias:
    """O bot leu as próprias mensagens e entrou em laço.

    O WhatsApp às vezes serve o `data-id` "pelado", sem o prefixo
    `false_`/`true_`. O filtro por prefixo não casava, a leitura caía no
    fallback `div[role="row"]` — que pega TUDO — e `startsWith('true_')`
    também não barrava um id sem prefixo. Resultado: o bot lia a própria
    resposta, não achava CPF válido, cobrava CPF, lia a cobrança e cobrava de
    novo. Foram 53 mensagens no grupo do cliente.
    """

    def _ler(self, navegador, corpo: str):
        pagina = navegador.new_page()
        try:
            pagina.set_content(f"""<!doctype html><html><body>
                <div id="main">{corpo}</div></body></html>""")
            return pagina.evaluate(READ_MESSAGES_JS, [30, "Operacional Capital"])
        finally:
            pagina.close()

    NOSSA_SEM_PREFIXO = """
      <div role="row">
        <div data-id="3EB0566994AE2D2798E44D">
          <div class="copyable-text" data-pre-plain-text="[02:04, 30/08/2026] Operacional Capital: ">
            <span class="selectable-text"><span>Operacional Capital, para simular preciso de: CPF.</span></span>
          </div>
          <span data-icon="msg-dblcheck"></span>
        </div>
      </div>"""

    DELE_SEM_PREFIXO = """
      <div role="row">
        <div data-id="2AF4AAAAAAAAAAAAAAAAAA">
          <div class="copyable-text" data-pre-plain-text="[02:05, 30/08/2026] Ryan: ">
            <span class="selectable-text"><span>Maria Tabaré 316.196.143.91</span></span>
          </div>
        </div>
      </div>"""

    def test_ignora_a_propria_mensagem_com_id_pelado(self, navegador):
        """O caso exato: recibo de entrega é o único sinal disponível."""
        lidas = self._ler(navegador, self.NOSSA_SEM_PREFIXO)
        assert lidas == [], f"leu a própria mensagem: {lidas}"

    def test_ainda_le_a_mensagem_do_consultor_com_id_pelado(self, navegador):
        """A proteção não pode cegar o bot para mensagens legítimas."""
        lidas = self._ler(navegador, self.DELE_SEM_PREFIXO)
        assert len(lidas) == 1
        assert "Maria Tabaré" in lidas[0]["text"]

    def test_separa_as_duas_no_mesmo_chat(self, navegador):
        lidas = self._ler(navegador, self.NOSSA_SEM_PREFIXO + self.DELE_SEM_PREFIXO)
        textos = [m["text"] for m in lidas]
        assert len(lidas) == 1, f"deveria ler só a do consultor, leu {textos}"
        assert "Maria Tabaré" in textos[0]

    @pytest.mark.parametrize("icone", ["msg-check", "msg-dblcheck", "msg-time",
                                       "status-dblcheck"])
    def test_qualquer_recibo_de_entrega_identifica_a_nossa(self, navegador, icone):
        corpo = f"""
          <div role="row"><div data-id="3EB0XYZ">
            <div class="copyable-text" data-pre-plain-text="[02:04, 30/08/2026] Operacional Capital: ">
              <span class="selectable-text"><span>resposta do bot</span></span></div>
            <span data-icon="{icone}"></span>
          </div></div>"""
        assert self._ler(navegador, corpo) == []

    def test_classe_message_out_continua_valendo(self, navegador):
        corpo = """
          <div role="row"><div class="message-out" data-id="3EB0ZZZ">
            <div class="copyable-text" data-pre-plain-text="[02:04, 30/08/2026] Operacional Capital: ">
              <span class="selectable-text"><span>resposta</span></span></div>
          </div></div>"""
        assert self._ler(navegador, corpo) == []


class TestMirarNaLinhaDaMensagem:
    """Citar mirava no elemento errado, e por isso falhava "achando" a mensagem.

    Esta instalação do WhatsApp serve o `data-id` pelado (ex.: `2A729AF702…`)
    num elemento INTERNO da bolha. O ícone de contexto e a área que responde
    ao hover ficam na LINHA, fora dele. As três vias de citação eram tentadas
    contra um alvo que nunca teria o menu — e o log dizia só "as três vias
    falharam", sem explicar por quê.
    """

    ID = "2A729AF7029257E5696C"

    # A estrutura real: o ícone é irmão da bolha, não filho do data-id.
    BOLHA = f"""
      <div role="row">
        <span data-icon="down-context" style="display:block;width:20px;height:20px"></span>
        <div class="message-in">
          <div class="copyable-text" data-id="{ID}"
               data-pre-plain-text="[02:03, 30/08/2026] Ryan: ">
            <span class="selectable-text"><span>Maria Tabaré</span></span>
          </div>
        </div>
      </div>"""

    def _pagina(self, navegador):
        pagina = navegador.new_page()
        pagina.set_content(
            f'<!doctype html><html><body><div id="main">{self.BOLHA}</div></body></html>')
        return pagina

    def test_marca_a_linha_e_nao_o_elemento_interno(self, navegador):
        from app.whatsapp import ACHAR_MENSAGEM_JS, MARCA_MENSAGEM
        pagina = self._pagina(navegador)
        try:
            assert pagina.evaluate(ACHAR_MENSAGEM_JS, self.ID) is True
            marcado = pagina.locator(f"[{MARCA_MENSAGEM}]").first
            assert marcado.get_attribute("role") == "row", (
                "marcou o elemento do data-id; o menu não responde nele")
        finally:
            pagina.close()

    def test_enxerga_o_gatilho_que_esta_fora_da_bolha(self, navegador):
        """A causa concreta: o ícone é irmão, não filho."""
        pagina = self._pagina(navegador)
        try:
            assert _quantas_setinhas(pagina, self.ID) == 1
        finally:
            pagina.close()

    def test_o_diagnostico_relata_o_icone(self, navegador):
        from app.whatsapp import DIAGNOSTICO_CITACAO_JS
        pagina = self._pagina(navegador)
        try:
            d = pagina.evaluate(DIAGNOSTICO_CITACAO_JS, self.ID)
            assert d["achou_linha"] is True
            assert "down-context" in d["icones"]
        finally:
            pagina.close()

    def test_id_pelado_ainda_e_lido_como_mensagem(self, navegador):
        """Toda a lógica de prefixo `false_`/`true_` não vale nesta versão."""
        pagina = self._pagina(navegador)
        try:
            lidas = pagina.evaluate(READ_MESSAGES_JS, [30, "Operacional Capital"])
            assert len(lidas) == 1
            assert lidas[0]["id"] == self.ID
            assert "Maria Tabaré" in lidas[0]["text"]
        finally:
            pagina.close()


class TestNaoResponderASiMesmo:
    """O laço que encheu o grupo de 53 cobranças de CPF.

    A resposta "para simular preciso de: CPF." contém a palavra CPF sem
    valor. Relida, o parser a entendia como pedido incompleto, e o bot
    cobrava CPF de novo — alimentando a si mesmo indefinidamente.

    São duas camadas: não reler o que enviamos (casando pelo TEXTO, já que
    esta versão do WhatsApp não usa message-in/message-out, serve o data-id
    sem prefixo e não expõe recibo de entrega em seletor estável) e, se uma
    escapar, reconhecer o próprio formato de saída.
    """

    MENSAGENS_DO_BOT = [
        "⚠️ *Operacional Capital*, para simular preciso de:\nCPF.",
        "❌ *Ryan* — não foi possível simular\nCarregado\n🆔 REQ000021",
        "🔁 *Resultado da sua simulação* (reenvio)\n\n🧑 Marina",
        "⚠️ *Não libera*\n👤 Ryan\n📋 Contrato\n🆔 REQ000020",
        "📥 *Simulação recebida*\n\n👤 Consultor: Ryan",
        "✅ *Libera R$ 2.219,77*\n👤 Ryan\n🆔 REQ000019",
        "🔎 *Simulação realizada*\n\n🧑 Silvana Brito",
    ]

    DE_CONSULTOR = [
        "Maria Tabaré\nAmapá\n316.196.143.91",
        "Ivone Teste\n42888832453\nAmapá",
        "sem matricula",
        "bom dia",
        "CLIENTE EM ATRASO EM PRODUTOS DO BANCO",
    ]

    def _detector(self):
        from app.manager import BotManager

        class Falso:
            _ASSINATURAS_DO_BOT = BotManager._ASSINATURAS_DO_BOT
            _e_mensagem_nossa = BotManager._e_mensagem_nossa
        return Falso()

    @pytest.mark.parametrize("texto", MENSAGENS_DO_BOT)
    def test_reconhece_toda_forma_de_resposta_nossa(self, texto):
        assert self._detector()._e_mensagem_nossa(texto) is True, (
            "esta resposta reiniciaria o laço se fosse relida")

    @pytest.mark.parametrize("texto", DE_CONSULTOR)
    def test_nunca_barra_mensagem_de_consultor(self, texto):
        assert self._detector()._e_mensagem_nossa(texto) is False, (
            "barrou um pedido legítimo — pior que o laço")

    def test_a_cobranca_de_cpf_relida_seria_um_pedido(self):
        """Prova de que o laço era real, e não teoria."""
        from app.parser import parse_request
        _pedido, erros = parse_request(
            "⚠️ *Operacional Capital*, para simular preciso de:\nCPF.")
        assert erros == ["CPF"], (
            "sem a proteção, o parser trata a própria cobrança como pedido")

    def test_acha_pelo_texto_a_mensagem_que_enviamos(self, navegador):
        from app.whatsapp import IDS_POR_TEXTO_JS
        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body><div id="main">
              <div data-id="2A_DELE"><span class="selectable-text">Maria Tabaré 316</span></div>
              <div data-id="3EB0_NOSSA"><span class="selectable-text">Operacional Capital, para simular preciso de: CPF.</span></div>
              </div></body></html>""")
            ids = pagina.evaluate(
                IDS_POR_TEXTO_JS, ["Operacional Capital, para simular", 8])
            assert ids == ["3EB0_NOSSA"], f"achou {ids}"
        finally:
            pagina.close()


class TestMenuRealDoOperador:
    """O menu exato que o operador tem na tela, capturado do print dele.

    A busca anterior procurava em `li, [role=menuitem], [role=button],
    button, div[tabindex]`. Este menu usa `<div>` simples, sem role e sem
    tabindex — nenhum candidato casava, e a citação falhava mesmo com o menu
    ABERTO e "Responder" visível. É a diferença entre o DOM que eu inventei
    para testar e o que existe de verdade.
    """

    # Ordem e textos exatos do print.
    ITENS = ["Responder", "Responder em particular", "Conversar com Ryan",
             "Copiar", "Reagir", "Encaminhar", "Fixar", "Pergunte à Meta AI",
             "Favoritar", "Denunciar", "Apagar"]

    def _menu(self, itens=None) -> str:
        corpo = "".join(
            f'<div class="x1n2onr6"><div><span>{i}</span></div></div>'
            for i in (itens if itens is not None else self.ITENS))
        return f'<!doctype html><html><body><div class="menu">{corpo}</div></body></html>'

    def _achar(self, navegador, html, alvos=("responder", "reply")):
        from app.whatsapp import ACHAR_ITEM_MENU_JS, MARCA_ITEM
        pagina = navegador.new_page()
        try:
            pagina.set_content(html)
            achado = pagina.evaluate(ACHAR_ITEM_MENU_JS, list(alvos))
            marcados = pagina.locator(f"[{MARCA_ITEM}]")
            texto = marcados.first.inner_text().strip() if marcados.count() else ""
            return achado, marcados.count(), texto
        finally:
            pagina.close()

    def test_acha_responder_em_divs_sem_role(self, navegador):
        """O caso real: nenhum `role`, nenhum `tabindex`, só `<div>`."""
        achado, n, texto = self._achar(navegador, self._menu())
        assert achado == "responder"
        assert n == 1
        assert texto == "Responder"

    def test_nao_confunde_com_responder_em_particular(self, navegador):
        """Os dois começam igual; só o exato serve."""
        _achado, _n, texto = self._achar(navegador, self._menu())
        assert texto == "Responder", f"pegou {texto!r}"

    @pytest.mark.parametrize("perigoso", ["Encaminhar", "Apagar", "Denunciar",
                                          "Reagir", "Fixar", "Favoritar"])
    def test_nunca_marca_os_itens_perigosos(self, navegador, perigoso):
        """Menu só com o item perigoso: não pode marcar nada."""
        achado, n, _ = self._achar(navegador, self._menu([perigoso]))
        assert achado == "" and n == 0

    def test_marca_um_elemento_clicavel_e_nao_o_span(self, navegador):
        from app.whatsapp import ACHAR_ITEM_MENU_JS, MARCA_ITEM
        pagina = navegador.new_page()
        try:
            pagina.set_content(self._menu())
            pagina.evaluate(ACHAR_ITEM_MENU_JS, ["responder"])
            marcado = pagina.locator(f"[{MARCA_ITEM}]").first
            # Sobe do <span> até a div do item — o clique precisa de área.
            assert marcado.evaluate("el => el.tagName") == "DIV"
        finally:
            pagina.close()

    def test_menu_em_ingles_tambem(self, navegador):
        achado, n, texto = self._achar(
            navegador, self._menu(["Reply", "Reply privately", "Forward", "Delete"]))
        assert n == 1 and texto == "Reply", f"achou {achado!r} / {texto!r}"


class TestImagemNuncaComoDocumento:
    """SPEC §4 — o defeito B, travado para sempre.

    O WhatsApp mantém VÁRIOS `input[type=file]` na página: um de foto (com
    `accept` contendo `image/*`) e um de documento (sem `accept` ou `*`).
    Pegar "o primeiro do DOM" pega o de documento — e o consultor recebe
    `REQ000029.png · 57 KB` para baixar em vez da imagem na conversa.
    """

    # O de DOCUMENTO vem PRIMEIRO no DOM, de propósito: é a armadilha real.
    DOIS_INPUTS = """<!doctype html><html><body>
      <input type="file" accept="*">
      <input type="file" accept="image/*,video/mp4,video/3gpp">
      </body></html>"""

    def test_escolhe_o_input_de_imagem_mesmo_vindo_depois(self, navegador):
        pagina = navegador.new_page()
        try:
            pagina.set_content(self.DOIS_INPUTS)
            entradas = pagina.locator('input[type="file"]')
            escolhido = None
            vistos = []
            for i in range(entradas.count()):
                accept = (entradas.nth(i).get_attribute("accept") or "").lower()
                vistos.append(accept)
                if "image" in accept:
                    escolhido = accept
                    break
            assert escolhido is not None, f"nenhum input de imagem em {vistos}"
            assert "image" in escolhido
            assert vistos[0] == "*", "o de documento tem de vir primeiro no teste"
        finally:
            pagina.close()

    def test_so_documento_disponivel_nao_serve(self, navegador):
        """Sem input de imagem, o certo é DESISTIR e cair para texto."""
        pagina = navegador.new_page()
        try:
            pagina.set_content(
                '<!doctype html><html><body><input type="file" accept="*"></body></html>')
            entradas = pagina.locator('input[type="file"]')
            achou = any("image" in (entradas.nth(i).get_attribute("accept") or "")
                        for i in range(entradas.count()))
            assert not achou
        finally:
            pagina.close()

    def test_preview_de_documento_e_recusado(self, navegador):
        from app.whatsapp import PREVIEW_E_IMAGEM_JS
        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body>
              <div>REQ000029.png</div><div>57 KB</div></body></html>""")
            estado = pagina.evaluate(PREVIEW_E_IMAGEM_JS)
            assert estado["temMiniatura"] is False
            assert estado["pareceDocumento"] is True
        finally:
            pagina.close()

    def test_preview_de_imagem_e_aceito(self, navegador):
        from app.whatsapp import PREVIEW_E_IMAGEM_JS
        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body>
              <img src="blob:abc"><div>legenda</div></body></html>""")
            estado = pagina.evaluate(PREVIEW_E_IMAGEM_JS)
            assert estado["temMiniatura"] is True
            assert estado["pareceDocumento"] is False
        finally:
            pagina.close()

    def test_ultima_saida_distingue_imagem_de_documento(self, navegador):
        from app.whatsapp import ULTIMA_SAIDA_JS
        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body>
              <div role="row"><div class="message-in">pedido do consultor</div></div>
              <div role="row"><div class="message-out">
                <img src="blob:x"><span>Não libera</span>
                <span data-icon="msg-dblcheck"></span></div></div>
              </body></html>""")
            saida = pagina.evaluate(ULTIMA_SAIDA_JS)
            assert saida["temImagem"] is True
            assert saida["pareceDocumento"] is False

            pagina.set_content("""<!doctype html><html><body>
              <div role="row"><div class="message-out">
                <span>REQ000029.png</span><span>57 KB</span>
                <span data-icon="msg-dblcheck"></span></div></div>
              </body></html>""")
            saida = pagina.evaluate(ULTIMA_SAIDA_JS)
            assert saida["temImagem"] is False
            assert saida["pareceDocumento"] is True, "não detectou o card de download"
        finally:
            pagina.close()

    def test_colar_precisa_do_compositor(self, navegador):
        from app.whatsapp import COLAR_IMAGEM_JS
        pagina = navegador.new_page()
        try:
            pagina.set_content("<!doctype html><html><body></body></html>")
            r = pagina.evaluate(COLAR_IMAGEM_JS, ["", "REQ1.png"])
            assert r["ok"] is False
            assert "compositor" in r["motivo"]
        finally:
            pagina.close()

    def test_colar_usa_o_compositor_do_footer(self, navegador):
        """O campo de legenda do preview NÃO está no footer; o compositor sim."""
        from app.whatsapp import COLAR_IMAGEM_JS
        import base64
        png = base64.b64encode(bytes.fromhex("89504e470d0a1a0a")).decode()
        pagina = navegador.new_page()
        try:
            pagina.set_content("""<!doctype html><html><body>
              <div contenteditable="true" id="legenda"></div>
              <footer><div contenteditable="true" id="compositor"></div></footer>
              </body></html>""")
            assert pagina.evaluate(COLAR_IMAGEM_JS, [png, "REQ1.png"])["ok"] is True
        finally:
            pagina.close()


class TestAlbumNossoEhNosso:
    """O WhatsApp agrupa imagens nossas num álbum, e o id muda de forma.

    Quando duas ou mais respostas em imagem saem seguidas, elas viram um
    álbum e o `data-id` passa a ser `album-3EB0...-3EB0...-2`. A checagem de
    autoria por prefixo olhava o começo do id — e `album-` na frente fazia um
    álbum de cards NOSSOS não ser reconhecido como nosso.

    A primeira camada (o autor no `data-pre-plain-text`) normalmente pega
    isso. Mas ela é a que depende de `BOT_SELF_NAME` estar certo, e o prefixo
    existe justamente para quando ela falha.
    """

    def _nossa(self, navegador, data_id: str, autor: str = "") -> bool:
        from app.whatsapp import READ_MESSAGES_JS

        pagina = navegador.new_page()
        try:
            pre = (f'data-pre-plain-text="[10:00, 01/09/2026] {autor}: "'
                   if autor else "")
            pagina.set_content(f"""<!doctype html><html><body><div id="main">
              <div role="row"><div data-id="{data_id}" {pre}>
                <span class="selectable-text">Maria Tabaré 31619614391</span>
              </div></div></div></body></html>""")
            lidas = pagina.evaluate(READ_MESSAGES_JS, [20, "Operacional Capital"])
            # Se a mensagem foi lida, ela NÃO foi considerada nossa.
            return not lidas
        finally:
            pagina.close()

    def test_album_com_id_nosso_e_reconhecido(self, navegador):
        assert self._nossa(
            navegador, "album-3EB040839CD946DF502E9E-3EB0F33DAEEB54D2672C93-4") is True

    def test_album_recebido_continua_sendo_lido(self, navegador):
        """Um álbum de um consultor não pode ser descartado como nosso."""
        assert self._nossa(navegador, "album-2A729AF7029257E5696C-2") is False

    def test_id_nosso_sem_album_continua_valendo(self, navegador):
        assert self._nossa(navegador, "3EB0C5A277F7F9B6C599") is True

    def test_id_recebido_continua_sendo_lido(self, navegador):
        assert self._nossa(navegador, "2A729AF7029257E5696C") is False
