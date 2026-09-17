"""O contrato de leitura da resposta da Evolution.

Duas partes, e a diferença entre elas importa:

1. **Tolerância de formato** (sempre roda): a leitura acha ``key.id`` e
   ``contextInfo.stanzaId`` em níveis diferentes, porque versões da Evolution
   devolvem a mensagem aninhada de jeitos diferentes. Os payloads aqui são
   SINTÉTICOS e estão marcados como tal -- eles provam a tolerância da
   leitura, não que a Evolution responda assim.

2. **Respostas REAIS** (só roda com `EVOLUTION_RESPOSTAS_REAIS` apontando para
   uma pasta com os JSON capturados): a resposta de verdade é analisada pelas
   mesmas funções de produção. As capturas ficam FORA do git -- elas trazem
   JID e telefone reais.

A regra que nenhum dos dois pode quebrar: sem ``stanzaId``, a citação fica
`unverified`. Nunca se inventa um `ok`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.evolution import conferir_citacao, extrair_key_id
from app.models import QuoteStatus

ID_PEDIDO = "3EB0SINTETICO01"


class TestToleranciaDeFormato:
    """Payloads SINTÉTICOS: provam o que a leitura tolera, não o que a API manda."""

    @pytest.mark.parametrize("corpo", [
        {"key": {"id": "BAE5001"}},
        {"key": {"id": "BAE5001"}, "message": {"conversation": "oi"}},
        {"data": {"key": {"id": "BAE5001"}}},                       # resposta embrulhada
    ])
    def test_acha_o_key_id(self, corpo):
        assert extrair_key_id(corpo) == "BAE5001"

    @pytest.mark.parametrize("corpo", [
        {}, {"status": "PENDING"}, {"key": {}}, {"key": {"id": ""}}, [], "texto", None,
    ])
    def test_sem_key_id_nao_inventa(self, corpo):
        assert extrair_key_id(corpo) == ""

    @pytest.mark.parametrize("corpo", [
        # dentro do tipo da mensagem (o formato que a v2 devolve no envio)
        {"message": {"extendedTextMessage": {"contextInfo": {"stanzaId": ID_PEDIDO}}}},
        # imagem
        {"message": {"imageMessage": {"contextInfo": {"stanzaId": ID_PEDIDO}}}},
        # um nível acima
        {"message": {"contextInfo": {"stanzaId": ID_PEDIDO}}},
        # na raiz
        {"contextInfo": {"stanzaId": ID_PEDIDO}},
        # dentro de uma lista
        {"messages": [{"message": {"extendedTextMessage": {
            "contextInfo": {"stanzaId": ID_PEDIDO}}}}]},
    ])
    def test_acha_o_stanza_em_qualquer_nivel(self, corpo):
        assert conferir_citacao(corpo, ID_PEDIDO) == QuoteStatus.OK

    @pytest.mark.parametrize("corpo", [
        {}, {"key": {"id": "BAE5001"}},
        {"key": {"id": "BAE5001"}, "message": {"conversation": "oi"}},
        {"message": {"extendedTextMessage": {"text": "oi"}}},
    ])
    def test_sem_stanza_fica_unverified(self, corpo):
        """Não achar a prova não é prova. Nunca vira `ok`."""
        assert conferir_citacao(corpo, ID_PEDIDO) == QuoteStatus.UNVERIFIED

    def test_stanza_de_outra_mensagem_e_not_applied(self):
        corpo = {"message": {"extendedTextMessage": {
            "contextInfo": {"stanzaId": "3EB0OUTRA"}}}}
        assert conferir_citacao(corpo, ID_PEDIDO) == QuoteStatus.NOT_APPLIED

    def test_a_citacao_aninhada_nao_confunde(self):
        """A mensagem citada pode, ela mesma, citar outra. Vale a mais rasa."""
        corpo = {"message": {"extendedTextMessage": {"contextInfo": {
            "stanzaId": ID_PEDIDO,
            "quotedMessage": {"extendedTextMessage": {
                "contextInfo": {"stanzaId": "3EB0MAIS_ANTIGA"}}}}}}}
        assert conferir_citacao(corpo, ID_PEDIDO) == QuoteStatus.OK

    def test_sem_id_pedido_nao_ha_citacao(self):
        assert conferir_citacao({"message": {}}, "") == QuoteStatus.NONE


class TestRespostasReais:
    """Só roda com capturas de verdade. Sem elas, pula -- não finge que passou."""

    @staticmethod
    def _capturas() -> list[Path]:
        pasta = os.getenv("EVOLUTION_RESPOSTAS_REAIS", "").strip()
        if not pasta or not Path(pasta).is_dir():
            return []
        return sorted(Path(pasta).glob("*.json"))

    def test_as_capturas_reais_sao_lidas(self):
        capturas = self._capturas()
        if not capturas:
            pytest.skip("defina EVOLUTION_RESPOSTAS_REAIS com a pasta dos JSON capturados")

        for caminho in capturas:
            corpo = json.loads(caminho.read_text(encoding="utf-8"))
            # O nome do arquivo pode trazer o id pedido: resposta--<quote_id>.json
            pedido = caminho.stem.split("--", 1)[1] if "--" in caminho.stem else ""
            enviado = extrair_key_id(corpo)
            situacao = conferir_citacao(corpo if isinstance(corpo, dict) else {}, pedido)
            assert enviado, (
                f"{caminho.name}: a resposta real não trouxe key.id onde a leitura "
                "procura — o bot trataria como entrega INCERTA. Ajuste "
                "`extrair_key_id` com este formato.")
            if pedido:
                assert situacao in (QuoteStatus.OK, QuoteStatus.NOT_APPLIED), (
                    f"{caminho.name}: a citação ficou '{situacao}' — a leitura não "
                    "achou stanzaId nesta resposta real.")
