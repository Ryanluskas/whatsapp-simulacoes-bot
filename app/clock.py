"""Tempo do sistema.

Regra unica e sem excecoes: tudo e' persistido em UTC no formato
``YYYY-MM-DDTHH:MM:SSZ`` (ordenavel lexicograficamente, que e' como o SQLite
compara as colunas de data). Os limites de "hoje", "ontem" e "mes" sao
calculados no fuso local configurado e so' entao convertidos para UTC, para
que um relatorio "de hoje" no Brasil nao comece as 21h do dia anterior.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ISO = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().strftime(ISO)


def iso_atras(segundos: float) -> str:
    """Instante ISO de N segundos atras, para comparar com colunas de tempo.

    As datas ficam guardadas como texto ISO em UTC, entao a comparacao no SQL
    e' lexicografica -- e correta, porque o formato tem largura fixa.
    """
    return to_iso(utc_now() - timedelta(seconds=segundos))


def to_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime(ISO)


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def local_now(tz: ZoneInfo) -> datetime:
    return utc_now().astimezone(tz)


def day_bounds(tz: ZoneInfo, offset_days: int = 0) -> tuple[str, str]:
    """Inicio (inclusivo) e fim (exclusivo) de um dia local, em ISO UTC."""
    start_local = (local_now(tz) + timedelta(days=offset_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return to_iso(start_local), to_iso(start_local + timedelta(days=1))


def month_bounds(tz: ZoneInfo) -> tuple[str, str]:
    now_local = local_now(tz)
    start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return to_iso(start_local), to_iso(now_local)


def range_bounds(
    period: str,
    tz: ZoneInfo,
    start: str | None = None,
    end: str | None = None,
) -> tuple[str, str]:
    """Traduz um periodo nomeado do painel para uma janela [inicio, fim) em UTC."""
    now_local = local_now(tz)

    if period == "today":
        return day_bounds(tz, 0)
    if period == "yesterday":
        return day_bounds(tz, -1)
    if period == "month":
        return month_bounds(tz)
    if period in {"7d", "30d", "90d"}:
        days = int(period[:-1])
        start_local = (now_local - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return to_iso(start_local), to_iso(now_local + timedelta(minutes=1))
    if period == "custom":
        a = _custom_edge(start, tz, end_of_day=False) or to_iso(now_local - timedelta(days=30))
        b = _custom_edge(end, tz, end_of_day=True) or to_iso(now_local + timedelta(minutes=1))
        return (a, b) if a <= b else (b, a)

    start_local = (now_local - timedelta(days=29)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return to_iso(start_local), to_iso(now_local + timedelta(minutes=1))


def _custom_edge(value: str | None, tz: ZoneInfo, end_of_day: bool) -> str | None:
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:  # YYYY-MM-DD
            moment = datetime.fromisoformat(text)
            moment = moment + timedelta(days=1) if end_of_day else moment
        else:
            moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return to_iso(moment)


def local_hour(iso_value: str | None, tz: ZoneInfo) -> int | None:
    moment = parse_iso(iso_value)
    return moment.astimezone(tz).hour if moment else None


def local_day(iso_value: str | None, tz: ZoneInfo) -> str | None:
    moment = parse_iso(iso_value)
    return moment.astimezone(tz).strftime("%Y-%m-%d") if moment else None


def humanize_uptime(since_iso: str | None) -> str:
    moment = parse_iso(since_iso)
    if not moment:
        return "-"
    total = int((utc_now() - moment).total_seconds())
    if total < 0:
        return "-"
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h {minutes}min"
    if hours:
        return f"{hours}h {minutes}min"
    return f"{minutes}min"
