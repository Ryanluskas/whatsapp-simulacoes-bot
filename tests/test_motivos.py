"""O motivo real do portal, literal — e por que a frase genérica era grave.

Toda recusa virava a mesma linha::

    Nenhum contrato encontrado para esse CPF no banco.

Isso é **factualmente errado** na maioria dos casos: um cliente com
``CLIENTE EM ATRASO EM PRODUTOS DO BANCO`` tem contrato — foi recusado por
outro motivo. E a ação do consultor muda por completo conforme o motivo:
pedir a matrícula, orientar a regularizar, ou partir para outro banco.

Com a frase genérica todos viram "não deu", e ele perde tempo ou desiste de
um caso que daria.

As frases testadas aqui vieram dos prints do grupo. São o que o portal
mostra de verdade.
"""

from __future__ import annotations

import json

import pytest

from app import mensagens
from app.models import (IncomingMessage, ParsedRequest, SimulationJob,
                        SimulationResult)
from app.motivos import (DESCONHECIDO, POLITICA, RESTRICAO, SEM_MATRICULA,
                         classificar, desconhecidos, motivos_do_portal,
                         vale_repetir)

#: Exatamente como aparecem na tela do portal, nos prints.
DOS_PRINTS = [
    "RATING RISCOS = 2",
    "CLIENTE EM ATRASO EM PRODUTOS DO BANCO",
    "NEGADO PELA POLITICA DE CREDITO",
    "NAO PASSIVEL A DECISAO MANUAL",
]


def _resultado(motivos=(), libera=0.0, cliente="Joselia Maria de Teste"):
    pedido = ParsedRequest(consultant_name="Ryan", cpf="41576764753",
                           bank="Santander", contract="",
                           customer_name=cliente, origin="Amapá")
    msg = IncomingMessage(message_id="2A1", chat_id="g", chat_name="G",
                          sender_id="55@c.us", sender_name="Ryan", text="x")
    job = SimulationJob(request=pedido, message=msg,
                        request_id="REQ000182", simulation_id=1)
    return SimulationResult(job=job, ok=True,
                            status="Sim" if libera > 0 else "Não",
                            reduction_value=libera, motivos=tuple(motivos))


class TestOTextoChegaIntegro:
    """Copiar literalmente. Nunca parafrasear, traduzir ou resumir."""

    @pytest.mark.parametrize("frase", DOS_PRINTS)
    def test_cada_motivo_dos_prints_chega_inteiro(self, frase):
        """Nos dois canais que o consultor lê: o card e o texto sem imagem.

        A legenda fica fora de propósito — ela é curta, e o card logo acima
        dela já carrega o motivo. Ver `TestALegendaDeRecusaFicaCurta`.
        """
        from app.cards import build_result_html

        resultado = _resultado(motivos_do_portal([frase]))
        assert frase in mensagens.texto(resultado, "REQ000182"), (
            "o portal foi parafraseado no texto")
        assert frase in build_result_html(resultado, "REQ000182"), (
            "o motivo sumiu do card, que é onde o detalhe mora")

    def test_sem_matricula_chega_inteiro(self):
        frase = "Cliente sem matrícula informada"
        assert frase in mensagens.texto(_resultado(motivos_do_portal([frase])),
                                        "REQ000182")

    def test_varios_motivos_aparecem_na_ordem(self):
        """A ordem é informação: o impedimento principal vem primeiro."""
        fala = mensagens.texto(_resultado(motivos_do_portal(DOS_PRINTS)),
                               "REQ000182")
        posicoes = [fala.index(f) for f in DOS_PRINTS]
        assert posicoes == sorted(posicoes), "a ordem do portal foi embaralhada"

    def test_um_motivo_por_linha(self):
        fala = mensagens.texto(_resultado(motivos_do_portal(DOS_PRINTS)),
                               "REQ000182")
        linhas = fala.splitlines()
        for frase in DOS_PRINTS:
            assert frase in linhas, f"{frase!r} não está sozinha numa linha"

    def test_a_caixa_alta_e_preservada(self):
        """O consultor reconhece as frases pela forma como o portal escreve."""
        fala = mensagens.texto(_resultado(motivos_do_portal(DOS_PRINTS)),
                               "REQ000182")
        assert "NEGADO PELA POLITICA DE CREDITO" in fala
        assert "Negado pela política de crédito" not in fala

    def test_sem_comentario_do_bot_no_meio(self):
        """"o consultor sabe ler" — nada de explicação nossa junto."""
        fala = mensagens.texto(_resultado(motivos_do_portal(DOS_PRINTS)),
                               "REQ000182")
        for intrometido in ("ou seja", "isso significa", "recomendo",
                            "sugiro", "provavelmente"):
            assert intrometido not in fala.casefold()

    def test_sem_emoji_no_meio(self):
        fala = mensagens.texto(_resultado(motivos_do_portal(DOS_PRINTS)),
                               "REQ000182")
        corpo = "\n".join(fala.splitlines()[1:])     # fora o emoji de status
        assert not any(c in corpo for c in "✅⛔⚠📥🔎")


class TestNadaEDescartado:
    """O mapa CLASSIFICA. Ele não filtra."""

    def test_motivo_desconhecido_vai_literal(self):
        # Uma frase que o mapa realmente não conhece. A primeira versão
        # deste teste usava "IMPEDIMENTO XYZ", que casa com `restricao` —
        # e a classificação estava certa; o exemplo é que era ruim.
        frase = "CONVENIO SUSPENSO PARA NOVAS OPERACOES"
        achados = motivos_do_portal([frase])
        assert achados[0]["categoria"] == DESCONHECIDO
        assert frase in mensagens.texto(_resultado(achados), "REQ000182"), (
            "desconhecido virou genérico — é justamente o que não pode")

    def test_desconhecido_e_listado_para_o_mapa_crescer(self):
        achados = motivos_do_portal(["CONVENIO SUSPENSO PARA NOVAS OPERACOES"])
        assert desconhecidos(achados) == ["CONVENIO SUSPENSO PARA NOVAS OPERACOES"]

    def test_conhecido_nao_entra_na_lista_de_desconhecidos(self):
        assert desconhecidos(motivos_do_portal(DOS_PRINTS)) == []

    @pytest.mark.parametrize("frase,categoria", [
        ("Cliente sem matrícula", SEM_MATRICULA),
        ("CLIENTE EM ATRASO EM PRODUTOS DO BANCO", RESTRICAO),
        ("RATING RISCOS = 2", RESTRICAO),
        ("NEGADO PELA POLITICA DE CREDITO", POLITICA),
        ("NAO PASSIVEL A DECISAO MANUAL", POLITICA),
    ])
    def test_a_classificacao_dos_prints(self, frase, categoria):
        assert classificar(frase) == categoria


class TestAFraseGenericaSoQuandoEVerdade:
    """"Nenhum contrato encontrado" passou a significar só isso."""

    def test_sem_motivo_nenhum_usa_a_frase(self):
        fala = mensagens.texto(_resultado(motivos=()), "REQ000182")
        assert "Nenhum contrato encontrado no banco" in fala

    def test_com_motivo_a_frase_generica_some(self):
        """Um cliente EM ATRASO tem contrato; dizer o contrário é errado."""
        fala = mensagens.texto(
            _resultado(motivos_do_portal(["CLIENTE EM ATRASO EM PRODUTOS DO BANCO"])),
            "REQ000182")
        assert "Nenhum contrato encontrado" not in fala
        assert "CLIENTE EM ATRASO EM PRODUTOS DO BANCO" in fala

    def test_a_legenda_segue_a_mesma_regra(self):
        com = mensagens.legenda(
            _resultado(motivos_do_portal(["NEGADO PELA POLITICA DE CREDITO"])),
            "REQ000182")
        assert "Nenhum contrato encontrado" not in com

    def test_quando_libera_nada_disso_aparece(self):
        fala = mensagens.texto(_resultado(libera=4913.52), "REQ000182")
        assert "Nenhum contrato" not in fala
        assert "NEGADO" not in fala


class TestRuidoDeTelaNaoViraMotivo:
    """"Carregado" já chegou ao consultor como motivo da falha.

    Ele recebeu "não foi possível simular / Carregado", que não explica nada
    e ainda passa a impressão de que o sistema está confuso. Este é o ÚNICO
    ponto em que um texto é descartado, e a lista é do que já apareceu.
    """

    @pytest.mark.parametrize("ruido", ["Carregado", "Carregando", "Aguarde",
                                       "Processando", "OK", "Resultado",
                                       "Pesquisar", "   "])
    def test_ruido_conhecido_sai(self, ruido):
        assert motivos_do_portal([ruido]) == []

    def test_ruido_junto_de_motivo_nao_leva_o_motivo(self):
        achados = motivos_do_portal(["Carregado", "NEGADO PELA POLITICA DE CREDITO"])
        assert [m["texto"] for m in achados] == ["NEGADO PELA POLITICA DE CREDITO"]

    def test_frase_que_contem_a_palavra_nao_e_ruido(self):
        """"Carregado" sozinho é ruído; dentro de uma frase, não."""
        achados = motivos_do_portal(["Contrato carregado com restrição"])
        assert len(achados) == 1

    def test_duplicata_sai(self):
        """A mesma frase costuma aparecer em dois elementos da tela."""
        achados = motivos_do_portal(["NEGADO PELA POLITICA DE CREDITO"] * 3)
        assert len(achados) == 1


class TestOCpfNaoVazaNoMotivo:
    """A regra de mascaramento vale para tudo que sai — motivo incluído."""

    def test_cpf_dentro_do_motivo_e_mascarado(self):
        achados = motivos_do_portal(["CPF 415.767.647-53 com pendência cadastral"])
        assert "415.767.647-53" not in achados[0]["texto"]
        assert "415.***.***-53" in achados[0]["texto"]

    def test_cpf_sem_pontuacao_tambem(self):
        achados = motivos_do_portal(["Documento 41576764753 irregular"])
        assert "41576764753" not in achados[0]["texto"]

    def test_o_motivo_mascarado_e_o_que_chega_na_mensagem(self):
        achados = motivos_do_portal(["CPF 415.767.647-53 com pendência"])
        fala = mensagens.texto(_resultado(achados), "REQ000182")
        assert "415.767.647-53" not in fala


class TestRepetirSoQuandoAdianta:
    """Repetir uma recusa de política dá a mesma recusa — só atrasa."""

    def test_falha_tecnica_vale_repetir(self):
        assert vale_repetir(motivos_do_portal(
            ["Não foi possível contatar a averbadora"])) is True

    @pytest.mark.parametrize("frase", DOS_PRINTS)
    def test_recusa_de_negocio_nao_vale_repetir(self, frase):
        assert vale_repetir(motivos_do_portal([frase])) is False

    def test_uma_recusa_no_meio_ja_decide(self):
        """Técnica + política: repetir daria a mesma política."""
        assert vale_repetir(motivos_do_portal([
            "Não foi possível contatar a averbadora",
            "NEGADO PELA POLITICA DE CREDITO"])) is False

    def test_sem_motivo_nao_repete(self):
        assert vale_repetir([]) is False


class TestOMotivoSobreviveAoReenvio:
    """A segunda mensagem não pode errar de novo, e diferente."""

    def test_o_reenvio_repete_o_motivo_guardado(self):
        fala = mensagens.resultado_reenviado({
            "cpf": "41576764753", "customer_name": "Joselia",
            "status": "completed", "reduction_value": 0,
            "request_id": "REQ000182",
            "motivos_portal": json.dumps(motivos_do_portal(DOS_PRINTS)),
        })
        for frase in DOS_PRINTS:
            assert frase in fala
        assert "Nenhum contrato encontrado" not in fala

    def test_linha_antiga_sem_a_coluna_nao_quebra(self):
        """Antes desta coluna existir, a frase genérica é tudo o que sabemos."""
        fala = mensagens.resultado_reenviado({
            "cpf": "41576764753", "customer_name": "Joselia",
            "status": "completed", "reduction_value": 0})
        assert "Nenhum contrato encontrado no banco" in fala

    def test_json_corrompido_nao_quebra(self):
        fala = mensagens.resultado_reenviado({
            "cpf": "41576764753", "status": "completed",
            "reduction_value": 0, "motivos_portal": "{não é json"})
        assert "Nenhum contrato encontrado no banco" in fala


class TestOsMotivosSaoGuardados:
    def test_a_coluna_existe(self, tmp_path):
        from app.db import Database

        db = Database(tmp_path / "t.db")
        colunas = {r["name"] for r in db.fetchall("PRAGMA table_info(simulations)")}
        assert "motivos_portal" in colunas

    def test_a_fila_grava_os_motivos(self):
        import inspect

        from app.jobs import QueueService

        fonte = inspect.getsource(QueueService)
        assert "motivos_portal" in fonte, (
            "sem gravar, o reenvio e o painel voltam à frase genérica")


class TestOSimuladorNaoParafraseia:
    def test_a_traducao_antiga_saiu(self):
        """`traduzir_erro_do_portal` transformava o motivo em outra frase."""
        import inspect

        from app.simulator import SimulatorService

        fonte = inspect.getsource(SimulatorService._ler_erro_do_portal)
        assert "traduzir_erro_do_portal" not in fonte

    def test_le_os_motivos_mesmo_sem_erro(self):
        """Uma recusa vem com ok=True; é nela que o motivo importa."""
        import inspect

        from app.simulator import SimulatorService

        fonte = inspect.getsource(SimulatorService._build_result)
        assert "_ler_motivos_do_portal" in fonte
        assert "motivos=" in fonte


class TestOMotivoNoCard:
    """O detalhe mora no card; a legenda fica curta.

    Cheguei a pôr o motivo na legenda de "não libera" também, e era excesso:
    o desenho define legenda curta e card com o detalhe, e repetir nos dois é
    a duplicação que os dois formatos existem para evitar. O erro continua
    sendo exceção — ali não há card com informação nenhuma além do motivo.
    """

    def _html(self, motivos):
        from app.cards import build_result_html

        return build_result_html(_resultado(motivos_do_portal(motivos)),
                                 "REQ000182")

    @pytest.mark.parametrize("frase", DOS_PRINTS)
    def test_o_motivo_aparece_no_card(self, frase):
        assert frase in self._html([frase])

    def test_varios_motivos_na_ordem_do_portal(self):
        html = self._html(DOS_PRINTS)
        posicoes = [html.index(f) for f in DOS_PRINTS]
        assert posicoes == sorted(posicoes)

    def test_com_motivo_o_card_nao_diz_nenhum_contrato(self):
        """As duas frases juntas se contradizem dentro do mesmo card.

        Um cliente "NEGADO PELA POLITICA DE CREDITO" TEM contrato. O bloco
        de tabela vazia dizia "nenhum contrato encontrado" logo abaixo do
        motivo — o mesmo erro factual do §2, sobrevivendo na imagem.
        """
        html = self._html(["NEGADO PELA POLITICA DE CREDITO"])
        assert "Nenhum contrato encontrado" not in html

    def test_sem_motivo_a_frase_volta(self):
        """Sem o portal dizer nada, ela é toda a verdade disponível."""
        from app.cards import build_result_html

        html = build_result_html(_resultado(motivos=()), "REQ000182")
        assert "Nenhum contrato encontrado" in html

    def test_quando_libera_o_card_nao_traz_motivo(self):
        from app.cards import build_result_html

        html = build_result_html(_resultado(libera=4913.52), "REQ000182")
        assert "class='motivos'" not in html

    def test_o_motivo_e_escapado(self):
        """Texto de terceiro entrando em HTML sem escape é injeção."""
        html = self._html(["<script>alert(1)</script> NEGADO"])
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestALegendaDeRecusaFicaCurta:
    """Duas linhas, como especificado — o detalhe está no card."""

    def test_recusa_com_motivo_ainda_tem_duas_linhas(self):
        legenda = mensagens.legenda(_resultado(motivos_do_portal(DOS_PRINTS)),
                                    "REQ000182")
        assert len(legenda.splitlines()) == 2

    def test_a_legenda_nao_repete_o_motivo_do_card(self):
        legenda = mensagens.legenda(
            _resultado(motivos_do_portal(["NEGADO PELA POLITICA DE CREDITO"])),
            "REQ000182")
        assert "NEGADO" not in legenda

    def test_mas_o_texto_sem_imagem_traz(self):
        """Sem card, o texto é tudo que o consultor recebe."""
        fala = mensagens.texto(
            _resultado(motivos_do_portal(["NEGADO PELA POLITICA DE CREDITO"])),
            "REQ000182")
        assert "NEGADO PELA POLITICA DE CREDITO" in fala
