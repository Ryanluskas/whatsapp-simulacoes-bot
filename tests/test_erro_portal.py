"""Explicar ao consultor o que o portal realmente disse.

"Não libera" e "não conseguimos contatar a averbadora" são coisas
completamente diferentes para quem está atendendo: a primeira encerra o
assunto, a segunda é para tentar de novo. O `bot.py` do Arqueiro levanta
exceções genéricas ("não disponível"); o motivo de verdade fica num banner
na tela do portal, e era ele que se perdia.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from app.simulator import ERRO_PORTAL_JS, traduzir_erro_do_portal

# Texto exato capturado da tela do operador.
BANNER_REAL = ("MENSAGEM DO SERVIÇO: ERRO NO CAM: 1106 - USUÁRIO NÃO TEM "
               "ACESSO À OPERAÇÃO SOLICITADA")


@pytest.fixture(scope="module")
def navegador():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


def _ler(navegador, corpo: str) -> list[str]:
    pagina = navegador.new_page()
    try:
        pagina.set_content(f"<!doctype html><html><body>{corpo}</body></html>")
        return pagina.evaluate(ERRO_PORTAL_JS)
    finally:
        pagina.close()


class TestLeituraDoBanner:
    def test_le_o_banner_real_do_portal(self, navegador):
        textos = _ler(navegador, f'<div role="alert"><span>{BANNER_REAL}</span></div>')
        assert any("1106" in t for t in textos)

    def test_le_mesmo_sem_role_alert(self, navegador):
        """O portal nem sempre marca o aviso com role/classe."""
        textos = _ler(navegador, "<div><p>Não foi possível contatar a averbadora.</p></div>")
        assert any("averbadora" in t.lower() for t in textos)

    def test_pagina_sem_erro_devolve_vazio(self, navegador):
        assert _ler(navegador, "<div><h1>Simulação de empréstimo consignado</h1></div>") == []


class TestTraducao:
    @pytest.mark.parametrize("bruto,esperado", [
        (BANNER_REAL, "O usuário do portal não tem acesso a esta operação."),
        ("Não conseguimos contatar a averbadora", "Não foi possível contatar a averbadora."),
        ("Matrícula não encontrada para o convênio", "Matrícula inválida ou não encontrada."),
        ("Sistema indisponível, tente mais tarde",
         "O sistema do banco está indisponível no momento."),
    ])
    def test_vira_frase_util_para_o_consultor(self, bruto, esperado):
        assert traduzir_erro_do_portal([bruto])[0] == esperado

    def test_sem_padrao_conhecido_devolve_o_texto_do_portal(self):
        """Cru é melhor que 'Erro'; o que não pode é inventar."""
        bruto = "MENSAGEM DO SERVIÇO: falha XPTO-42 no roteador de propostas"
        saida, repetir = traduzir_erro_do_portal([bruto])
        assert "XPTO-42" in saida
        assert not saida.upper().startswith("MENSAGEM DO SERVI"), "o prefixo é ruído"
        assert repetir is False, "erro que não entendemos não deve ser repetido"

    def test_sem_nada_devolve_vazio(self):
        assert traduzir_erro_do_portal([]) == ("", False)

    def test_a_traducao_vence_a_ordem_da_lista(self):
        """Com vários textos na tela, o padrão conhecido é o que importa."""
        assert traduzir_erro_do_portal([
            "Simulação de empréstimo consignado",
            "Não conseguimos contatar a averbadora",
        ])[0] == "Não foi possível contatar a averbadora."


class TestQuandoRepetir:
    """Repetir só o que repetir resolve.

    "Não foi possível completar a operação" é falha transitória do portal: a
    segunda tentativa costuma passar. Já "usuário sem acesso" e "matrícula
    inválida" dariam exatamente o mesmo resultado, e repetir só atrasa a
    resposta ao consultor.
    """

    @pytest.mark.parametrize("bruto", [
        "Atenção! Não foi possível completar a operação.",
        "Não foi possível completar a operação",
        "Não conseguimos contatar a averbadora",
        "Sistema indisponível, tente novamente",
    ])
    def test_erro_transitorio_pede_nova_tentativa(self, bruto):
        assert traduzir_erro_do_portal([bruto])[1] is True

    @pytest.mark.parametrize("bruto", [
        BANNER_REAL,
        "Matrícula não encontrada para o convênio",
        "CPF inválido",
    ])
    def test_erro_de_cadastro_nao_se_repete(self, bruto):
        assert traduzir_erro_do_portal([bruto])[1] is False

    def test_a_tela_do_print_e_reconhecida(self, navegador):
        """A tela exata que o operador mandou: 'Atenção! Não foi possível...'"""
        textos = _ler(navegador, """
            <div><h2>Atenção!</h2>
            <p>Não foi possível completar a operação.</p></div>""")
        mensagem, repetir = traduzir_erro_do_portal(textos)
        assert repetir is True, "esta tela tem de disparar nova tentativa"
        assert "não completou a operação" in mensagem.lower()


class TestChegaAoConsultor:
    def _resultado_com_erro(self, erro: str):
        from app.models import (IncomingMessage, ParsedRequest, SimulationJob,
                                SimulationResult)

        pedido = ParsedRequest(consultant_name="Ryan", cpf="94106789353",
                               bank="Santander", contract="", customer_name="Palmas")
        mensagem = IncomingMessage(
            message_id="false_grupo@g.us_MSG1_5566999998888@c.us",
            chat_id="grupo@g.us", chat_name="Santander Capital Simulações",
            sender_id="5566999998888@c.us", sender_name="Ryan",
            text="Palmas\n941.067.893-53",
        )
        job = SimulationJob(request=pedido, message=mensagem,
                            request_id="REQ000007", simulation_id=7)
        return SimulationResult(job=job, ok=False, status="Erro", error=erro)

    def test_o_erro_aparece_na_legenda_enviada(self):
        """De nada adianta capturar se não sai na resposta.

        Contra `mensagens.legenda`, que é o que roda: `cards.build_caption`
        deixou de ser usado quando os textos foram centralizados, e um teste
        contra código morto dá confiança falsa.

        A legenda não repete o que o card mostra — mas num ERRO o motivo é a
        única informação útil, e deixá-lo só dentro da imagem obrigaria o
        consultor a abrir o card para saber o que houve.
        """
        from app import mensagens

        legenda = mensagens.legenda(
            self._resultado_com_erro("Não foi possível contatar a averbadora."),
            "REQ000007")
        assert "averbadora" in legenda

    def test_o_erro_aparece_na_imagem(self):
        from app.cards import build_result_html

        html = build_result_html(
            self._resultado_com_erro("Não foi possível contatar a averbadora."),
            "REQ000007")
        assert "averbadora" in html

    def test_erro_generico_nao_se_disfarca_de_recusa(self):
        """"Erro" não pode chegar parecendo "não libera".

        São coisas diferentes para o consultor: "não libera" é resposta do
        banco e encerra o assunto; erro é falha nossa e pede nova tentativa.
        """
        from app import mensagens

        erro = self._resultado_com_erro("Matrícula inválida ou não encontrada.")
        for fala in (mensagens.legenda(erro, "REQ000008"),
                     mensagens.texto(erro, "REQ000008")):
            assert "Matrícula" in fala
            assert "não libera" not in fala.casefold()


class TestNaoInventarErro:
    """Texto neutro da tela não pode virar "motivo da falha".

    O portal exibe a palavra "Carregado", e ela chegou ao consultor como
    `❌ não foi possível simular / Carregado / REQ000021`. Isso não explica
    nada e ainda passa a impressão de que o sistema está perdido.
    """

    @pytest.mark.parametrize("neutro", [
        "Carregado", "Carregando...", "Ver produtos", "Concluído",
        "Simulação de empréstimo consignado", "Dados do empregador",
        "Nome PF ou Sócio", "Produtos",
    ])
    def test_texto_neutro_nao_vira_motivo(self, neutro):
        mensagem, repetir = traduzir_erro_do_portal([neutro])
        assert mensagem == "", f"{neutro!r} foi tratado como erro: {mensagem!r}"
        assert repetir is False

    @pytest.mark.parametrize("erro", [
        BANNER_REAL,
        "Não foi possível completar a operação.",
        "Não conseguimos contatar a averbadora",
        "Sistema indisponível, tente novamente",
        "MENSAGEM DO SERVIÇO: falha XPTO-42 no roteador de propostas",
        "Matrícula inválida",
    ])
    def test_erro_de_verdade_continua_passando(self, erro):
        mensagem, _repetir = traduzir_erro_do_portal([erro])
        assert mensagem, f"{erro!r} deixou de ser reconhecido como erro"

    def test_tela_normal_com_ruido_nao_gera_motivo(self):
        """A tela do formulário tem vários textos; nenhum é erro."""
        tela = ["Simulação de empréstimo consignado", "1. Dados do empregador",
                "Nome do cliente", "Carregado", "Ver produtos"]
        assert traduzir_erro_do_portal(tela) == ("", False)

    def test_erro_no_meio_do_ruido_e_encontrado(self):
        tela = ["Simulação de empréstimo consignado", "Carregado",
                "Não conseguimos contatar a averbadora", "Ver produtos"]
        mensagem, repetir = traduzir_erro_do_portal(tela)
        assert "averbadora" in mensagem
        assert repetir is True


class TestNaoRecarregarPorCimaDoOperador:
    """Login manual impossível: a página recarregava enquanto ele digitava.

    O `aguardar_relogin` do Arqueiro navega para a landing a cada 5 minutos e
    tenta o login automático. Somado às duas navegações por job do nosso lado
    e ao reinício do navegador a cada erro do Playwright, digitar CPF e senha
    virava corrida contra o bot.

    O `bot.py` do Arqueiro não foi alterado — apenas deixamos de chamar a
    função dele que navega.
    """

    def _fonte(self, nome: str) -> str:
        import inspect

        from app.simulator import SimulatorService
        return inspect.getsource(getattr(SimulatorService, nome))

    def test_nao_chamamos_mais_o_relogin_que_navega(self):
        import inspect

        from app.simulator import SimulatorService
        fonte = inspect.getsource(SimulatorService)
        chamadas = [l for l in fonte.splitlines()
                    if "aguardar_relogin" in l and not l.strip().startswith("#")
                    and "``" not in l]
        assert not chamadas, f"voltou a chamar quem recarrega: {chamadas}"

    def test_a_espera_pelo_login_e_passiva(self):
        fonte = self._fonte("_esperar_login_do_operador")
        assert "goto" not in fonte, "a espera navegou — apagaria o que o operador digita"
        assert "_encontrar_pagina_formulario" in fonte, "precisa conferir se o login saiu"

    def test_a_espera_tem_limite_e_respeita_o_desligamento(self):
        fonte = self._fonte("_esperar_login_do_operador")
        assert "self.stopping" in fonte, "travaria o encerramento do sistema"
        from app.simulator import SimulatorService
        assert 60 <= SimulatorService._ESPERA_LOGIN_SEGUNDOS <= 1800

    def test_nao_recarrega_se_o_portal_ja_esta_aberto(self):
        fonte = self._fonte("_open_form")
        assert "parceirosantander" in fonte, (
            "voltou a navegar sem conferir se o operador já está no portal")

    def test_nao_insiste_no_formulario_com_a_sessao_caida(self):
        fonte = self._fonte("_open_form")
        assert fonte.index("Conferir a sessão") < fonte.index("Abrir o formulário direto"), (
            "insistir no formulário com sessão caída recarrega por cima do login")
