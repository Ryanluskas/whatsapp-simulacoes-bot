"""Escolher a aba certa do navegador.

O sintoma que perseguimos a noite toda tinha uma explicação só: um perfil
persistente RESTAURA as abas da sessão anterior, e `context.pages[0]` pode
ser uma `about:blank` que sobrou. Quando isso acontece o bot pilota uma
página em branco a sessão inteira — sem campo de digitação, sem mensagem
para citar, sem menu de anexo. Cada sintoma parecia um defeito diferente.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright


@pytest.fixture(scope="module")
def contexto(tmp_path_factory):
    perfil = tmp_path_factory.mktemp("perfil")
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(perfil), headless=True)
        yield ctx
        ctx.close()


class _ServicoFalso:
    """Só o suficiente para exercitar a escolha de aba, sem subir o serviço."""

    def __init__(self, contexto):
        self._context = contexto
        self._page = None
        self.registros = []

    def _log(self, nivel, msg):
        self.registros.append((nivel, msg))

    # os métodos reais, emprestados da classe
    from app.whatsapp import WhatsAppService
    _escolher_pagina = WhatsAppService._escolher_pagina
    _fechar_abas_extras = WhatsAppService._fechar_abas_extras


class TestEscolhaDaAba:
    def _servico(self, contexto):
        return _ServicoFalso(contexto)

    def test_escolhe_a_aba_do_whatsapp_entre_varias(self, contexto):
        """O caso real: about:blank restaurada antes da aba boa."""
        for p in list(contexto.pages):
            p.close()
        branca = contexto.new_page()          # vira pages[0]
        boa = contexto.new_page()
        boa.goto("https://web.whatsapp.com", wait_until="commit")

        s = self._servico(contexto)
        escolhida = s._escolher_pagina()
        assert escolhida is not branca, "escolheu a página em branco"
        assert "web.whatsapp.com" in escolhida.url

    def test_sem_whatsapp_reaproveita_a_em_branco(self, contexto):
        """Não pode criar aba nova a cada boot: elas se acumulam."""
        for p in list(contexto.pages):
            p.close()
        branca = contexto.new_page()

        s = self._servico(contexto)
        assert s._escolher_pagina() is branca

    def test_sem_nenhuma_aba_cria_uma(self, contexto):
        for p in list(contexto.pages):
            p.close()
        s = self._servico(contexto)
        pagina = s._escolher_pagina()
        assert pagina is not None
        pagina.close()

    def test_fecha_as_abas_extras(self, contexto):
        for p in list(contexto.pages):
            p.close()
        boa = contexto.new_page()
        boa.goto("https://web.whatsapp.com", wait_until="commit")
        contexto.new_page()
        contexto.new_page()
        assert len(contexto.pages) == 3

        s = self._servico(contexto)
        s._page = boa
        s._fechar_abas_extras()

        assert len(contexto.pages) == 1
        assert "web.whatsapp.com" in contexto.pages[0].url
        assert any("aba(s) extra" in m for _n, m in s.registros)

    def test_nunca_fecha_a_aba_em_uso(self, contexto):
        for p in list(contexto.pages):
            p.close()
        boa = contexto.new_page()
        boa.goto("https://web.whatsapp.com", wait_until="commit")

        s = self._servico(contexto)
        s._page = boa
        s._fechar_abas_extras()
        assert not boa.is_closed()

    def test_nao_reclama_quando_nao_ha_o_que_fechar(self, contexto):
        for p in list(contexto.pages):
            p.close()
        boa = contexto.new_page()
        boa.goto("https://web.whatsapp.com", wait_until="commit")
        s = self._servico(contexto)
        s._page = boa
        s._fechar_abas_extras()
        assert s.registros == [], "logou sem ter fechado nada"
