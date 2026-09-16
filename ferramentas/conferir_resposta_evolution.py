"""O que a resposta REAL da Evolution prova sobre o envio e a citação.

Este projeto não inventa o formato da Evolution. Aqui você joga a resposta que
ELA devolveu (a do `curl` do MIGRACAO-EVOLUTION.md, ou a que aparece no log) e
a ferramenta responde, usando as MESMAS funções de produção:

* saiu uma mensagem? (``key.id``, a prova de entrega)
* a citação pegou? (``contextInfo.stanzaId`` igual ao id pedido)

Regra que vale aqui e no bot: **sem ``stanzaId``, a citação fica
``unverified`` -- nunca ``ok``.** Não achar a prova não é prova de sucesso.

Uso::

    .venv/Scripts/python.exe ferramentas/conferir_resposta_evolution.py resposta.json --quote 3EB0ABC
    curl ... | .venv/Scripts/python.exe ferramentas/conferir_resposta_evolution.py - --quote 3EB0ABC

Guarde as capturas FORA do git: elas trazem JID e telefone de verdade
(ver .gitignore). Para rodar o teste de contrato contra elas:

    set EVOLUTION_RESPOSTAS_REAIS=C:\\caminho\\com\\os\\json
    .venv/Scripts/python.exe -m pytest tests/test_contrato_evolution.py -q
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evolution import conferir_citacao, extrair_key_id  # noqa: E402
from app.models import QuoteStatus  # noqa: E402

EXPLICACAO = {
    QuoteStatus.OK: "a resposta prova que a mensagem cita o id pedido",
    QuoteStatus.NOT_APPLIED: "a resposta prova que a mensagem cita OUTRA mensagem",
    QuoteStatus.UNVERIFIED: ("a resposta não traz stanzaId: não dá para afirmar nem "
                             "negar a citação (fica unverified, nunca ok)"),
    QuoteStatus.NONE: "nenhum id foi pedido para citar",
}


def analisar(corpo, quote_message_id: str = "") -> dict:
    enviado_id = extrair_key_id(corpo)
    citacao = conferir_citacao(corpo if isinstance(corpo, dict) else {}, quote_message_id)
    return {
        "sent_message_id": enviado_id,
        "entrega": ("com prova (key.id)" if enviado_id else
                    "SEM prova: 2xx sem key.id vira entrega incerta"),
        "quote_status": citacao,
        "quote_explicacao": EXPLICACAO.get(citacao, ""),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("arquivo", help="JSON da resposta, ou '-' para ler da entrada padrão")
    parser.add_argument("--quote", default="", help="id que foi pedido para citar (key.id do pedido)")
    args = parser.parse_args(argv)

    bruto = sys.stdin.read() if args.arquivo == "-" else Path(args.arquivo).read_text(encoding="utf-8")
    try:
        corpo = json.loads(bruto)
    except ValueError as exc:
        print(f"não é JSON: {exc}")
        print("Um 2xx com corpo ilegível é tratado como ENTREGA INCERTA pelo bot.")
        return 2

    resultado = analisar(corpo, args.quote)
    print(f"sent_message_id : {resultado['sent_message_id'] or '(nenhum)'}")
    print(f"entrega         : {resultado['entrega']}")
    print(f"quote_status    : {resultado['quote_status']}")
    print(f"                  {resultado['quote_explicacao']}")
    if args.quote and resultado["quote_status"] == QuoteStatus.UNVERIFIED:
        print("\nDica: se a sua Evolution devolve o stanzaId em outro lugar, mande "
              "esta resposta junto do relato -- a leitura procura a chave em "
              "qualquer nível, mas não adivinha um campo com outro nome.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
