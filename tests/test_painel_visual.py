"""Regras da interface que valem a pena travar.

Não testam aparência — testam as invariantes que o painel precisa manter para
continuar legível e honesto:

1. cor vive num arquivo só (trocar a paleta não pode virar caça ao HEX);
2. o que o `index.html` referencia existe em disco (um `src` errado quebra a
   marca em silêncio);
3. tabela com no máximo 6 colunas (o resto vai para o painel de detalhe);
4. estado do backend é traduzido num módulo só.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ESTATICOS = Path(__file__).resolve().parents[1] / "dashboard" / "static"
TOKENS = ESTATICOS / "css" / "tokens.css"

# `\b` sozinho casaria com o "#hist" de "#history"; a olhada adiante recusa
# qualquer coisa que continue com letra, número, `-` ou `_`.
HEX = re.compile(r"#[0-9A-Fa-f]{3,8}\b(?![0-9A-Za-z_-])")


def _arquivos(*sufixos: str) -> list[Path]:
    return sorted(p for s in sufixos for p in ESTATICOS.rglob(f"*{s}"))


class TestCorSoNosTokens:
    def test_nenhum_hex_fora_do_tokens(self):
        fora = {
            str(f.relative_to(ESTATICOS)): sorted({m.group(0) for m in HEX.finditer(
                f.read_text(encoding="utf-8"))})
            for f in _arquivos(".css", ".js", ".html")
            if f != TOKENS
        }
        fora = {k: v for k, v in fora.items() if v}
        assert not fora, f"cor fora de tokens.css: {fora}"

    def test_tokens_da_paleta_existem(self):
        texto = TOKENS.read_text(encoding="utf-8")
        for token in ("--bg", "--surface", "--surface-2", "--surface-hover", "--border",
                      "--text", "--text-secondary", "--muted", "--allana-red",
                      "--allana-red-dark", "--allana-red-soft", "--lilac", "--lilac-soft",
                      "--purple-dark", "--success", "--warning", "--error", "--info"):
            assert f"{token}:" in texto, f"token da paleta ausente: {token}"


class TestArquivosDaMarca:
    def test_o_que_o_index_referencia_existe(self):
        html = (ESTATICOS / "index.html").read_text(encoding="utf-8")
        caminhos = set(re.findall(r'(?:src|href)="(/[^"#]+)"', html))
        faltando = [c for c in caminhos if not (ESTATICOS / c.lstrip("/")).exists()]
        assert not faltando, f"referência sem arquivo: {faltando}"

    def test_a_allana_aparece_so_onde_pode(self):
        """Barra lateral, login, estado vazio e estado de erro — mais nada."""
        permitido = {"index.html", str(Path("js") / "core" / "logo.js"),
                     str(Path("js") / "core" / "ui.js")}
        usos = [
            str(f.relative_to(ESTATICOS))
            for f in _arquivos(".js", ".html", ".css")
            if "allana-avatar" in f.read_text(encoding="utf-8")
            or "allana-logo" in f.read_text(encoding="utf-8")
            or "allanaAvatar" in f.read_text(encoding="utf-8")
        ]
        assert set(usos) <= permitido, f"a personagem vazou para: {sorted(set(usos) - permitido)}"


class TestTabelas:
    @pytest.mark.parametrize("arquivo", sorted(
        p.name for p in (ESTATICOS / "js" / "views").glob("*.js")))
    def test_no_maximo_seis_colunas(self, arquivo):
        texto = (ESTATICOS / "js" / "views" / arquivo).read_text(encoding="utf-8")
        for bloco in re.findall(r"const COLUMNS = \[(.*?)\n\];", texto, re.S):
            colunas = re.findall(r"\{\s*label:", bloco)
            assert len(colunas) <= 6, f"{arquivo}: {len(colunas)} colunas na tabela"


class TestEstadoNumLugarSo:
    def test_rotulos_de_entrega_so_no_status_js(self):
        status = ESTATICOS / "js" / "core" / "status.js"
        assert "unconfirmed:" in status.read_text(encoding="utf-8")
        vazados = [
            str(f.relative_to(ESTATICOS))
            for f in _arquivos(".js")
            if f != status and re.search(r"\bunconfirmed:\s*\{", f.read_text(encoding="utf-8"))
        ]
        assert not vazados, f"tradução de entrega duplicada em: {vazados}"

    def test_badge_nao_depende_so_de_cor(self):
        """Todo badge de estado leva ícone junto do texto."""
        texto = (ESTATICOS / "js" / "core" / "status.js").read_text(encoding="utf-8")
        assert "iconName ? icon(iconName, 12) : null" in texto
        for estado in ("delivered", "pending", "retrying", "unconfirmed", "failed"):
            linha = re.search(rf"{estado}:\s*\{{[^}}]*\}}", texto)
            assert linha and "icon:" in linha.group(0), f"{estado} sem ícone"


class TestOsModulosSeEncontram:
    """O painel é ES modules servidos direto, sem empacotador.

    Um caminho errado num `import` não quebra o arquivo: quebra o painel
    INTEIRO, em silêncio, com a página branca e o erro só no console do
    navegador. Não há build para avisar — então avisa este teste.
    """

    def test_todo_import_aponta_para_um_arquivo_que_existe(self):
        padrao = re.compile(r"""(?:import|export)\s[^;]*?from\s+['"]([^'"]+)['"]""", re.S)
        quebrados: list[str] = []
        vistos = 0
        for arquivo in _arquivos(".js"):
            for alvo in padrao.findall(arquivo.read_text(encoding="utf-8")):
                if alvo.startswith("http"):
                    continue
                vistos += 1
                destino = (ESTATICOS / alvo.lstrip("/") if alvo.startswith("/")
                           else arquivo.parent / alvo)
                if not destino.exists():
                    quebrados.append(f"{arquivo.relative_to(ESTATICOS)} -> {alvo}")
        assert vistos, "nenhum import encontrado: o padrão de busca quebrou"
        assert not quebrados, f"import sem arquivo: {quebrados}"


class TestVariavelDeCorSempreDefinida:
    """`var(--nao-existe)` não é erro de CSS: a regra simplesmente não pinta.
    O elemento fica transparente ou com a cor herdada, e ninguém vê o defeito
    — só um contraste estranho em uma tela específica."""

    def test_toda_variavel_usada_tem_definicao(self):
        sem_comentario = re.compile(r"/\*.*?\*/", re.S)
        definidos: set[str] = set()
        for arquivo in _arquivos(".css"):
            definidos |= set(re.findall(r"(--[A-Za-z0-9_-]+)\s*:",
                                        arquivo.read_text(encoding="utf-8")))
        faltando: dict[str, str] = {}
        for arquivo in _arquivos(".css", ".js", ".html"):
            texto = sem_comentario.sub("", arquivo.read_text(encoding="utf-8"))
            for nome in re.findall(r"var\(\s*(--[A-Za-z0-9_-]+)", texto):
                if nome not in definidos:
                    faltando[nome] = str(arquivo.relative_to(ESTATICOS))
        assert not faltando, f"variável de cor sem definição: {faltando}"
