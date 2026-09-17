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
    """Token assinado por HMAC. Sem estado no servidor.

    Sobrevive a reinicio SO' com ``SESSION_SECRET`` fixo. Vazio, o segredo e'
    sorteado a cada partida: as sessoes antigas deixam de valer.
    """

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
    # Sem senha configurada o painel NAO abre. Antes, DASHBOARD_PASSWORD vazio
    # fazia a senha vazia valer: sha256("") == sha256("").
    if not (expected or "").strip():
        return False
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


# ------------------------------------------------------- configuracao exposta
#: Senhas que nao protegem nada. Qualquer uma delas com o painel na rede e' o
#: mesmo que deixar a porta aberta: o painel tem CPF de cliente.
SENHAS_OBVIAS = {"admin", "", "senha", "123456", "password", "1234", "12345678",
                 "admin123", "changeme", "troque", "mudar"}

#: Placeholders do .env.example. Valem tanto quanto vazio.
SEGREDOS_PLACEHOLDER = {"trocar-este-segredo", "troque-por-um-valor-longo-e-aleatorio",
                        "troque", "changeme", "segredo"}

LOCAIS = {"127.0.0.1", "localhost", "::1"}

#: Tamanhos minimos para o que fica exposto na rede.
MINIMO_SENHA = 10
MINIMO_SEGREDO = 16


def esta_exposto(web_host: str) -> bool:
    """O painel escuta fora do loopback? (Docker publica em 0.0.0.0.)"""
    return (web_host or "").strip() not in LOCAIS


def _fraca(valor: str, minimo: int, proibidos: set[str]) -> bool:
    limpo = (valor or "").strip()
    return (not limpo) or limpo.lower() in proibidos or len(limpo) < minimo


def problema_do_session_secret(valor: str) -> str:
    """O que esta' errado com o ``SESSION_SECRET`` -- ou "" se nada.

    Os tres casos tem riscos DIFERENTES, e o texto antigo ("sem ele qualquer
    um pode forjar um cookie") so' era verdade para dois deles:

    * vazio -- ``SessionManager`` sorteia um segredo novo a cada partida.
      Ninguem forja cookie; o custo e' todo mundo deslogado a cada reinicio
      (e, com mais de um processo, cada um com o seu segredo);
    * placeholder -- o valor esta' no ``.env.example`` publico: quem leu o
      repositorio assina um cookie de sessao valido;
    * curto -- da' para descobrir por forca bruta e, com ele, assinar cookies.
    """
    limpo = (valor or "").strip()
    if not limpo:
        return ("SESSION_SECRET vazio: um segredo novo é sorteado a cada partida. "
                "Não dá para forjar cookie, mas todo mundo é deslogado a cada reinício. "
                "Defina um valor fixo, longo e aleatório.")
    if limpo.lower() in SEGREDOS_PLACEHOLDER:
        return ("SESSION_SECRET é o placeholder público do .env.example: quem leu o "
                "repositório consegue assinar um cookie de sessão válido e entrar no painel.")
    if len(limpo) < MINIMO_SEGREDO:
        return (f"SESSION_SECRET curto ({len(limpo)} caracteres; mínimo {MINIMO_SEGREDO}): "
                "dá para descobri-lo por força bruta e assinar um cookie de sessão válido.")
    return ""


def problemas_de_seguranca(config) -> tuple[list[str], list[str]]:
    """Devolve ``(problemas, avisos)`` da configuracao atual.

    A regra, explicita: **em localhost, defaults passam com aviso; exposto na
    rede, configuracao fraca e' recusa de partida.** Desenvolvimento continua
    funcionando sem cerimonia; producao nao sobe insegura em silencio.
    """
    exposto = esta_exposto(config.web_host)
    achados: list[str] = []

    if not (config.dashboard_password or "").strip():
        achados.append(
            "DASHBOARD_PASSWORD vazio: o painel não aceita login até você definir uma "
            f"senha (mínimo {MINIMO_SENHA} caracteres).")
    elif _fraca(config.dashboard_password, MINIMO_SENHA, SENHAS_OBVIAS):
        achados.append(
            f"DASHBOARD_PASSWORD é fraca ou padrão (mínimo {MINIMO_SENHA} caracteres). "
            "O painel mostra CPF de cliente.")
    problema_da_sessao = problema_do_session_secret(config.session_secret)
    if problema_da_sessao:
        achados.append(problema_da_sessao)
    if getattr(config, "whatsapp_mode", "") == "evolution" and _fraca(
            config.evolution_webhook_token, MINIMO_SEGREDO, SEGREDOS_PLACEHOLDER):
        achados.append(
            f"EVOLUTION_WEBHOOK_TOKEN fraco (mínimo {MINIMO_SEGREDO}). A rota do "
            "webhook recebe dado de cliente e enfileira trabalho.")
    if getattr(config, "simulator_mode", "") == "remote" and _fraca(
            config.agent_token, MINIMO_SEGREDO, SEGREDOS_PLACEHOLDER):
        achados.append(
            f"AGENT_TOKEN fraco (mínimo {MINIMO_SEGREDO}). Com ele se reivindica "
            "simulação e se devolve resultado.")

    return (achados, []) if exposto else ([], achados)


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


_CPF_EM_TEXTO = None


def redact(text: str | None) -> str:
    """Remove CPFs de textos livres antes de gravar em log.

    Aceita os separadores que o grupo realmente usa -- ``529.982.247-25``,
    ``182.841.754.87``, ``118 902 594 97``, ``31611176034``. A versao anterior
    so' reconhecia ``-`` antes dos dois ultimos digitos, e o formato com ponto
    (o mais comum no grupo) ia inteiro para o log.
    """
    import re

    global _CPF_EM_TEXTO
    if not text:
        return ""
    if _CPF_EM_TEXTO is None:
        _CPF_EM_TEXTO = re.compile(
            r"(?<![\d.])(\d{3})[.\s]?\d{3}[.\s]?\d{3}[-.\s]?(\d{2})(?![\d])")
    return _CPF_EM_TEXTO.sub(lambda m: f"{m.group(1)}.***.***-{m.group(2)}", text)
