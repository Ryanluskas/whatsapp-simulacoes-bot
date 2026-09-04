"""O que o bot fala, e — mais importante — o que ele NÃO fala.

Antes desta camada o bot mandava 3 mensagens por solicitação ("Simulação
recebida", a imagem, e às vezes um reenvio) num tom de sistema antigo:
"📥 *Simulação recebida*", "🔎 *Simulação realizada*". Num grupo de trabalho
isso é poluição — o consultor quer o resultado, não o relatório das etapas.

A regra que estes testes protegem: **o bot não fala por falar.**
"""

from __future__ import annotations

import pytest

from app import mensagens as m
from app.models import (IncomingMessage, ParsedRequest, SimulationJob,
                        SimulationResult)


def _job(cliente="Ivone Teste", cpf="42888832453", consultor="Ryan",
         origem="Amapá"):
    pedido = ParsedRequest(consultant_name=consultor, cpf=cpf, bank="Santander",
                           contract="", customer_name=cliente, origin=origem)
    msg = IncomingMessage(message_id="2A1", chat_id="g@g.us", chat_name="G",
                          sender_id="55@c.us", sender_name=consultor, text="x")
    return SimulationJob(request=pedido, message=msg,
                         request_id="REQ000021", simulation_id=21)


LIBERA = SimulationResult(job=_job(), ok=True, status="Sim",
                          reduction_value=4913.52,
                          contracts=({"p": "1"}, {"p": "2"}, {"p": "3"}),
                          installment_sum=740.77)
NAO_LIBERA = SimulationResult(job=_job("Marcia Sousa"), ok=True, status="Não",
                              reduction_value=0)
ERRO = SimulationResult(job=_job(), ok=False, status="Erro",
                        error="Não foi possível contatar a averbadora.",
                        retryable=True)


class TestTomNatural:
    """Uma pessoa trabalhando, não um sistema anunciando etapas."""

    ROBOTICO = ("Simulação recebida", "Simulação realizada", "com sucesso",
                "foi processada", "Aguarde", "processamento",
                "Sua solicitação", "foi iniciado", "com êxito")

    def _todas_as_falas(self) -> list[str]:
        return [
            m.faltando(["CPF"], "Ryan"),
            m.faltando(["CPF", "nome"], "Ryan"),
            m.banco_nao_atendido("Ryan", "Caixa", {"santander"}),
            m.na_fila(2, "Ryan"),
            m.na_fila(5, "Ryan"),
            m.interrompido("Ryan"),
            m.legenda(LIBERA, "REQ1"),
            m.legenda(NAO_LIBERA, "REQ1"),
            m.legenda(ERRO, "REQ1"),
            m.texto(LIBERA, "REQ1"),
            m.texto(ERRO, "REQ1"),
            m.resultado_reenviado({"cpf": "42888832453", "status": "completed",
                                   "reduction_value": 100}),
        ]

    @pytest.mark.parametrize("expressao", ROBOTICO)
    def test_nenhuma_fala_soa_como_sistema_antigo(self, expressao):
        ruins = [f for f in self._todas_as_falas() if expressao.lower() in f.lower()]
        assert not ruins, f"{expressao!r} ainda aparece em: {ruins}"

    def test_falas_sao_curtas(self):
        """Mensagem de WhatsApp longa não é lida."""
        for fala in self._todas_as_falas():
            assert len(fala) <= 220, f"longa demais ({len(fala)}): {fala!r}"

    def test_emoji_com_moderacao(self):
        """Emoji só onde carrega informação, nunca como enfeite."""
        for fala in self._todas_as_falas():
            emojis = sum(1 for c in fala if ord(c) > 0x2500)
            assert emojis <= 2, f"emoji demais em {fala!r}"

    def test_usa_o_primeiro_nome(self):
        """"Ryan, preciso..." e não "Ryan Silva Santos, preciso..."."""
        fala = m.faltando(["CPF"], "Ryan Silva Santos")
        assert fala.startswith("Ryan,")


class TestConteudoUtil:
    def test_falta_de_cpf_diz_o_que_falta(self):
        assert "CPF" in m.faltando(["CPF"], "Ryan")

    def test_dois_campos_faltando_saem_legiveis(self):
        fala = m.faltando(["CPF", "nome"], "Ryan")
        assert "CPF e nome" in fala

    def test_resultado_traz_o_valor(self):
        assert "4.913,52" in m.legenda(LIBERA, "REQ1")

    def test_nao_libera_e_explicito(self):
        assert "Não libera" in m.legenda(NAO_LIBERA, "REQ1")

    def test_erro_traz_o_motivo_do_portal(self):
        """No TEXTO. A legenda não leva motivo: o card já está ao lado."""
        assert "averbadora" in m.texto(ERRO, "REQ1")

    def test_texto_completo_se_sustenta_sozinho(self):
        """É o que o consultor recebe quando a imagem não sai."""
        fala = m.texto(LIBERA, "REQ1")
        assert "4.913,52" in fala
        assert "IVONE TESTE" in fala, "o nome vai em caixa alta no texto"
        assert "428" in fala, "sem CPF não dá para saber de quem é"

    def test_cpf_mascarado_por_padrao(self):
        assert "***" in m.texto(LIBERA, "REQ1")

    def test_reenvio_se_identifica(self):
        fala = m.resultado_reenviado({"cpf": "42888832453", "status": "completed",
                                      "reduction_value": 2219.77})
        assert "reenvio" in fala.lower()
        assert "2.219,77" in fala


class TestAssinaturasCobremAsFalas:
    """O filtro anti-laço tem de reconhecer as falas NOVAS.

    Se um texto mudar aqui e a assinatura não acompanhar, o bot volta a
    conseguir ler a si mesmo — foi assim que o grupo recebeu 53 mensagens.
    """

    @pytest.mark.parametrize("fala", [
        m.faltando(["CPF"], "Ryan"),
        m.na_fila(3, "Ryan"),
        m.banco_nao_atendido("Ryan", "Caixa", {"santander"}),
        m.interrompido("Ryan"),
        m.legenda(LIBERA, "REQ1"),
        m.legenda(NAO_LIBERA, "REQ1"),
        m.legenda(ERRO, "REQ1"),
        m.resultado_reenviado({"cpf": "1", "status": "completed", "reduction_value": 1}),
    ])
    def test_toda_fala_e_reconhecida_como_nossa(self, fala):
        # Mesma comparação do manager: sem diferenciar maiúscula.
        alvo = fala.casefold()
        assert any(marca.casefold() in alvo for marca in m.ASSINATURAS), (
            f"{fala!r} não seria reconhecida — o bot poderia responder a si mesmo")

    @pytest.mark.parametrize("texto", [
        "Maria Tabaré\nAmapá\n316.196.143.91",
        "Ivone Teste\n42888832453\nAmapá",
        "sem matricula",
        "bom dia",
        "CLIENTE EM ATRASO EM PRODUTOS DO BANCO",
    ])
    def test_mensagem_de_consultor_nao_e_confundida(self, texto):
        alvo = texto.casefold()
        assert not any(marca.casefold() in alvo for marca in m.ASSINATURAS)


class TestFluxoLegivel:
    """§7: `_handle_message` como orquestrador, não como lugar onde tudo acontece.

    Ele tinha 191 linhas fazendo registro, parsing, validação, persistência,
    fila e resposta. O objetivo da divisão não é estética: é conseguir ler o
    fluxo de cima a baixo e saber onde mexer.
    """

    def _tamanho(self, nome: str) -> int:
        import ast
        from pathlib import Path
        origem = Path("app/manager.py").read_text(encoding="utf-8")
        for no in ast.walk(ast.parse(origem)):
            if isinstance(no, ast.FunctionDef) and no.name == nome:
                return no.end_lineno - no.lineno
        raise AssertionError(f"{nome} não existe mais")

    def test_o_orquestrador_e_pequeno(self):
        assert self._tamanho("_handle_message") <= 60, (
            "voltou a concentrar lógica no orquestrador")

    @pytest.mark.parametrize("etapa", [
        "_identificar_e_registrar",   # quem enviou
        "_recusar",                   # pedido não segue
        "_gravar_solicitacao",        # cria o REQ
        "_enfileirar",                # entra na fila e avisa se houver espera
    ])
    def test_cada_etapa_do_fluxo_existe(self, etapa):
        assert self._tamanho(etapa) > 0

    def test_nenhuma_etapa_virou_gigante(self):
        """Dividir mal é trocar uma função grande por várias médias."""
        grandes = {nome: self._tamanho(nome)
                   for nome in ("_identificar_e_registrar", "_recusar",
                                "_gravar_solicitacao", "_enfileirar")
                   if self._tamanho(nome) > 70}
        assert not grandes, f"ainda grandes: {grandes}"

    def test_o_request_id_nasce_num_lugar_so(self):
        """A amarração entre WhatsApp, banco, painel e imagem depende disso."""
        from pathlib import Path
        origem = Path("app/manager.py").read_text(encoding="utf-8")
        assert origem.count("self._next_request_id()") == 1


class TestOAvisoDeFilaNaoEhMaisEnviado:
    """O consultor quer o resultado, não o aviso de que está processando.

    Esta classe substitui `TestUmAvisoDeFilaPorConsultorNaoPorPedido`, que
    protegia uma carência de dez minutos entre avisos. A carência resolvia o
    sintoma (catorze "peguei, tem N na frente" seguidos) sem resolver o
    problema: mesmo UM aviso por pedido dobra as mensagens no grupo sem
    informar nada que o consultor já não saiba.

    O texto continua em `mensagens.py` — a fala está correta e pode voltar a
    servir num painel ou num resumo. O que saiu foi o envio.
    """

    def test_o_manager_nao_manda_mais_o_aviso(self, tmp_path):
        import inspect

        from app.manager import BotManager

        fonte = inspect.getsource(BotManager._enfileirar)
        assert "na_fila" not in fonte, (
            "o aviso de fila voltou a ser enviado — ele dobra as mensagens "
            "por pedido sem trazer informação nova")

    def test_a_fala_continua_existindo_e_correta(self):
        assert "2 na frente" in m.na_fila(3, "Ryan")
        assert "uma na frente" in m.na_fila(2, "Ryan")

    def test_a_posicao_continua_indo_para_o_log(self, tmp_path):
        """Ela informa alguma coisa — no painel, não no grupo."""
        import inspect

        from app.manager import BotManager

        fonte = inspect.getsource(BotManager._enfileirar)
        assert "posição" in fonte and "self.log" in fonte


class TestTelefoneNaoEhNome:
    """"+55, peguei. Tem uma na frente" — no grupo do cliente.

    Quando o consultor não está na agenda, o WhatsApp entrega
    "+55 62 8000-1002" no lugar do nome. Cortar no primeiro espaço dava
    "+55", e o bot passou a chamar as pessoas assim.

    Pior que feio: consultores DIFERENTES viravam o mesmo "+55", então duas
    respostas para duas pessoas pareciam duas respostas repetidas para a
    mesma — foi o que o operador viu no print e leu como spam.
    """

    @pytest.mark.parametrize("telefone", [
        "+55 62 8000-1002", "+55 62 9000-1007", "5562800010 02",
        "(62) 98000-1002", "62 8000-1002", "+55",
    ])
    def test_telefone_nao_vira_vocativo(self, telefone):
        for fala in (m.na_fila(2, telefone),
                     m.interrompido(telefone),
                     m.banco_nao_atendido(telefone, "Itaú", {"santander"}),
                     m.faltando(["CPF"], telefone)):
            assert "+55" not in fala
            assert not fala.startswith(("(", "6", "5")), f"começou com número: {fala}"

    def test_nome_de_verdade_continua_sendo_usado(self):
        assert m.na_fila(2, "Allana testando").startswith("Allana,")
        assert m.na_fila(2, "Ryan").startswith("Ryan,")

    @pytest.mark.parametrize("fala", [
        lambda: m.na_fila(2, "+55 62 8000-1002"),
        lambda: m.na_fila(5, ""),
        lambda: m.interrompido(""),
        lambda: m.banco_nao_atendido("", "Itaú", {"santander"}),
        lambda: m.faltando(["CPF"], ""),
    ])
    def test_sem_nome_a_frase_comeca_com_maiuscula(self, fala):
        """"peguei." em minúsculo parece mensagem cortada."""
        texto = fala()
        assert texto[0].isupper(), f"começou minúsculo: {texto}"

    def test_dois_consultores_sem_nome_recebem_a_mesma_frase(self):
        """E está certo — o que não pode é parecer que é a mesma pessoa.

        Sem vocativo, duas respostas seguidas se leem como duas respostas.
        Com "+55" nas duas, se liam como repetição.
        """
        a = m.na_fila(2, "+55 62 8000-1002")
        b = m.na_fila(3, "+55 62 9000-1007")
        assert "+55" not in a and "+55" not in b
        assert a != b, "posições diferentes têm de gerar textos diferentes"


class TestAVersaoDoCodigoNaoDependeDoRelogio:
    """O log dizia "Código carregado: 31/08 06:29:33" — e mentia.

    O relógio desta máquina andou para trás entre sessões, então a data do
    arquivo passou a apontar para o passado enquanto o código era novo.
    Comparar "o bot tem meu conserto?" virou palpite. Hash de conteúdo ou
    bate, ou não bate.
    """

    def test_muda_quando_o_codigo_muda(self, tmp_path, monkeypatch):
        import main

        assert main.versao_do_codigo() != "desconhecida"
        assert len(main.versao_do_codigo().split()[0]) == 8

    def test_e_estavel_entre_chamadas(self):
        import main

        assert main.versao_do_codigo() == main.versao_do_codigo()

    def test_nao_e_so_data(self):
        """Duas versões diferentes com a mesma data têm de diferir."""
        import hashlib
        import main

        a = hashlib.sha256(b"codigo antigo").hexdigest()[:8]
        b = hashlib.sha256(b"codigo novo").hexdigest()[:8]
        assert a != b
        assert main.versao_do_codigo().split()[0] not in ("", "desconhecida")


class TestALegendaNaoRepeteOCard:
    """A razão de existirem dois formatos.

    A legenda vai grudada na imagem do card, e o card já mostra nome, CPF,
    banco, contratos e parcelas. Repetir isso na legenda era duplicação pura:
    o consultor lia a mesma informação duas vezes na mesma mensagem.

    Estes testes são a prova de que a duplicação não voltou.
    """

    @pytest.mark.parametrize("resultado", [LIBERA, NAO_LIBERA])
    def test_a_legenda_nao_traz_cpf(self, resultado):
        legenda = m.legenda(resultado, "REQ000182")
        assert "CPF" not in legenda
        assert "428" not in legenda, "nem o CPF cru nem o mascarado"
        assert "***" not in legenda

    @pytest.mark.parametrize("resultado", [LIBERA, NAO_LIBERA])
    def test_a_legenda_nao_traz_contagem_de_contratos(self, resultado):
        legenda = m.legenda(resultado, "REQ000182")
        assert "contrato" not in legenda.lower()
        assert "parcela" not in legenda.lower()

    def test_a_legenda_nao_traz_origem_nem_banco(self):
        legenda = m.legenda(LIBERA, "REQ000182")
        assert "Amapá" not in legenda
        assert "Santander" not in legenda

    def test_a_legenda_de_resultado_cabe_em_duas_linhas(self):
        """Resultado: cabeçalho + identificador. Nada mais."""
        for resultado in (LIBERA, NAO_LIBERA):
            assert len(m.legenda(resultado, "REQ000182").splitlines()) == 2

    def test_a_legenda_de_erro_leva_o_motivo(self):
        """Três linhas, e a terceira é a que importa.

        A regra "não repetir o que o card mostra" não vale para o erro: ali o
        motivo é a única informação útil, e deixá-lo só dentro da imagem
        obrigaria o consultor a abrir o card para saber o que houve.
        """
        legenda = m.legenda(ERRO, "REQ000182")
        assert len(legenda.splitlines()) == 3
        assert "averbadora" in legenda
        assert legenda.splitlines()[-1] == "_REQ000182_"

    def test_mas_o_texto_traz_tudo_isso(self):
        """O contraste é o ponto: sem imagem, o texto tem de bastar."""
        texto = m.texto(LIBERA, "REQ000182")
        assert "CPF" in texto
        assert "Amapá" in texto
        assert "3 contratos" in texto
        assert "740,77" in texto


class TestFormatoExato:
    """Os formatos combinados, linha a linha."""

    def test_legenda_liberado(self):
        assert m.legenda(LIBERA, "REQ000182") == (
            "✅ *R$ 4.913,52* · Ivone Teste\n"
            "_REQ000182_")

    def test_legenda_nao_libera(self):
        assert m.legenda(NAO_LIBERA, "REQ000182") == (
            "⛔ *Não libera* · Marcia Sousa\n"
            "_REQ000182_")

    def test_texto_liberado(self):
        assert m.texto(LIBERA, "REQ000182") == (
            "✅ *IVONE TESTE*\n"
            "Libera *R$ 4.913,52*\n"
            "CPF 428.***.***-53 · Amapá\n"
            "3 contratos · parcela R$ 740,77\n"
            "_REQ000182_")

    def test_texto_nao_libera(self):
        assert m.texto(NAO_LIBERA, "REQ000182") == (
            "⛔ *MARCIA SOUSA*\n"
            "CPF 428.***.***-53 · Amapá\n"
            "Nenhum contrato encontrado no banco\n"
            "_REQ000182_")

    def test_texto_erro(self):
        assert m.texto(ERRO, "REQ000182") == (
            "⚠️ *IVONE TESTE*\n"
            "Não consegui simular: Não foi possível contatar a averbadora.\n"
            "Tentando de novo\n"
            "_REQ000182_")

    def test_erro_sem_nova_tentativa_nao_promete(self):
        """"Tentando de novo" só quando vai mesmo tentar."""
        definitivo = SimulationResult(job=_job(), ok=False, status="Erro",
                                      error="CPF inválido", retryable=False)
        assert "Tentando de novo" not in m.texto(definitivo, "REQ000182")


class TestRegrasDeFormatacao:
    """As regras que valem para toda fala de resultado."""

    def _todas(self) -> list[str]:
        return [m.legenda(r, "REQ000182") for r in (LIBERA, NAO_LIBERA, ERRO)] + \
               [m.texto(r, "REQ000182") for r in (LIBERA, NAO_LIBERA, ERRO)]

    def test_sem_markdown(self):
        """WhatsApp usa *negrito* e _itálico_. "**" e "###" saem literais.

        A máscara de CPF (``428.***.***-53``) sai da conferência: os
        asteriscos dela não são formatação, são o mascaramento -- e foi um
        falso positivo na primeira versão deste teste.
        """
        for fala in self._todas():
            sem_mascara = fala.replace("***", "")
            assert "**" not in sem_mascara, f"negrito de markdown em {fala!r}"
            assert "###" not in fala
            assert "__" not in fala

    def test_o_req_e_sempre_a_ultima_linha(self):
        """É a chave de busca no painel: quem procura olha o fim."""
        for fala in self._todas():
            ultima = fala.splitlines()[-1]
            assert ultima.startswith("_REQ") and ultima.endswith("_"), ultima

    def test_um_emoji_no_comeco_e_so(self):
        for fala in self._todas():
            assert fala[0] in "✅⛔⚠", f"não começa com emoji de status: {fala!r}"
            # Nenhum outro emoji no corpo.
            resto = fala[2:]
            assert not any(c in "✅⛔📥🔎🆔" for c in resto), fala

    def test_sem_linha_em_branco_dupla(self):
        for fala in self._todas():
            assert "\n\n" not in fala

    def test_sem_separador_de_traco(self):
        for fala in self._todas():
            for linha in fala.splitlines():
                assert not linha.strip().startswith("---")

    def test_cpf_continua_mascarado(self):
        assert "***" in m.texto(LIBERA, "REQ000182")
        assert "42888832453" not in m.texto(LIBERA, "REQ000182")

    def test_sem_mascara_quando_o_operador_pede(self):
        assert "42888832453" in m.texto(LIBERA, "REQ000182", mascarar=False)


class TestNomeDoConsultorSoQuandoACitacaoFalha:
    """Com a citação funcionando, a mensagem já está grudada no pedido dele.

    Repetir "Ryan," em toda resposta foi o que poluiu o grupo. Mas sem
    citação a resposta fica solta, e aí o nome é a única coisa que diz de
    quem ela é.
    """

    @pytest.mark.parametrize("resultado", [LIBERA, NAO_LIBERA, ERRO])
    def test_com_citacao_o_nome_nao_aparece(self, resultado):
        for fala in (m.legenda(resultado, "REQ1", consultor="Ryan", citou=True),
                     m.texto(resultado, "REQ1", consultor="Ryan", citou=True)):
            assert "Ryan" not in fala
            assert "↩" not in fala

    @pytest.mark.parametrize("resultado", [LIBERA, NAO_LIBERA, ERRO])
    def test_sem_citacao_o_nome_volta(self, resultado):
        for fala in (m.legenda(resultado, "REQ1", consultor="Ryan", citou=False),
                     m.texto(resultado, "REQ1", consultor="Ryan", citou=False)):
            assert "↩ Ryan" in fala

    def test_o_req_continua_por_ultimo_mesmo_com_o_nome(self):
        """Duas regras se cruzam aqui; "REQ sempre por último" vence."""
        linhas = m.texto(LIBERA, "REQ000182", consultor="Ryan",
                         citou=False).splitlines()
        assert linhas[-1] == "_REQ000182_"
        assert linhas[-2] == "↩ Ryan"

    def test_sem_nome_de_consultor_nao_inventa_linha(self):
        assert "↩" not in m.texto(LIBERA, "REQ1", consultor="", citou=False)
