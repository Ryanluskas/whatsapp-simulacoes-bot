"""Acesso ao SQLite.

Duas decisoes importantes em relacao a versao anterior:

1. Uma conexao *por thread* (thread-local) em vez de abrir e fechar uma
   conexao a cada consulta. O codigo antigo serializava tudo num RLock global
   e pagava o custo de abrir o arquivo em toda query.
2. WAL ligado, o que permite leituras concorrentes enquanto o worker escreve.
   As escritas continuam serializadas por um lock explicito, porque todos os
   escritores vivem neste processo e assim evitamos SQLITE_BUSY.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .clock import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS consultants (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    wa_id         TEXT UNIQUE,
    phone         TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    pinned_name   INTEGER NOT NULL DEFAULT 0,
    notes         TEXT DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

CREATE TABLE IF NOT EXISTS simulations (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id         TEXT NOT NULL UNIQUE,
    consultant_id      INTEGER,
    consultant_name    TEXT,
    chat_id            TEXT,
    sender_id          TEXT,
    sender_name        TEXT,
    source_message_id  TEXT,
    cpf                TEXT,
    bank               TEXT,
    contract           TEXT,
    phone              TEXT,
    customer_name      TEXT,
    origin             TEXT DEFAULT '',
    simulation_type    TEXT DEFAULT 'consignado',
    status             TEXT NOT NULL DEFAULT 'queued',
    stage              TEXT NOT NULL DEFAULT 'queued',
    error_message      TEXT DEFAULT '',
    refin              TEXT DEFAULT '',
    reduction_value    REAL DEFAULT 0,
    margin             TEXT DEFAULT '',
    installment_sum    REAL DEFAULT 0,
    installment_count  INTEGER DEFAULT 0,
    debt_sum           REAL DEFAULT 0,
    contracts_count    INTEGER DEFAULT 0,
    contracts_json     TEXT DEFAULT '',
    motivos_portal     TEXT DEFAULT '',
    attempts           INTEGER NOT NULL DEFAULT 0,
    max_attempts       INTEGER NOT NULL DEFAULT 2,
    raw_message        TEXT,
    created_at         TEXT NOT NULL,
    queued_at          TEXT,
    started_at         TEXT,
    finished_at        TEXT,
    replied_at         TEXT,
    processing_seconds REAL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    simulation_id   INTEGER,
    request_id      TEXT,
    direction       TEXT NOT NULL,
    kind            TEXT DEFAULT 'text',
    chat_id         TEXT,
    wa_message_id   TEXT,
    consultant_id   INTEGER,
    consultant_name TEXT,
    sender_id       TEXT,
    text            TEXT,
    media_path      TEXT,
    status          TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    type            TEXT NOT NULL,
    stage           TEXT DEFAULT '',
    level           TEXT DEFAULT 'info',
    title           TEXT DEFAULT '',
    detail          TEXT DEFAULT '',
    request_id      TEXT DEFAULT '',
    simulation_id   INTEGER,
    consultant_name TEXT DEFAULT '',
    chat_id         TEXT DEFAULT '',
    payload_json    TEXT DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    level      TEXT NOT NULL,
    service    TEXT,
    message    TEXT,
    request_id TEXT,
    consultant TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Os indices ficam separados de proposito: varios apontam para colunas que so'
# existem depois do migrador rodar. Criar tudo num script so' quebrava a
# abertura de um banco da versao anterior com "no such column: wa_message_id".
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sim_created     ON simulations(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sim_status      ON simulations(status);
CREATE INDEX IF NOT EXISTS idx_sim_consultant  ON simulations(consultant_name);
CREATE INDEX IF NOT EXISTS idx_sim_cpf         ON simulations(cpf);
CREATE INDEX IF NOT EXISTS idx_sim_contract    ON simulations(contract);
CREATE INDEX IF NOT EXISTS idx_msg_created     ON messages(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_msg_simulation  ON messages(simulation_id);
CREATE INDEX IF NOT EXISTS idx_msg_waid        ON messages(wa_message_id);
CREATE INDEX IF NOT EXISTS idx_sim_source_msg  ON simulations(source_message_id);
CREATE INDEX IF NOT EXISTS idx_sim_delivery    ON simulations(delivery_status);
CREATE INDEX IF NOT EXISTS idx_evt_created     ON events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_evt_request     ON events(request_id);
CREATE INDEX IF NOT EXISTS idx_log_created     ON logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_log_level       ON logs(level);
"""

# Colunas acrescentadas depois da primeira versao do schema. O migrador roda
# em todo boot e e' idempotente, entao um banco antigo e' atualizado sem perder
# nada do que ja' estava gravado.
MIGRATIONS: dict[str, dict[str, str]] = {
    "consultants": {
        "wa_id": "TEXT",
        "pinned_name": "INTEGER NOT NULL DEFAULT 0",
        "notes": "TEXT DEFAULT ''",
        "updated_at": "TEXT",
        "last_seen_at": "TEXT",
    },
    "simulations": {
        # Quantas vezes ja' tentamos ENTREGAR o resultado ao consultor. Sem
        # isto, uma resposta que falhava se perdia em silencio.
        "reply_attempts": "INTEGER NOT NULL DEFAULT 0",
        "chat_id": "TEXT",
        "source_message_id": "TEXT",
        "customer_name": "TEXT",
        "origin": "TEXT DEFAULT ''",
        "simulation_type": "TEXT DEFAULT 'consignado'",
        "stage": "TEXT NOT NULL DEFAULT 'queued'",
        "refin": "TEXT DEFAULT ''",
        "contracts_json": "TEXT DEFAULT ''",
        # O que o portal disse, literal, em JSON. Guardado para o painel e
        # para o reenvio poderem repetir o MOTIVO em vez da frase generica.
        "motivos_portal": "TEXT DEFAULT ''",
        "max_attempts": "INTEGER NOT NULL DEFAULT 2",
        "queued_at": "TEXT",
        "replied_at": "TEXT",
        "updated_at": "TEXT",
        # --- origem: o que a resposta precisa, sem depender de memoria ---
        # Com isto a citacao sobrevive a reinicio: o texto citado e o autor
        # vinham de um dicionario em RAM, e depois de reiniciar a citacao
        # saia vazia.
        "chat_name": "TEXT DEFAULT ''",
        "participant": "TEXT DEFAULT ''",
        "source_timestamp": "TEXT DEFAULT ''",
        # --- entrega, separada do resultado (ver models.Delivery) ---
        # 1/0 quando a simulacao terminou; NULL enquanto nao. E' o que impede
        # um reinicio no meio da ENTREGA de simular de novo no portal.
        "result_ok": "INTEGER",
        "delivery_status": "TEXT DEFAULT ''",
        "delivery_error": "TEXT DEFAULT ''",
        "next_delivery_at": "TEXT",
        "sent_message_id": "TEXT DEFAULT ''",
        "quote_status": "TEXT DEFAULT ''",
        # Por que a CITACAO foi recusada. Separado de delivery_error: citacao
        # recusada nao e' entrega falha (ver models.QuoteStatus).
        "quote_error": "TEXT DEFAULT ''",
        "media_status": "TEXT DEFAULT ''",
        # Quem desempatou uma entrega incerta no painel, e como:
        # "manual:chegou" | "manual:nao_chegou". Vazio = ninguem precisou.
        "delivery_resolution": "TEXT DEFAULT ''",
        "delivery_resolved_by": "TEXT DEFAULT ''",
        "delivery_resolved_at": "TEXT DEFAULT ''",
    },
    "messages": {
        "request_id": "TEXT",
        "kind": "TEXT DEFAULT 'text'",
        "chat_id": "TEXT",
        "wa_message_id": "TEXT",
        "consultant_id": "INTEGER",
        "media_path": "TEXT",
        # --- entrada: tudo que identifica a mensagem original ---
        "chat_name": "TEXT DEFAULT ''",
        "sender_name": "TEXT DEFAULT ''",
        "participant": "TEXT DEFAULT ''",
        "wa_timestamp": "TEXT DEFAULT ''",
        # --- saida: evidencia de cada tentativa de envio ---
        "provider": "TEXT DEFAULT ''",
        "attempt": "INTEGER",
        "origin_message_id": "TEXT DEFAULT ''",
        "quoted_message_id": "TEXT DEFAULT ''",
        "quote_status": "TEXT DEFAULT ''",
        "quote_error": "TEXT DEFAULT ''",
        # Como a requisicao terminou (models.Desfecho): entregue, incerta,
        # transitoria, recusada... E' o que diz se a mensagem PODE ter saido.
        "desfecho": "TEXT DEFAULT ''",
        "http_status": "INTEGER",
        "media_id": "TEXT DEFAULT ''",
        "error": "TEXT DEFAULT ''",
        "response_excerpt": "TEXT DEFAULT ''",
    },
}

#: Estados da mensagem RECEBIDA (``messages.status`` com ``direction='in'``).
#: ``received`` e' "gravada, ainda nao tratada": e' o que o boot retoma.
ENTRADA_RECEBIDA = "received"
ENTRADA_IGNORADA = "ignored"      # conversa comum, nao e' pedido
ENTRADA_RECUSADA = "rejected"     # pedido sem dado ou de banco nao atendido
ENTRADA_SOLICITACAO = "request"   # virou uma solicitacao (REQ)
ENTRADA_EXPIRADA = "expired"      # ficou sem tratar tempo demais

#: ``messages.status`` de uma SAIDA gravada antes de chamar a API. Se o
#: processo cair durante a chamada, a linha fica assim -- e e' ela que avisa
#: o proximo boot que a mensagem PODE ter saido (ver jobs.QueueService).
ENVIO_EM_CURSO = "sending"


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._init_schema()

    # ------------------------------------------------------------------ conn
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Transacao de escrita. Serializada porque todos os escritores sao nossos."""
        with self._write_lock:
            conn = self.connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except Exception:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # ---------------------------------------------------------------- schema
    def _init_schema(self) -> None:
        with self._write_lock:
            conn = self.connection()
            conn.executescript(SCHEMA)
            self._migrate(conn)      # acrescenta colunas que faltam...
            conn.executescript(INDEXES)  # ...antes de indexar qualquer uma delas
            self._backfill(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _backfill(self, conn: sqlite3.Connection) -> None:
        stamp = now_iso()
        conn.execute("UPDATE simulations SET updated_at=created_at WHERE updated_at IS NULL")
        conn.execute("UPDATE simulations SET stage=status WHERE stage IS NULL OR stage=''")
        conn.execute("UPDATE consultants SET updated_at=created_at WHERE updated_at IS NULL")
        # A versao antiga guardava a string "false"/"true" em consultants.phone
        # por causa de um split errado do data-id do WhatsApp. Nao ha' como
        # recuperar o numero real, entao limpamos para nao colidir no UNIQUE.
        conn.execute(
            "UPDATE consultants SET wa_id=NULL, phone='' "
            "WHERE phone IN ('false','true') OR wa_id IN ('false','true')"
        )
        # Entregas antigas ja' confirmadas ganham o estado explicito. As nao
        # confirmadas ficam vazias (legado) e seguem a regra antiga do reenvio.
        conn.execute(
            "UPDATE simulations SET delivery_status='delivered' "
            "WHERE replied_at IS NOT NULL AND COALESCE(delivery_status,'')=''"
        )
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_migrated_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (stamp,),
        )

    # ------------------------------------------------------------------ crud
    def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        cur = self.connection().execute(sql, tuple(params))
        return [dict(row) for row in cur.fetchall()]

    def fetchone(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        row = self.connection().execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = 0) -> Any:
        row = self.connection().execute(sql, tuple(params)).fetchone()
        if not row:
            return default
        value = row[0]
        return default if value is None else value

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.write() as conn:
            conn.execute(sql, tuple(params))

    def insert(self, table: str, data: dict) -> int:
        cols = list(data.keys())
        sql = (
            f"INSERT INTO {table} ({','.join(cols)}) "
            f"VALUES ({','.join('?' for _ in cols)})"
        )
        with self.write() as conn:
            return conn.execute(sql, tuple(data.values())).lastrowid

    def update(self, table: str, data: dict, where: dict) -> int:
        set_clause = ",".join(f"{k}=?" for k in data)
        where_clause = " AND ".join(f"{k}=?" for k in where)
        sql = f"UPDATE {table} SET {set_clause} WHERE {where_clause}"
        with self.write() as conn:
            return conn.execute(sql, tuple(data.values()) + tuple(where.values())).rowcount

    # ------------------------------------------------------- mensagem recebida
    def registrar_entrada(self, dados: dict) -> dict:
        """Grava a mensagem recebida UMA vez. Consultar e gravar sao atomicos.

        E' a trava de idempotencia: duas entregas do mesmo webhook (a
        Evolution reentrega quando nao recebe 200 a tempo) nao podem virar
        duas simulacoes -- nem em paralelo, nem depois de um reinicio. A
        versao anterior guardava os ids vistos num dicionario em RAM; um
        reinicio apagava a memoria e a reentrega passava.

        A chave e' (chat_id, wa_message_id). Sem id nao ha' como deduplicar,
        e a mensagem entra sempre.

        Devolve ``{"id", "novo", "status", "simulation_id", "request_id"}``.
        """
        message_id = (dados.get("wa_message_id") or "").strip()
        chat_id = dados.get("chat_id") or ""
        with self.write() as conn:
            if message_id:
                row = conn.execute(
                    "SELECT id, status, simulation_id, request_id FROM messages "
                    " WHERE direction='in' AND wa_message_id=? AND COALESCE(chat_id,'')=? "
                    " ORDER BY id LIMIT 1",
                    (message_id, chat_id),
                ).fetchone()
                if row is not None:
                    return {**dict(row), "novo": False}
            registro = {"direction": "in", "status": ENTRADA_RECEBIDA, **dados}
            cols = list(registro)
            cur = conn.execute(
                f"INSERT INTO messages ({','.join(cols)}) "
                f"VALUES ({','.join('?' for _ in cols)})",
                tuple(registro.values()),
            )
            return {"id": cur.lastrowid, "status": ENTRADA_RECEBIDA,
                    "simulation_id": None, "request_id": None, "novo": True}

    # ------------------------------------------------------------------ meta
    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def get_meta(self, key: str, default: str = "") -> str:
        row = self.fetchone("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row and row["value"] is not None else default

    def bump_meta(self, key: str, delta: int = 1) -> int:
        """Incremento atomico: o contador vive no banco, nao na memoria."""
        with self.write() as conn:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(meta.value AS INTEGER)+? AS TEXT)",
                (key, str(delta), delta),
            )
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return 0

    def get_meta_int(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get_meta(key, str(default)))
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------- manutencao
    def purge_older_than(self, days: int) -> dict[str, int]:
        from datetime import timedelta

        from .clock import to_iso, utc_now

        cutoff = to_iso(utc_now() - timedelta(days=days))
        removed: dict[str, int] = {}
        with self.write() as conn:
            for table in ("events", "logs"):
                cur = conn.execute(f"DELETE FROM {table} WHERE created_at < ?", (cutoff,))
                removed[table] = cur.rowcount
        return removed
