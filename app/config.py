"""Configuracao central do sistema, carregada do .env uma unica vez."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

_TRUTHY = {"1", "true", "sim", "yes", "on"}


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in _TRUTHY


def _path(name: str, default: str) -> Path:
    raw = Path(os.getenv(name, default).strip() or default)
    return raw if raw.is_absolute() else (ROOT / raw)


def _int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(float(os.getenv(name, str(default)).strip()))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


@dataclass(frozen=True)
class Config:
    # --- Simulador (projeto Arqueiro) ---
    sim_bot_path: Path
    simulator_profile_dir: Path
    simulator_enabled: bool
    job_timeout_seconds: float
    max_attempts: int
    default_phone: str
    browser_executable: str
    simulator_mode: str          # 'local' | 'remote'
    agent_token: str

    # --- WhatsApp ---
    whatsapp_group_name: str
    # Como o BOT aparece no grupo. E' o sinal mais confiavel de autoria nesta
    # instalacao: o data-pre-plain-text traz "[20:17, 30/08/2026] <nome>: ",
    # e as classes message-in/message-out simplesmente nao existem aqui.
    bot_self_name: str
    whatsapp_profile_dir: Path
    whatsapp_headless: bool
    require_trigger: bool
    poll_seconds: float
    reply_quote: bool
    send_result_image: bool
    image_show_client_data: bool
    #: "portal" manda o print da tela do Santander; "card" manda
    #: a imagem que montamos. O print e' a tela que o consultor
    #: veria; o card e' a nossa transcricao dela.
    imagem_da_resposta: str

    # --- Camada de WhatsApp: 'dom' (navegador) ou 'evolution' (API) ---
    #
    # As duas coexistem de proposito. O modo 'dom' raspa a tela do WhatsApp
    # Web e e' onde nascem os tres defeitos historicos (nao citar, mandar
    # documento, falhar calado). O modo 'evolution' fala a API, onde citar e
    # mandar imagem sao campos de um JSON. O 'dom' continua sendo o plano de
    # retorno ate' o 'evolution' provar que entrega no grupo real.
    whatsapp_mode: str
    evolution_url: str
    evolution_api_key: str
    evolution_instance: str
    evolution_group_jid: str
    evolution_webhook_token: str

    # --- Alarmes da fila ---
    #
    # Sao dois problemas diferentes: fila grande e' enxurrada (normal, so'
    # demora) e espera longa e' travamento (nao e' normal). Em 01/09 entraram
    # 50 pedidos em 2 minutos, o bot respondeu UM e caiu -- e nada na tela
    # dizia que 49 consultores estavam esperando.
    alerta_fila: int
    alerta_espera_minutos: float

    # --- Regras de negocio ---
    supported_banks: set[str]
    worker_count: int

    # --- Persistencia / painel ---
    db_path: Path
    state_path: Path
    dashboard_password: str
    web_host: str
    web_port: int
    session_secret: str
    session_hours: int
    mask_cpf_in_ui: bool
    timezone: str
    retention_days: int
    #: Só ligue com um proxy de verdade na frente (nginx, Caddy, Cloudflare).
    #: Sem ele, `X-Forwarded-For` é escrito pelo próprio cliente — e o limite
    #: de tentativas de login cairia com um IP inventado por tentativa.
    trust_proxy_header: bool = False
    desktop_mode: bool = False
    #: Onde ficam os PNGs enviados. None = ``<projeto>/comprovantes``.
    comprovantes_dir: Path | None = None

    tz: ZoneInfo = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        try:
            zone = ZoneInfo(self.timezone)
        except Exception:
            zone = ZoneInfo("America/Sao_Paulo")
        object.__setattr__(self, "tz", zone)

    @property
    def bot_module_path(self) -> Path:
        return self.sim_bot_path / "bot.py"

    @property
    def browser_path(self) -> str:
        """Executavel do navegador usado pelos DOIS servicos.

        Um perfil copiado do Brave deve ser aberto pelo Brave: abrir com o
        Chromium do Playwright funciona na maioria dos casos, mas as senhas
        salvas dependem de detalhes da instalacao. Uma fonte de verdade so',
        para WhatsApp e simulador nunca divergirem.
        """
        if self.browser_executable:
            escolhido = Path(self.browser_executable)
            if escolhido.exists():
                return str(escolhido)

        # A busca automatica so' faz sentido no Windows: e' onde existe o Brave
        # com o perfil do operador. No container (Linux) nao ha' Brave nenhum e
        # o Chromium da imagem do Playwright e' o certo — deixar explicito
        # evita depender de "o caminho nao existe" por acaso.
        if os.name != "nt":
            return ""

        for candidato in (
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
            / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
        ):
            if candidato.exists():
                return str(candidato)
        return ""   # sem Brave: cai no Chromium do Playwright

    @property
    def default_bank(self) -> str:
        """Banco assumido quando a mensagem não diz qual.

        O grupo é dedicado a um banco só, então exigir o campo seria burocracia
        sem informação nova.
        """
        return sorted(self.supported_banks)[0].title() if self.supported_banks else "Santander"

    def bank_supported(self, bank: str) -> bool:
        return (bank or "").strip().lower() in self.supported_banks


def load_config(env_file: str | Path | None = None) -> Config:
    load_dotenv(env_file or (ROOT / ".env"), override=False)

    banks = {
        b.strip().lower()
        for b in os.getenv("SUPPORTED_BANKS", "Santander").split(",")
        if b.strip()
    } or {"santander"}

    return Config(
        sim_bot_path=_path("SIM_BOT_PATH", r"C:\Users\Ryyan\Downloads\arqueiro"),
        simulator_profile_dir=_path("SIMULATOR_PROFILE_DIR", ".simulator-profile"),
        simulator_enabled=_flag("SIMULATOR_ENABLED", "true"),
        job_timeout_seconds=_float("JOB_TIMEOUT_SECONDS", 300.0, minimum=30.0),
        max_attempts=_int("MAX_ATTEMPTS", 2, minimum=1, maximum=5),
        # Usado quando o consultor nao informa telefone. O bot do Arqueiro ja'
        # tem um placeholder proprio, mas ele monta o DDD como "00" quando o
        # campo chega vazio, e o portal recusa esse DDD.
        default_phone=("".join(ch for ch in os.getenv("DEFAULT_PHONE", "11999999999") if ch.isdigit())
                       or "11999999999"),
        browser_executable=os.getenv("BROWSER_EXECUTABLE", "").strip(),
        # 'local'  = o simulador roda neste processo (Windows, com o Brave)
        # 'remote' = a fila fica aqui e um agente externo executa (Docker)
        simulator_mode=(os.getenv("SIMULATOR_MODE", "local").strip().lower()
                        if os.getenv("SIMULATOR_MODE", "local").strip().lower() in {"local", "remote"}
                        else "local"),
        agent_token=os.getenv("AGENT_TOKEN", "").strip(),
        whatsapp_group_name=os.getenv("WHATSAPP_GROUP_NAME", "").strip(),
        bot_self_name=os.getenv("BOT_SELF_NAME", "Operacional Capital").strip()
        or "Operacional Capital",
        whatsapp_profile_dir=_path("WHATSAPP_PROFILE_DIR", ".whatsapp-profile"),
        whatsapp_headless=_flag("WHATSAPP_HEADLESS", "false"),
        # O CPF valido e' o gatilho natural. Exigir "fazer simulacao" faria o
        # bot ignorar o formato que o grupo realmente usa.
        require_trigger=_flag("REQUIRE_TRIGGER", "false"),
        poll_seconds=_float("POLL_SECONDS", 3.0, minimum=1.0),
        reply_quote=_flag("REPLY_QUOTE", "true"),
        # Responder com a imagem dos cards em vez de so' texto.
        send_result_image=_flag("SEND_RESULT_IMAGE", "true"),
        # Desconhecido cai em 'dom': o modo novo so' entra quando pedido, nunca
        # por um erro de digitacao no .env.
        whatsapp_mode=(os.getenv("WHATSAPP_MODE", "dom").strip().lower()
                       if os.getenv("WHATSAPP_MODE", "dom").strip().lower()
                       in {"dom", "evolution"} else "dom"),
        evolution_url=os.getenv("EVOLUTION_URL", "http://localhost:8080").strip().rstrip("/"),
        evolution_api_key=os.getenv("EVOLUTION_API_KEY", "").strip(),
        evolution_instance=os.getenv("EVOLUTION_INSTANCE", "allana").strip(),
        evolution_group_jid=os.getenv("EVOLUTION_GROUP_JID", "").strip(),
        evolution_webhook_token=os.getenv("EVOLUTION_WEBHOOK_TOKEN", "").strip(),
        # Nome e CPF do cliente sem mascara NA IMAGEM enviada ao grupo.
        # E' uma chave separada de MASK_CPF_IN_UI de proposito: o painel e a
        # imagem sao publicos diferentes, e a decisao aqui e' do operador.
        image_show_client_data=_flag("IMAGE_SHOW_CLIENT_DATA", "true"),
        imagem_da_resposta=(os.getenv("IMAGEM_DA_RESPOSTA", "portal")
                            .strip().lower() or "portal"),
        alerta_fila=_int("ALERTA_FILA", 10, minimum=1, maximum=500),
        alerta_espera_minutos=_float("ALERTA_ESPERA_MINUTOS", 5.0, minimum=0.5),
        supported_banks=banks,
        worker_count=_int("WORKER_COUNT", 1, minimum=1, maximum=4),
        db_path=_path("DB_PATH", "simulacoes.db"),
        state_path=_path("STATE_PATH", "state.json"),
        dashboard_password=os.getenv("DASHBOARD_PASSWORD", "admin"),
        web_host=os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1",
        web_port=_int("WEB_PORT", 8000, minimum=1, maximum=65535),
        session_secret=os.getenv("SESSION_SECRET", "").strip(),
        session_hours=_int("SESSION_HOURS", 12, minimum=1, maximum=720),
        mask_cpf_in_ui=_flag("MASK_CPF_IN_UI", "true"),
        desktop_mode=_flag("DESKTOP_MODE", "true"),
        timezone=os.getenv("TIMEZONE", "America/Sao_Paulo").strip() or "America/Sao_Paulo",
        retention_days=_int("RETENTION_DAYS", 180, minimum=7),
        trust_proxy_header=_flag("TRUST_PROXY_HEADER", "false"),
        comprovantes_dir=(_path("COMPROVANTES_DIR", "comprovantes")
                          if os.getenv("COMPROVANTES_DIR", "").strip() else None),
    )
