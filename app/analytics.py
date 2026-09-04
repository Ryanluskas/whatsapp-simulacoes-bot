"""Metricas e relatorios.

Tudo e' calculado sobre os registros reais de ``simulations``/``messages``.
Nao ha' numero inventado em lugar nenhum: se uma serie nao tem dados, ela volta
vazia e o painel mostra o estado vazio correspondente.

As datas ficam gravadas em UTC, mas os cortes de "hoje", "por dia" e "por hora"
sao feitos no fuso configurado. Na versao anterior tudo era UTC cru, entao o
relatorio "de hoje" no Brasil comecava as 21h do dia anterior.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .clock import day_bounds, local_now, month_bounds, range_bounds, to_iso, utc_now
from .db import Database
from .models import STAGE_LABELS, STATUS_LABELS, Status
from .security import mask_cpf


def _tz_modifier(tz: ZoneInfo) -> str:
    """Deslocamento atual do fuso, no formato aceito pelo SQLite."""
    offset = local_now(tz).utcoffset() or timedelta(0)
    return f"{int(offset.total_seconds() // 60)} minutes"


# ------------------------------------------------------------------------ KPIs
def kpis(db: Database, tz: ZoneInfo, since_iso: str, until_iso: str) -> dict:
    rows = db.fetchall(
        "SELECT status, COUNT(*) AS n FROM simulations "
        "WHERE created_at >= ? AND created_at < ? GROUP BY status",
        (since_iso, until_iso),
    )
    counts = {r["status"]: int(r["n"]) for r in rows}
    total = sum(counts.values())
    completed = counts.get(Status.COMPLETED, 0)
    errors = counts.get(Status.ERROR, 0)
    cancelled = counts.get(Status.CANCELLED, 0)
    interrupted = counts.get(Status.INTERRUPTED, 0)
    queued = counts.get(Status.QUEUED, 0)
    processing = counts.get(Status.PROCESSING, 0)

    finished = completed + errors
    success_rate = round(completed / finished * 100, 1) if finished else 0.0

    today_start, today_end = day_bounds(tz)
    month_start, month_end = month_bounds(tz)

    avg_seconds = db.scalar(
        "SELECT AVG(processing_seconds) FROM simulations "
        "WHERE processing_seconds IS NOT NULL AND status=? AND created_at >= ? AND created_at < ?",
        (Status.COMPLETED, since_iso, until_iso),
        default=0.0,
    )

    return {
        "total": total,
        "completed": completed,
        "errors": errors,
        "cancelled": cancelled,
        "interrupted": interrupted,
        "queued": queued,
        "processing": processing,
        "pending": queued + processing,
        "today": int(db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE created_at >= ? AND created_at < ?",
            (today_start, today_end),
        )),
        "today_completed": int(db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE created_at >= ? AND created_at < ? AND status=?",
            (today_start, today_end, Status.COMPLETED),
        )),
        "month": int(db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE created_at >= ? AND created_at <= ?",
            (month_start, month_end),
        )),
        "avg_seconds": round(float(avg_seconds or 0), 1),
        "active_consultants": int(db.scalar(
            "SELECT COUNT(DISTINCT COALESCE(consultant_id, -1) || consultant_name) "
            "FROM simulations WHERE created_at >= ? AND created_at < ?",
            (since_iso, until_iso),
        )),
        "success_rate": success_rate,
        "with_refin": int(db.scalar(
            "SELECT COUNT(*) FROM simulations WHERE status=? AND refin='Sim' "
            "AND created_at >= ? AND created_at < ?",
            (Status.COMPLETED, since_iso, until_iso),
        )),
        "total_released": round(float(db.scalar(
            "SELECT SUM(reduction_value) FROM simulations WHERE status=? "
            "AND created_at >= ? AND created_at < ?",
            (Status.COMPLETED, since_iso, until_iso),
            default=0.0,
        ) or 0), 2),
    }


# ----------------------------------------------------------------------- series
def by_day(db: Database, tz: ZoneInfo, since_iso: str, until_iso: str) -> list[dict]:
    modifier = _tz_modifier(tz)
    rows = db.fetchall(
        f"""
        SELECT date(created_at, '{modifier}') AS dia,
               COUNT(*)                                            AS total,
               SUM(CASE WHEN status=? THEN 1 ELSE 0 END)           AS completed,
               SUM(CASE WHEN status=? THEN 1 ELSE 0 END)           AS errors
        FROM simulations
        WHERE created_at >= ? AND created_at < ?
        GROUP BY dia ORDER BY dia
        """,
        (Status.COMPLETED, Status.ERROR, since_iso, until_iso),
    )
    known = {r["dia"]: r for r in rows}
    if not known:
        return []

    # Preenche os dias sem movimento para o grafico nao mentir sobre a cadencia.
    first = min(known)
    last = max(known)
    out: list[dict] = []
    cursor = _date(first)
    end = _date(last)
    while cursor <= end:
        key = cursor.isoformat()
        row = known.get(key)
        out.append(
            {
                "dia": key,
                "total": int(row["total"]) if row else 0,
                "completed": int(row["completed"] or 0) if row else 0,
                "errors": int(row["errors"] or 0) if row else 0,
            }
        )
        cursor += timedelta(days=1)
    return out


def by_hour(db: Database, tz: ZoneInfo, since_iso: str, until_iso: str) -> list[dict]:
    modifier = _tz_modifier(tz)
    rows = db.fetchall(
        f"""
        SELECT CAST(strftime('%H', created_at, '{modifier}') AS INTEGER) AS hora,
               COUNT(*) AS total
        FROM simulations
        WHERE created_at >= ? AND created_at < ?
        GROUP BY hora ORDER BY hora
        """,
        (since_iso, until_iso),
    )
    found = {int(r["hora"]): int(r["total"]) for r in rows if r["hora"] is not None}
    if not found:
        return []
    return [{"hora": h, "total": found.get(h, 0)} for h in range(24)]


def by_consultant(db: Database, since_iso: str, until_iso: str, limit: int = 50) -> list[dict]:
    rows = db.fetchall(
        """
        SELECT COALESCE(NULLIF(consultant_name,''), 'Desconhecido') AS nome,
               consultant_id,
               COUNT(*)                                   AS total,
               SUM(CASE WHEN status=? THEN 1 ELSE 0 END)  AS completed,
               SUM(CASE WHEN status=? THEN 1 ELSE 0 END)  AS errors,
               AVG(CASE WHEN status=? THEN processing_seconds END) AS avg_seconds,
               MAX(created_at)                            AS last_activity,
               SUM(CASE WHEN refin='Sim' THEN 1 ELSE 0 END) AS with_refin
        FROM simulations
        WHERE created_at >= ? AND created_at < ?
        GROUP BY nome, consultant_id
        ORDER BY total DESC, nome COLLATE NOCASE
        LIMIT ?
        """,
        (Status.COMPLETED, Status.ERROR, Status.COMPLETED, since_iso, until_iso, limit),
    )
    out = []
    for row in rows:
        total = int(row["total"] or 0)
        completed = int(row["completed"] or 0)
        out.append(
            {
                "nome": row["nome"],
                "consultant_id": row["consultant_id"],
                "total": total,
                "completed": completed,
                "errors": int(row["errors"] or 0),
                "with_refin": int(row["with_refin"] or 0),
                "avg_seconds": round(float(row["avg_seconds"]), 1) if row["avg_seconds"] else 0.0,
                "success_rate": round(completed / total * 100, 1) if total else 0.0,
                "last_activity": row["last_activity"] or "",
            }
        )
    return out


def status_breakdown(db: Database, since_iso: str, until_iso: str) -> list[dict]:
    rows = db.fetchall(
        "SELECT status, COUNT(*) AS n FROM simulations "
        "WHERE created_at >= ? AND created_at < ? GROUP BY status ORDER BY n DESC",
        (since_iso, until_iso),
    )
    return [
        {"status": r["status"], "label": STATUS_LABELS.get(r["status"], r["status"]), "total": int(r["n"])}
        for r in rows
    ]


def duration_buckets(db: Database, since_iso: str, until_iso: str) -> list[dict]:
    rows = db.fetchall(
        "SELECT processing_seconds FROM simulations "
        "WHERE processing_seconds IS NOT NULL AND status=? AND created_at >= ? AND created_at < ?",
        (Status.COMPLETED, since_iso, until_iso),
    )
    if not rows:
        return []
    edges = [(0, 15, "até 15s"), (15, 30, "15-30s"), (30, 60, "30-60s"),
             (60, 120, "1-2min"), (120, 300, "2-5min"), (300, None, "5min+")]
    buckets = [{"label": label, "total": 0} for _, _, label in edges]
    for row in rows:
        value = float(row["processing_seconds"] or 0)
        for index, (low, high, _label) in enumerate(edges):
            if value >= low and (high is None or value < high):
                buckets[index]["total"] += 1
                break
    return buckets


def funnel(db: Database, tz: ZoneInfo) -> list[dict]:
    """Do que chegou no WhatsApp ao que virou resposta - contagens reais."""
    since, until = day_bounds(tz)
    received = int(db.scalar(
        "SELECT COUNT(*) FROM messages WHERE direction='in' AND created_at >= ? AND created_at < ?",
        (since, until),
    ))
    created = int(db.scalar(
        "SELECT COUNT(*) FROM simulations WHERE created_at >= ? AND created_at < ?",
        (since, until),
    ))
    completed = int(db.scalar(
        "SELECT COUNT(*) FROM simulations WHERE status=? AND created_at >= ? AND created_at < ?",
        (Status.COMPLETED, since, until),
    ))
    replied = int(db.scalar(
        "SELECT COUNT(*) FROM simulations WHERE replied_at IS NOT NULL "
        "AND created_at >= ? AND created_at < ?",
        (since, until),
    ))
    return [
        {"etapa": "Mensagens recebidas", "total": received},
        {"etapa": "Solicitações criadas", "total": created},
        {"etapa": "Simulações concluídas", "total": completed},
        {"etapa": "Respostas enviadas", "total": replied},
    ]


# -------------------------------------------------------------------- snapshot
def dashboard_snapshot(db: Database, tz: ZoneInfo) -> dict[str, Any]:
    since, until = range_bounds("30d", tz)
    today_start, today_end = day_bounds(tz)
    return {
        "kpis": kpis(db, tz, since, until),
        "by_day": by_day(db, tz, since, until),
        "by_hour": by_hour(db, tz, since, until),
        "by_consultant": by_consultant(db, today_start, today_end, limit=8),
        "status_breakdown": status_breakdown(db, since, until),
        "funnel": funnel(db, tz),
        "time": to_iso(utc_now()),
        "period": {"since": since, "until": until},
    }


def report(db: Database, tz: ZoneInfo, period: str, start: str | None, end: str | None) -> dict[str, Any]:
    since, until = range_bounds(period, tz, start, end)
    return {
        "period": {"name": period, "since": since, "until": until},
        "general": kpis(db, tz, since, until),
        "by_consultant": by_consultant(db, since, until),
        "by_day": by_day(db, tz, since, until),
        "by_hour": by_hour(db, tz, since, until),
        "status_breakdown": status_breakdown(db, since, until),
        "durations": duration_buckets(db, since, until),
    }


# --------------------------------------------------------------------- listas
SORTABLE = {
    "created_at": "created_at",
    "consultant": "consultant_name",
    "status": "status",
    "value": "reduction_value",
    "duration": "processing_seconds",
    "id": "id",
}


def list_simulations(
    db: Database,
    filters: dict,
    tz: ZoneInfo,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where: list[str] = []
    params: list[Any] = []

    def like(column: str, value: str) -> None:
        where.append(f"{column} LIKE ?")
        params.append(f"%{value.strip()}%")

    if filters.get("consultant"):
        like("consultant_name", filters["consultant"])
    if filters.get("cpf"):
        digits = "".join(ch for ch in str(filters["cpf"]) if ch.isdigit())
        if digits:
            like("cpf", digits)
    if filters.get("contract"):
        like("contract", filters["contract"])
    if filters.get("bank"):
        like("bank", filters["bank"])
    if filters.get("status"):
        statuses = [s for s in str(filters["status"]).split(",") if s]
        if statuses:
            where.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
    if filters.get("refin") in {"Sim", "Não"}:
        where.append("refin = ?")
        params.append(filters["refin"])
    if filters.get("request_id"):
        like("request_id", filters["request_id"])
    if filters.get("id"):
        try:
            where.append("id = ?")
            params.append(int(filters["id"]))
        except (TypeError, ValueError):
            pass
    if filters.get("q"):
        term = f"%{str(filters['q']).strip()}%"
        digits = "".join(ch for ch in str(filters["q"]) if ch.isdigit())
        where.append(
            "(consultant_name LIKE ? OR contract LIKE ? OR request_id LIKE ? OR bank LIKE ?"
            + (" OR cpf LIKE ?" if digits else "")
            + ")"
        )
        params.extend([term, term, term, term] + ([f"%{digits}%"] if digits else []))

    period = filters.get("period")
    if period and period != "all":
        since, until = range_bounds(period, tz, filters.get("start"), filters.get("end"))
        where.append("created_at >= ? AND created_at < ?")
        params.extend([since, until])

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    sort_column = SORTABLE.get(str(filters.get("sort", "created_at")), "created_at")
    direction = "ASC" if str(filters.get("dir", "desc")).lower() == "asc" else "DESC"

    total = int(db.scalar(f"SELECT COUNT(*) FROM simulations{clause}", params))
    rows = db.fetchall(
        f"SELECT * FROM simulations{clause} ORDER BY {sort_column} {direction}, id DESC "
        f"LIMIT ? OFFSET ?",
        (*params, max(1, min(limit, 500)), max(0, offset)),
    )
    return rows, total


def list_logs(
    db: Database,
    level: str = "",
    service: str = "",
    q: str = "",
    request_id: str = "",
    consultant: str = "",
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[dict], int]:
    where: list[str] = []
    params: list[Any] = []
    if level:
        levels = [x for x in level.split(",") if x]
        where.append(f"level IN ({','.join('?' for _ in levels)})")
        params.extend(levels)
    if service:
        where.append("service = ?")
        params.append(service)
    if q:
        where.append("message LIKE ?")
        params.append(f"%{q.strip()}%")
    if request_id:
        where.append("request_id = ?")
        params.append(request_id)
    if consultant:
        where.append("consultant LIKE ?")
        params.append(f"%{consultant.strip()}%")

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = int(db.scalar(f"SELECT COUNT(*) FROM logs{clause}", params))
    rows = db.fetchall(
        f"SELECT * FROM logs{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, max(1, min(limit, 1000)), max(0, offset)),
    )
    return rows, total


def log_services(db: Database) -> list[str]:
    return [
        r["service"]
        for r in db.fetchall("SELECT DISTINCT service FROM logs WHERE service<>'' ORDER BY service")
        if r["service"]
    ]


def timeline_for(db: Database, request_id: str) -> list[dict]:
    rows = db.fetchall(
        "SELECT id, type, stage, level, title, detail, created_at "
        "FROM events WHERE request_id=? ORDER BY id ASC",
        (request_id,),
    )
    for row in rows:
        row["stage_label"] = STAGE_LABELS.get(row["stage"], row["stage"] or row["title"])
    return rows


def serialize_simulation(row: dict, mask: bool) -> dict:
    out = dict(row)
    out["cpf_display"] = mask_cpf(row.get("cpf")) if mask else row.get("cpf") or ""
    if mask:
        out.pop("cpf", None)
        out.pop("raw_message", None)
    out["status_label"] = STATUS_LABELS.get(row.get("status"), row.get("status"))
    out["stage_label"] = STAGE_LABELS.get(row.get("stage"), row.get("stage"))
    for key in ("reduction_value", "installment_sum", "debt_sum", "processing_seconds"):
        if out.get(key) is not None:
            out[key] = round(float(out[key]), 2)
    return out


def _date(value: str):
    from datetime import date

    year, month, day = (int(part) for part in value.split("-"))
    return date(year, month, day)
