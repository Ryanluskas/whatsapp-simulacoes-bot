"""O que o banco diz de UMA solicitacao -- so' campos seguros.

Uso::

    .venv/Scripts/python.exe ferramentas/observar_entrega.py REQ000123
    .venv/Scripts/python.exe ferramentas/observar_entrega.py --ultima

Feito para o ROTEIRO-TESTE-REAL.md: responde "o pedido virou solicitacao?",
"a entrega saiu, ficou incerta ou vai repetir?", "a citacao pegou?" e "qual id
o WhatsApp deu?" sem abrir o banco na mao.

Nunca imprime CPF, nome do cliente, telefone, JID, texto das mensagens nem o
motivo cru do portal (que pode citar o CPF). Os ids de mensagem saem inteiros:
sao eles que ligam a linha do banco a mensagem no celular.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

#: So' isto sai da tabela ``simulations``.
CAMPOS_DA_SOLICITACAO = (
    "request_id", "status", "stage", "result_ok", "delivery_status", "quote_status",
    "media_status", "sent_message_id", "source_message_id", "reply_attempts",
    "delivery_resolution", "delivery_resolved_by", "delivery_resolved_at",
    "created_at", "finished_at", "replied_at", "updated_at",
)
#: E isto de cada linha de ``messages``.
CAMPOS_DA_MENSAGEM = (
    "direction", "kind", "status", "attempt", "provider", "http_status", "desfecho",
    "quote_status", "origin_message_id", "quoted_message_id", "wa_message_id", "created_at",
)


def _colunas(conn: sqlite3.Connection, tabela: str) -> set[str]:
    return {linha[1] for linha in conn.execute(f"PRAGMA table_info({tabela})")}


def observar(db_path: str | Path, request_id: str = "", ultima: bool = False) -> dict:
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        existentes = _colunas(conn, "simulations")
        campos = [c for c in CAMPOS_DA_SOLICITACAO if c in existentes]
        onde, args = (("ORDER BY id DESC LIMIT 1", ()) if ultima
                      else ("WHERE request_id=?", (request_id,)))
        linha = conn.execute(f"SELECT id, {', '.join(campos)} FROM simulations {onde}",
                             args).fetchone()
        if not linha:
            return {"encontrada": False, "request_id": request_id}
        de_mensagem = [c for c in CAMPOS_DA_MENSAGEM if c in _colunas(conn, "messages")]
        mensagens = conn.execute(
            f"SELECT {', '.join(de_mensagem)} FROM messages "
            "WHERE simulation_id=? OR (request_id=? AND request_id<>'') ORDER BY id",
            (linha["id"], linha["request_id"])).fetchall()
        eventos = conn.execute(
            "SELECT type, stage, level, title, created_at FROM events "
            "WHERE request_id=? ORDER BY id", (linha["request_id"],)).fetchall()
        return {
            "encontrada": True,
            "solicitacao": {c: linha[c] for c in campos},
            "mensagens": [dict(m) for m in mensagens],
            "eventos": [dict(e) for e in eventos],
        }
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("request_id", nargs="?", default="")
    parser.add_argument("--ultima", action="store_true", help="a solicitacao mais recente")
    parser.add_argument("--db", default="", help="caminho do banco (padrao: DB_PATH do .env)")
    args = parser.parse_args(argv)
    if not args.request_id and not args.ultima:
        parser.error("informe o REQ ou --ultima")
    db = args.db
    if not db:
        from app.config import load_config
        db = str(load_config().db_path)
    resultado = observar(db, args.request_id, ultima=args.ultima)
    print(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
    return 0 if resultado["encontrada"] else 1


if __name__ == "__main__":
    sys.exit(main())
