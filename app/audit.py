"""Audit trail propio de la aplicacion: solo-agregar y encadenado por SHA-256.

Cada registro guarda el hash del anterior; modificar o borrar una fila rompe la cadena
y verify_chain() lo detecta.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime

from sqlalchemy import select

from .db import session_scope
from .models import AppAudit
from .wincc_csv import dt_to_epoch_ms

GENESIS = "0" * 64
_lock = threading.Lock()


def _digest(prev: str, ts_ms: int, username: str | None, action: str, entity: str | None, details: str | None) -> str:
    payload = "\x1f".join([prev, str(ts_ms), username or "", action, entity or "", details or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record(action: str, username: str | None = None, entity: str | None = None, details: dict | None = None,
           session=None) -> AppAudit:
    """Agrega un evento. Si se pasa 'session' se usa esa transaccion (todo o nada con el cambio)."""
    det = json.dumps(details, ensure_ascii=False, sort_keys=True, default=str) if details else None
    with _lock:
        def _do(s):
            last = s.scalar(select(AppAudit).order_by(AppAudit.id.desc()).limit(1))
            prev = last.hash if last else GENESIS
            ts = dt_to_epoch_ms(datetime.now())
            row = AppAudit(ts_ms=ts, username=username, action=action, entity=entity, details=det,
                           prev_hash=prev, hash=_digest(prev, ts, username, action, entity, det))
            s.add(row)
            s.flush()
            return row
        if session is not None:
            return _do(session)
        with session_scope() as s:
            return _do(s)


def verify_chain() -> dict:
    with session_scope() as s:
        prev = GENESIS
        n = 0
        for row in s.scalars(select(AppAudit).order_by(AppAudit.id)):
            n += 1
            expected = _digest(prev, row.ts_ms, row.username, row.action, row.entity, row.details)
            if row.prev_hash != prev or row.hash != expected:
                return {"ok": False, "records": n, "broken_at_id": row.id}
            prev = row.hash
        return {"ok": True, "records": n}
