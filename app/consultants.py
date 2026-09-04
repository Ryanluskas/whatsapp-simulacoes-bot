"""Identificacao e cadastro de consultores.

A chave de identidade e' o JID do WhatsApp (``5567999999999@c.us``), extraido
do ``data-id`` da mensagem. A versao anterior gravava a string ``"false"`` como
telefone de todo mundo; como a coluna e' UNIQUE, todos os consultores viravam
uma linha so', renomeada a cada mensagem.

Nome exibido: por padrao acompanha o nome do WhatsApp, mas se o operador editar
o cadastro pelo painel o nome fica *fixado* (``pinned_name``) e deixa de ser
sobrescrito pelo push name.
"""

from __future__ import annotations

import threading

from .clock import now_iso
from .db import Database
from .models import Status


class ConsultantRepository:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.Lock()

    # ------------------------------------------------------------- identidade
    def resolve(self, wa_id: str, push_name: str = "") -> dict:
        """Encontra (ou cria) o consultor dono deste JID."""
        wa_id = (wa_id or "").strip()
        push_name = (push_name or "").strip()
        stamp = now_iso()

        if not wa_id or wa_id == "desconhecido":
            return {
                "id": None,
                "name": push_name or "Desconhecido",
                "wa_id": "",
                "phone": "",
                "active": 1,
                "known": False,
            }

        phone = "".join(ch for ch in wa_id.split("@", 1)[0] if ch.isdigit())

        with self._lock:
            row = self.db.fetchone("SELECT * FROM consultants WHERE wa_id=?", (wa_id,))
            if row is None and phone:
                # Cadastro feito a mao pelo painel, so' com o telefone.
                row = self.db.fetchone(
                    "SELECT * FROM consultants WHERE phone=? AND (wa_id IS NULL OR wa_id='')",
                    (phone,),
                )
                if row is not None:
                    self.db.update("consultants", {"wa_id": wa_id, "updated_at": stamp}, {"id": row["id"]})
                    row["wa_id"] = wa_id

            if row is None:
                new_id = self.db.insert(
                    "consultants",
                    {
                        "name": push_name or phone or "Consultor",
                        "wa_id": wa_id,
                        "phone": phone,
                        "active": 1,
                        "pinned_name": 0,
                        "notes": "",
                        "created_at": stamp,
                        "updated_at": stamp,
                        "last_seen_at": stamp,
                    },
                )
                row = self.db.fetchone("SELECT * FROM consultants WHERE id=?", (new_id,))
                return {**(row or {}), "known": False}

            changes = {"last_seen_at": stamp, "updated_at": stamp}
            if push_name and not row.get("pinned_name") and push_name != row.get("name"):
                changes["name"] = push_name
            if phone and not row.get("phone"):
                changes["phone"] = phone
            self.db.update("consultants", changes, {"id": row["id"]})
            return {**row, **changes, "known": True}

    def touch(self, consultant_id: int | None) -> None:
        if consultant_id:
            self.db.update(
                "consultants",
                {"last_seen_at": now_iso(), "updated_at": now_iso()},
                {"id": consultant_id},
            )

    # ------------------------------------------------------------------- CRUD
    def create(self, name: str, phone: str = "", wa_id: str = "", notes: str = "") -> int:
        stamp = now_iso()
        phone = "".join(ch for ch in (phone or "") if ch.isdigit())
        wa_id = (wa_id or "").strip() or (f"{phone}@c.us" if phone else "")
        return self.db.insert(
            "consultants",
            {
                "name": (name or "").strip() or "Consultor",
                "wa_id": wa_id or None,
                "phone": phone,
                "active": 1,
                "pinned_name": 1,
                "notes": (notes or "").strip(),
                "created_at": stamp,
                "updated_at": stamp,
                "last_seen_at": None,
            },
        )

    def update(self, consultant_id: int, **fields) -> None:
        allowed = {"name", "phone", "wa_id", "active", "notes"}
        data = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if "phone" in data:
            data["phone"] = "".join(ch for ch in str(data["phone"]) if ch.isdigit())
        if "wa_id" in data:
            data["wa_id"] = (str(data["wa_id"]).strip() or None)
        if "active" in data:
            data["active"] = int(bool(data["active"]))
        if "name" in data:
            data["name"] = str(data["name"]).strip()
            data["pinned_name"] = 1  # nome editado a mao nao e' mais sobrescrito
        if not data:
            return
        data["updated_at"] = now_iso()
        self.db.update("consultants", data, {"id": consultant_id})

    def set_active(self, consultant_id: int, active: bool) -> None:
        self.db.update(
            "consultants",
            {"active": int(bool(active)), "updated_at": now_iso()},
            {"id": consultant_id},
        )

    def exists_phone(self, phone: str, ignore_id: int | None = None) -> bool:
        phone = "".join(ch for ch in (phone or "") if ch.isdigit())
        if not phone:
            return False
        params: list = [phone]
        clause = "phone=?"
        if ignore_id:
            clause += " AND id<>?"
            params.append(ignore_id)
        return bool(self.db.scalar(f"SELECT COUNT(*) FROM consultants WHERE {clause}", params))

    # ----------------------------------------------------------------- consulta
    def list_with_stats(self, since_iso: str = "", until_iso: str = "") -> list[dict]:
        params: list = []
        window = ""
        if since_iso and until_iso:
            window = " AND s.created_at >= ? AND s.created_at < ?"
            params.extend([since_iso, until_iso])

        rows = self.db.fetchall(
            f"""
            SELECT c.*,
                   COUNT(s.id) AS total,
                   SUM(CASE WHEN s.status=? THEN 1 ELSE 0 END) AS completed,
                   SUM(CASE WHEN s.status=? THEN 1 ELSE 0 END) AS errors,
                   AVG(s.processing_seconds)                   AS avg_seconds,
                   MAX(s.created_at)                           AS last_request_at
            FROM consultants c
            LEFT JOIN simulations s
                   ON (s.consultant_id = c.id
                       OR (s.consultant_id IS NULL AND s.consultant_name = c.name)){window}
            GROUP BY c.id
            ORDER BY c.active DESC, total DESC, c.name COLLATE NOCASE
            """,
            (Status.COMPLETED, Status.ERROR, *params),
        )

        for row in rows:
            total = int(row.get("total") or 0)
            completed = int(row.get("completed") or 0)
            row["total"] = total
            row["completed"] = completed
            row["errors"] = int(row.get("errors") or 0)
            row["avg_seconds"] = round(float(row["avg_seconds"]), 1) if row.get("avg_seconds") else 0.0
            row["success_rate"] = round(completed / total * 100, 1) if total else 0.0
            row["last_activity"] = row.get("last_request_at") or row.get("last_seen_at") or ""
        return rows

    def get(self, consultant_id: int) -> dict | None:
        return self.db.fetchone("SELECT * FROM consultants WHERE id=?", (consultant_id,))
