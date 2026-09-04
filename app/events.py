"""Barramento de eventos em tempo real.

Substitui o antigo ``EventBus`` de fila unica, que tinha dois defeitos serios:
os eventos publicados antes de alguem abrir o painel ficavam acumulados para
sempre (vazamento de memoria) e, por ser fila e nao broadcast, dependia de um
unico consumidor vivo.

Aqui cada assinante tem a sua propria fila *limitada* (descarta o mais antigo
quando enche), e os eventos operacionais tambem sao gravados em ``events``.
E' essa persistencia que faz o monitor sobreviver a um F5: a tela e' montada a
partir do banco e so' depois passa a receber o fluxo ao vivo.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import Any, Callable, Iterable

from .clock import now_iso
from .db import Database

# Eventos de alta frequencia que so' fazem sentido ao vivo. Nao vao para o
# banco para nao inchar a tabela sem necessidade.
TRANSIENT_TYPES = {"metrics", "queue_update", "whatsapp_status", "heartbeat"}

MAX_SUBSCRIBER_BACKLOG = 500


class Subscription:
    def __init__(self, hub: "EventHub", maxsize: int) -> None:
        self._hub = hub
        self.queue: queue.Queue[dict] = queue.Queue(maxsize=maxsize)
        self.dropped = 0

    def get(self, timeout: float | None = None) -> dict | None:
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def offer(self, event: dict) -> None:
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            # Descarta o mais antigo: para um monitor operacional, o evento
            # recente vale mais do que o atrasado.
            try:
                self.queue.get_nowait()
                self.dropped += 1
                self.queue.put_nowait(event)
            except (queue.Empty, queue.Full):
                pass

    def close(self) -> None:
        self._hub.unsubscribe(self)


class EventHub:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db
        self._lock = threading.RLock()
        self._subscribers: list[Subscription] = []
        self._listeners: list[Callable[[dict], None]] = []

    # ------------------------------------------------------------ assinatura
    def subscribe(self, maxsize: int = MAX_SUBSCRIBER_BACKLOG) -> Subscription:
        sub = Subscription(self, maxsize)
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def add_listener(self, fn: Callable[[dict], None]) -> None:
        with self._lock:
            self._listeners.append(fn)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    # ------------------------------------------------------------ publicacao
    def publish(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        persist: bool | None = None,
        stage: str = "",
        level: str = "info",
        title: str = "",
        detail: str = "",
        request_id: str = "",
        simulation_id: int | None = None,
        consultant_name: str = "",
        chat_id: str = "",
    ) -> dict:
        payload = dict(payload or {})
        payload.setdefault("time", now_iso())

        if persist is None:
            persist = event_type not in TRANSIENT_TYPES

        record = {
            "type": event_type,
            "stage": stage,
            "level": level,
            "title": title,
            "detail": detail,
            "request_id": request_id,
            "simulation_id": simulation_id,
            "consultant_name": consultant_name,
            "chat_id": chat_id,
            "created_at": payload["time"],
        }

        if persist and self._db is not None:
            try:
                row = dict(record)
                row["payload_json"] = json.dumps(payload, ensure_ascii=False, default=str)
                record["id"] = self._db.insert("events", row)
            except Exception:
                # Um evento nunca pode derrubar o fluxo operacional.
                pass

        event = {**record, "payload": payload}
        self._fanout(event)
        return event

    def _fanout(self, event: dict) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
            listeners = list(self._listeners)
        for sub in subscribers:
            sub.offer(event)
        for fn in listeners:
            try:
                fn(event)
            except Exception:
                pass

    # -------------------------------------------------------------- historico
    def recent(self, limit: int = 120, types: Iterable[str] | None = None) -> list[dict]:
        if self._db is None:
            return []
        params: list[Any] = []
        clause = ""
        if types:
            names = list(types)
            clause = f" WHERE type IN ({','.join('?' for _ in names)})"
            params.extend(names)
        rows = self._db.fetchall(
            f"SELECT * FROM events{clause} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )
        out: list[dict] = []
        for row in reversed(rows):
            payload = {}
            if row.get("payload_json"):
                try:
                    payload = json.loads(row["payload_json"])
                except (ValueError, TypeError):
                    payload = {}
            row.pop("payload_json", None)
            out.append({**row, "payload": payload})
        return out

    def timeline(self, request_id: str) -> list[dict]:
        if self._db is None or not request_id:
            return []
        rows = self._db.fetchall(
            "SELECT * FROM events WHERE request_id=? ORDER BY id ASC", (request_id,)
        )
        for row in rows:
            row.pop("payload_json", None)
        return rows
