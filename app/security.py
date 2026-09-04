"""Sessao assinada, mascaramento de dados sensiveis e limite de tentativas.

A versao anterior guardava as sessoes num ``set`` em memoria (todo mundo era
deslogado a cada reinicio), nunca usava ``SESSION_SECRET`` e nunca aplicava
``MASK_CPF_IN_UI`` - o CPF completo saia na API e nos exports.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time

from .clock import utc_now

SESSION_COOKIE = "sid"
_SEPARATOR = "."


# --------------------------------------------------------------------- sessao
class SessionManager:
    """Token assinado por HMAC. Sem estado no servidor, sobrevive a reinicio."""

    def __init__(self, secret: str, ttl_hours: int = 12) -> None:
        self._secret = (secret or secrets.token_hex(32)).encode("utf-8")
        self._ttl = ttl_hours * 3600
        self._revoked: set[str] = set()
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    def issue(self, subject: str = "admin") -> str:
        issued = int(utc_now().timestamp())
        nonce = secrets.token_hex(8)
        body = f"{subject}{_SEPARATOR}{issued}{_SEPARATOR}{nonce}"
        return f"{body}{_SEPARATOR}{self._sign(body)}"

    def verify(self, token: str | None) -> str | None:
        if not token:
            return None
        parts = token.split(_SEPARATOR)
        if len(parts) != 4:
            return None
        subject, issued_raw, nonce, signature = parts
        body = f"{subject}{_SEPARATOR}{issued_raw}{_SEPARATOR}{nonce}"
        if not hmac.compare_digest(signature, self._sign(body)):
            return None
        try:
            issued = int(issued_raw)
        except ValueError:
            return None
        if int(utc_now().timestamp()) - issued > self._ttl:
            return None
        with self._lock:
            if token in self._revoked:
                return None
        return subject

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._revoked.add(token)
            if len(self._revoked) > 5000:
                self._revoked.clear()

    def _sign(self, body: str) -> str:
        digest = hmac.new(self._secret, body.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def check_password(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256((candidate or "").encode("utf-8")).digest(),
        hashlib.sha256((expected or "").encode("utf-8")).digest(),
    )


# ------------------------------------------------------------- rate limiting
class RateLimiter:
    """Janela deslizante simples, por chave (IP)."""

    def __init__(self, max_attempts: int = 8, window_seconds: int = 300) -> None:
        self._max = max_attempts
        self._window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self._window]
            if len(hits) >= self._max:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 1000:
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self._window]:
                    self._hits.pop(k, None)
            return True

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self._window]
            if not hits:
                return 0
            return max(0, int(self._window - (now - hits[0])))


# ----------------------------------------------------------------- mascaras
def digits_only(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def mask_cpf(cpf: str | None) -> str:
    d = digits_only(cpf)
    if len(d) != 11:
        return "***" if d else "-"
    return f"{d[:3]}.***.***-{d[9:]}"


def format_cpf(cpf: str | None) -> str:
    d = digits_only(cpf)
    if len(d) != 11:
        return cpf or "-"
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


def mask_phone(phone: str | None) -> str:
    d = digits_only(phone)
    if len(d) < 8:
        return "-" if not d else "***"
    return f"+{d[:2]} {d[2:4]} *****-{d[-4:]}" if len(d) >= 12 else f"({d[:2]}) *****-{d[-4:]}"


def redact(text: str | None) -> str:
    """Remove CPFs de textos livres antes de gravar em log."""
    import re

    if not text:
        return ""
    return re.sub(
        r"\b(\d{3})\.?\d{3}\.?\d{3}-?(\d{2})\b",
        lambda m: f"{m.group(1)}.***.***-{m.group(2)}",
        text,
    )
