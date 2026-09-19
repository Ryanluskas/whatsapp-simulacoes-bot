"""
O laço da busca: o bot achava o grupo, não abria, e digitava o nome de novo.

Cada teste é uma tela que o WhatsApp Web realmente mostra — lista, busca
preenchida, resultado, conversa errada aberta, clique que não abre — e cobra
a sequência determinística:

    LISTA/BUSCA → RESULTADO → CLIQUE → CABEÇALHO CONFERIDO → CHAT_READY

Nenhum teste abre navegador.
"""
from types import SimpleNamespace

import pytest

from app.state_store import StateStore
from app.whatsapp import (ACHAR_CONVERSA_JS, CHAT_INFO_JS, ESTADO_DA_TELA_JS,
                          READ_MESSAGES_JS, WhatsAppService)

GRUPO = "Santander Capital Simulações"
OUTRO = "Financeiro"


class TelaFalsa:
    """O WhatsApp Web de mentira: lista, busca, conversa aberta.

    `clique_abre=False` reproduz o caso de produção: o resultado aparece, o
    clique acontece, e a conversa não muda.
    """

    def __init__(self, aberto="", busca="", na_lista=(GRUPO,), linhas=3,
                 clique_abre=True, tem_main=None):
        self.aberto = aberto
        self.busca = busca
        self.na_lista = list(na_lista)
        self.linhas = linhas
        self.clique_abre = clique_abre
        self._tem_main = tem_main
        self.marcado = ""
        self.digitacoes = []          # cada vez que o bot digitou na busca
        self.cliques = 0
        self.escapes = 0
        self.screenshots = 0
        self.keyboard = SimpleNamespace(press=self._press)

    # ---------------------------------------------------------- Playwright
    @property
    def tem_main(self):
        return bool(self.aberto) if self._tem_main is None else self._tem_main

    def evaluate(self, script, arg=None):
        if script is ESTADO_DA_TELA_JS:
            return {
                "url": "https://web.whatsapp.com/",
                "search_visible": True,
                "search_value": self.busca,
                "target_in_list": any(n == arg for n in self.na_lista) or bool(self.busca),
                "header_visible": self.tem_main,
                "header_titles": [self.aberto] if self.aberto else [],
                "main_visible": self.tem_main,
                "message_rows": self.linhas if self.aberto else 0,
            }
        if script is ACHAR_CONVERSA_JS:
            achou = any(n == arg for n in self.na_lista)
            self.marcado = arg if achou else ""
            return achou
        if script is CHAT_INFO_JS:
            return {"titulos": [self.aberto] if self.aberto else [],
                    "jid": "120363@g.us", "temMain": self.tem_main}
        if script is READ_MESSAGES_JS:
            return []
        return None

    def locator(self, seletor):
        return LocatorFalso(self, seletor)

    def wait_for_selector(self, seletor, timeout=None, state=None):
        if seletor == "#main" and not self.tem_main:
            raise TimeoutErroFalso("#main não apareceu")
        return True

    def wait_for_timeout(self, _ms):
        return None

    def screenshot(self, path=None):
        self.screenshots += 1

    def _press(self, tecla):
        if tecla == "Escape":
            self.escapes += 1
            self.busca = ""
        elif tecla == "Backspace":
            self.busca = ""


class TimeoutErroFalso(Exception):
    pass


class LocatorFalso:
    def __init__(self, tela, seletor):
        self.tela = tela
        self.seletor = seletor

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def click(self, timeout=None, force=False):
        if "allana-alvo" in self.seletor:
            self.tela.cliques += 1          # clique no resultado/conversa
            if self.tela.clique_abre and self.tela.marcado:
                self.tela.aberto = self.tela.marcado

    def type(self, texto, delay=None):
        self.tela.digitacoes.append(texto)
        self.tela.busca = texto


def servico(tela, grupo=GRUPO, tmp_path=None):
    s = WhatsAppService(profile_dir=tmp_path / "perfil", group_name=grupo,
                        state=StateStore(tmp_path / "state.json"), headless=True)
    s._log = lambda *a, **k: None
    s._page = tela
    s._escape = lambda: None
    return s


@pytest.fixture(autouse=True)
def _sem_playwright(monkeypatch):
    """Os erros do dublê precisam ser tratados como os do Playwright."""
    import app.whatsapp as wa
    monkeypatch.setattr(wa, "PlaywrightTimeout", TimeoutErroFalso)
    monkeypatch.setattr(wa, "PlaywrightError", TimeoutErroFalso)


# ===================================================== 1. ESTADO DA TELA ====
class TestDiagnostico:
    def test_conversa_certa_aberta_e_ready(self, tmp_path):
        s = servico(TelaFalsa(aberto=GRUPO), tmp_path=tmp_path)
        d = s._diagnosticar_chat_atual(GRUPO)
        assert d["chat_ready"] is True and d["estado"] == s.CHAT_READY

    def test_so_a_lista_nao_e_ready(self, tmp_path):
        s = servico(TelaFalsa(aberto=""), tmp_path=tmp_path)
        d = s._diagnosticar_chat_atual(GRUPO)
        assert d["chat_ready"] is False and d["estado"] in (s.CHAT_LIST, s.CHAT_RESULT)
        assert "nenhuma conversa aberta" in d["reason"]

    def test_busca_aberta_nao_e_ready(self, tmp_path):
        s = servico(TelaFalsa(aberto="", busca=GRUPO), tmp_path=tmp_path)
        d = s._diagnosticar_chat_atual(GRUPO)
        assert d["chat_ready"] is False
        assert d["estado"] in (s.CHAT_SEARCH, s.CHAT_RESULT)

    def test_resultado_encontrado_nao_significa_aberto(self, tmp_path):
        """O ponto central: o card do grupo no resultado NÃO é o grupo aberto."""
        s = servico(TelaFalsa(aberto="", busca=GRUPO, na_lista=(GRUPO,)), tmp_path=tmp_path)
        d = s._diagnosticar_chat_atual(GRUPO)
        assert d["target_in_list"] is True and d["chat_ready"] is False

    def test_grupo_errado_aberto(self, tmp_path):
        s = servico(TelaFalsa(aberto=OUTRO), tmp_path=tmp_path)
        d = s._diagnosticar_chat_atual(GRUPO)
        assert d["chat_ready"] is False and d["reason"] == "outra conversa está aberta"

    def test_main_presente_mas_sem_mensagens(self, tmp_path):
        s = servico(TelaFalsa(aberto=GRUPO, linhas=0), tmp_path=tmp_path)
        d = s._diagnosticar_chat_atual(GRUPO)
        assert d["chat_ready"] is False and "ainda não carregaram" in d["reason"]

    def test_acento_decomposto_nao_derruba(self, tmp_path):
        decomposto = "Santander Capital Simulações"
        s = servico(TelaFalsa(aberto=decomposto), tmp_path=tmp_path)
        assert s._diagnosticar_chat_atual(GRUPO)["chat_ready"] is True

    def test_espaco_duplo_e_caixa_nao_derrubam(self, tmp_path):
        s = servico(TelaFalsa(aberto="  santander  capital   simulações "), tmp_path=tmp_path)
        assert s._diagnosticar_chat_atual(GRUPO)["chat_ready"] is True

    def test_diagnostico_nao_expoe_o_texto_da_busca_no_arquivo(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        tela = TelaFalsa(aberto="", busca="algum texto")
        s = servico(tela, tmp_path=tmp_path)
        caminho = s._salvar_diagnostico_de_abertura(s._diagnosticar_chat_atual(GRUPO))
        assert caminho and "algum texto" not in (tmp_path / caminho).read_text(encoding="utf-8")


# ========================================================= 2. ABERTURA =====
class TestAbertura:
    def test_ja_aberto_nao_busca_nem_clica(self, tmp_path):
        tela = TelaFalsa(aberto=GRUPO)
        s = servico(tela, tmp_path=tmp_path)
        assert s._open_chat(GRUPO) is True
        assert tela.digitacoes == [] and tela.cliques == 0

    def test_fechado_mas_na_lista_abre_com_um_clique_sem_digitar(self, tmp_path):
        tela = TelaFalsa(aberto="", na_lista=(GRUPO,))
        s = servico(tela, tmp_path=tmp_path)
        assert s._open_chat(GRUPO) is True
        assert tela.cliques == 1 and tela.digitacoes == []
        assert tela.aberto == GRUPO

    def test_busca_ja_preenchida_nao_digita_de_novo(self, tmp_path):
        """Regressão do laço: com o nome já na busca, digitar de novo é proibido."""
        tela = TelaFalsa(aberto="", busca=GRUPO, na_lista=())
        s = servico(tela, tmp_path=tmp_path)
        s._abrir_pela_lista = lambda _n: False      # a lista não tem o grupo
        tela.na_lista = [GRUPO]                     # mas o resultado da busca tem
        s._open_chat(GRUPO)
        assert tela.digitacoes == [], "redigitou o nome com a busca já preenchida"

    def test_nao_encontrado_em_lugar_nenhum_para_em_duas_tentativas(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        tela = TelaFalsa(aberto="", na_lista=())
        s = servico(tela, tmp_path=tmp_path)
        assert s._open_chat(GRUPO) is False
        assert len(tela.digitacoes) <= s.MAX_OPEN_CHAT_ATTEMPTS
        assert tela.screenshots == 1                # diagnóstico salvo uma vez

    def test_clique_que_nao_abre_nao_vira_sucesso(self, tmp_path, monkeypatch):
        """Produção: o resultado aparece, o clique acontece, nada muda."""
        monkeypatch.chdir(tmp_path)
        tela = TelaFalsa(aberto="", na_lista=(GRUPO,), clique_abre=False)
        s = servico(tela, tmp_path=tmp_path)
        assert s._open_chat(GRUPO) is False
        assert s._current_chat == ""
        assert tela.cliques <= s.MAX_OPEN_CHAT_ATTEMPTS

    def test_abre_conversa_errada_nao_vira_sucesso(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        tela = TelaFalsa(aberto="", na_lista=(GRUPO,))
        s = servico(tela, tmp_path=tmp_path)
        original = tela.evaluate

        def abre_errado(script, arg=None):
            if script is ACHAR_CONVERSA_JS:
                tela.marcado = OUTRO                # clica e abre outra conversa
                return True
            return original(script, arg)

        tela.evaluate = abre_errado
        assert s._open_chat(GRUPO) is False
        assert s._current_chat == ""

    def test_current_chat_stale_nao_e_prova(self, tmp_path, monkeypatch):
        """`_current_chat` dizia que estava aberto; a tela dizia o contrário."""
        monkeypatch.chdir(tmp_path)
        tela = TelaFalsa(aberto=OUTRO, na_lista=())
        s = servico(tela, tmp_path=tmp_path)
        s._current_chat = GRUPO                     # cache mentiroso
        assert s._open_chat(GRUPO) is False
        assert s._current_chat == ""

    def test_limpa_a_busca_depois_de_abrir(self, tmp_path):
        """Busca preenchida deixava a lateral em modo resultado — metade do laço."""
        tela = TelaFalsa(aberto="", busca="lixo", na_lista=(GRUPO,))
        s = servico(tela, tmp_path=tmp_path)
        assert s._open_chat(GRUPO) is True
        assert tela.busca == ""

    def test_nunca_digita_duas_vezes_seguidas(self, tmp_path, monkeypatch):
        """O teste que o laço reprova: duas digitações consecutivas iguais."""
        monkeypatch.chdir(tmp_path)
        tela = TelaFalsa(aberto="", na_lista=(), clique_abre=False)
        s = servico(tela, tmp_path=tmp_path)
        s._abrir_pela_lista = lambda _n: False
        s._open_chat(GRUPO)
        consecutivas = [a for a, b in zip(tela.digitacoes, tela.digitacoes[1:]) if a == b]
        assert not consecutivas, f"digitou o mesmo nome duas vezes: {tela.digitacoes}"


# ========================================================== 3. LEITURA =====
class TestLeitura:
    def _preparar(self, tela, tmp_path):
        s = servico(tela, tmp_path=tmp_path)
        s._conferir_nome_proprio = lambda: None
        return s

    def test_nao_le_fora_do_chat_ready(self, tmp_path):
        """Uma tela de busca não pode virar solicitação."""
        tela = TelaFalsa(aberto="", busca=GRUPO, na_lista=(GRUPO,))
        lidos = []
        s = self._preparar(tela, tmp_path)
        tela.evaluate_original = tela.evaluate

        def com_linhas(script, arg=None):
            if script is READ_MESSAGES_JS:
                lidos.append(1)
                return [{"id": "false_120363@g.us_ABC_5562@c.us",
                         "text": "VANDIRA ROSA\n89794150100\nINHUMAS", "meta": ""}]
            return tela.evaluate_original(script, arg)

        tela.evaluate = com_linhas
        recebidas = []
        s._on_incoming = lambda m: recebidas.append(m)
        s._poll_messages()
        # O gate barra ANTES de a mensagem virar solicitação; o bot pode (e deve)
        # aproveitar a volta para reabrir a conversa.
        assert recebidas == [], "processou mensagem sem CHAT_READY"

    def test_le_quando_esta_ready(self, tmp_path):
        tela = TelaFalsa(aberto=GRUPO, linhas=1)
        recebidas = []
        s = self._preparar(tela, tmp_path)
        s._on_incoming = lambda m: recebidas.append(m)
        tela.evaluate_original = tela.evaluate

        def com_linhas(script, arg=None):
            if script is READ_MESSAGES_JS:
                return [{"id": "false_120363@g.us_ABC_5562900000001@c.us",
                         "text": "VANDIRA ROSA\n89794150100\nINHUMAS",
                         "meta": "[07:01, 18/09/2026] Ryan:"}]
            return tela.evaluate_original(script, arg)

        tela.evaluate = com_linhas
        s._poll_messages()          # primeira volta: linha de base
        s._poll_messages()
        assert s._current_chat == GRUPO or recebidas, "não leu com a conversa pronta"
