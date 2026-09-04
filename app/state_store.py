"""Memoria das mensagens ja' vistas.

Duas mudancas em relacao a versao anterior:

* Linha de base por conversa. Sem ela, uma instalacao nova (ou um
  ``state.json`` apagado) fazia o bot ler as ultimas 30 mensagens do grupo e
  responder todas como se fossem pedidos novos.
* Escrita atomica e com limite. O arquivo e' gravado num temporario e so'
  entao substitui o original, para que uma queda no meio da gravacao nao deixe
  um JSON truncado - que a versao antiga tratava como "nunca vi nada".
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from pathlib import Path

MAX_SEEN = 5000
FLUSH_EVERY = 10


class StateStore:
    def __init__(self, path: Path, max_seen: int = MAX_SEEN) -> None:
        self.path = Path(path)
        self._max_seen = max_seen
        self._lock = threading.RLock()
        self._order: deque[str] = deque(maxlen=max_seen)
        self._seen: set[str] = set()
        self._baselined: set[str] = set()
        self._pending_writes = 0
        self._load()

    # ---------------------------------------------------------------- leitura
    def has_seen(self, message_id: str) -> bool:
        with self._lock:
            return message_id in self._seen

    def is_baselined(self, chat_id: str) -> bool:
        with self._lock:
            return chat_id in self._baselined

    # ---------------------------------------------------------------- escrita
    def flush(self) -> None:
        """Grava o que ficou pendente. Usado depois de marcar varios ids."""
        self._save()

    def mark_seen(self, message_id: str, flush: bool = True) -> None:
        if not message_id:
            return
        with self._lock:
            if message_id in self._seen:
                return
            if len(self._order) == self._order.maxlen:
                self._seen.discard(self._order[0])
            self._order.append(message_id)
            self._seen.add(message_id)
            self._pending_writes += 1
            should_write = flush and self._pending_writes >= 1
        if should_write:
            self._save()

    def baseline_if_new(self, chat_id: str, message_ids: list[str]) -> bool:
        """Marca todas as mensagens visiveis como vistas na primeira leitura.

        Devolve True se a linha de base acabou de ser criada (ou seja: nada
        deste ciclo deve ser processado).
        """
        if not chat_id:
            return False
        with self._lock:
            if chat_id in self._baselined:
                return False
            self._baselined.add(chat_id)
            for message_id in message_ids:
                if message_id and message_id not in self._seen:
                    if len(self._order) == self._order.maxlen:
                        self._seen.discard(self._order[0])
                    self._order.append(message_id)
                    self._seen.add(message_id)
        self._save()
        return True

    def reset_baseline(self, chat_id: str = "") -> None:
        with self._lock:
            if chat_id:
                self._baselined.discard(chat_id)
            else:
                self._baselined.clear()
        self._save()

    # ------------------------------------------------------------------- disco
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return
        seen = [str(x) for x in data.get("seen_message_ids", []) if x]
        with self._lock:
            self._order = deque(seen[-self._max_seen:], maxlen=self._max_seen)
            self._seen = set(self._order)
            self._baselined = {str(x) for x in data.get("baselined_chats", []) if x}

    def _save(self) -> None:
        with self._lock:
            payload = {
                "seen_message_ids": list(self._order),
                "baselined_chats": sorted(self._baselined),
            }
            self._pending_writes = 0
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
