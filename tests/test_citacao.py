"""A regra da citação: uma só, e ela decide.

Cada teste aqui corresponde a uma forma de a resposta sair ligada à mensagem
errada -- ou a nenhuma. Nenhum deles toca rede, tela ou banco: a regra é pura
de propósito, porque é ela que as duas camadas (dom e evolution) consultam.
"""

from __future__ import annotations

import pytest

from app.citacao import (APROVADO, QUOTE_ALVO_AUSENTE, QUOTE_CHAT_MISMATCH,
                         QUOTE_ID_MISMATCH, QUOTE_SEM_CHAT_ID,
                         QUOTE_SEM_ORIGIN_MESSAGE_ID, QUOTE_SEM_REQUEST_ID,
                         Conferencia, Origem, conferir_integridade, linha_de_log)

GRUPO = "120363000000000001@g.us"
ORIGEM = Origem(request_id="REQ000001", message_id="3EB0AAAA1111",
                chat_id=GRUPO, participant="5562900000001@s.whatsapp.net")


class TestOCaminhoCerto:
    def test_alvo_igual_a_origem_passa(self):
        assert conferir_integridade(ORIGEM, alvo=ORIGEM.message_id, chat_id=GRUPO)

    def test_sem_citacao_ainda_confere_a_conversa(self):
        """Resposta sem citação continua não podendo ir para outro grupo."""
        assert conferir_integridade(ORIGEM, alvo="", chat_id=GRUPO, com_citacao=False)
        recusa = conferir_integridade(ORIGEM, alvo="", chat_id="outro@g.us",
                                      com_citacao=False)
        assert not recusa and recusa.motivo == QUOTE_CHAT_MISMATCH


class TestOQueNuncaPodeSair:
    def test_citar_outra_mensagem_e_bloqueado(self):
        """O defeito que este módulo existe para impedir."""
        recusa = conferir_integridade(ORIGEM, alvo="3EB0OUTRA9999", chat_id=GRUPO)
        assert not recusa
        assert recusa.motivo == QUOTE_ID_MISMATCH
        assert "3EB0OUTRA9999" in recusa.detalhe and ORIGEM.message_id in recusa.detalhe

    def test_pedir_citacao_sem_alvo_e_bloqueado(self):
        """"Cita alguma coisa" é como se responde à última mensagem por engano."""
        recusa = conferir_integridade(ORIGEM, alvo="", chat_id=GRUPO)
        assert not recusa and recusa.motivo == QUOTE_ALVO_AUSENTE

    def test_resposta_em_outra_conversa_e_bloqueada(self):
        recusa = conferir_integridade(ORIGEM, alvo=ORIGEM.message_id,
                                      chat_id="120363000000000999@g.us")
        assert not recusa and recusa.motivo == QUOTE_CHAT_MISMATCH

    @pytest.mark.parametrize("campo,motivo", [
        ("request_id", QUOTE_SEM_REQUEST_ID),
        ("message_id", QUOTE_SEM_ORIGIN_MESSAGE_ID),
        ("chat_id", QUOTE_SEM_CHAT_ID),
    ])
    def test_origem_incompleta_nao_passa(self, campo, motivo):
        """Sem identidade gravada não há o que citar -- e não se adivinha."""
        from dataclasses import replace

        incompleta = replace(ORIGEM, **{campo: ""})
        recusa = conferir_integridade(incompleta, alvo=ORIGEM.message_id, chat_id=GRUPO)
        assert not recusa and recusa.motivo == motivo


class TestOrigemVemDoBanco:
    def test_da_linha_le_a_solicitacao_gravada(self):
        linha = {"request_id": "REQ000007", "source_message_id": "2AF4BBBB",
                 "chat_id": GRUPO, "participant": "5562900000002@s.whatsapp.net",
                 "customer_name": "Cliente Teste", "cpf": "52998224725"}
        origem = Origem.da_linha(linha)
        assert origem.request_id == "REQ000007"
        assert origem.message_id == "2AF4BBBB"
        assert origem.chat_id == GRUPO
        assert origem.completa

    def test_linha_sem_origem_nao_finge_estar_completa(self):
        origem = Origem.da_linha({"request_id": "REQ000008", "chat_id": GRUPO})
        assert not origem.completa
        assert not conferir_integridade(origem, alvo="qualquer", chat_id=GRUPO)

    def test_nao_ha_atalho_a_partir_da_tela(self):
        """`Origem` só tem um construtor auxiliar, e ele lê a linha do banco."""
        construtores = [nome for nome in vars(Origem)
                        if nome.startswith(("de_", "da_", "from_"))]
        assert construtores == ["da_linha"], (
            f"apareceu outro caminho para montar a origem: {construtores}")


class TestOLogExplicaSozinho:
    def test_sucesso_tem_os_ids_que_permitem_conferir(self):
        linha = linha_de_log(ORIGEM, alvo=ORIGEM.message_id, chat_id=GRUPO,
                             estrategia="dom", quote_status="ok",
                             delivery_status="delivered", wa_message_id="3EB0RESPOSTA")
        for pedaco in ("REQ000001", "origin_message_id=3EB0AAAA1111",
                       "quoted_message_id=3EB0AAAA1111", "strategy=dom",
                       "quote_status=ok", "delivery_status=delivered",
                       "wa_message_id=3EB0RESPOSTA"):
            assert pedaco in linha, f"falta {pedaco!r} em: {linha}"

    def test_falha_diz_o_motivo_estruturado(self):
        recusa = conferir_integridade(ORIGEM, alvo="3EB0OUTRA", chat_id=GRUPO)
        linha = linha_de_log(ORIGEM, alvo="3EB0OUTRA", chat_id=GRUPO,
                             estrategia="dom", quote_status="mismatch",
                             motivo=recusa.motivo)
        assert "motivo=QUOTE_ID_MISMATCH" in linha
        assert "quoted_message_id=3EB0OUTRA" in linha

    def test_o_log_nao_leva_dado_de_cliente(self):
        """O log vai para o painel e para arquivo; CPF e nome não entram."""
        linha = linha_de_log(ORIGEM, alvo=ORIGEM.message_id, chat_id=GRUPO,
                             estrategia="evolution", quote_status="ok")
        assert "52998224725" not in linha and "Cliente" not in linha
        assert ORIGEM.participant not in linha, "o telefone do consultor vazou no log"


class TestVeredito:
    def test_conferencia_e_booleana(self):
        assert bool(APROVADO) is True
        assert bool(Conferencia(False, QUOTE_ID_MISMATCH)) is False
