"""Punto de entrada: API + interfaz web + tareas programadas de ingesta.

Desarrollo:   .venv\\Scripts\\python -m app.main
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import __version__, auth, ingest, limits
from .api import router
from .config import BASE_DIR, get_settings
from .db import init_db

log = logging.getLogger("gestor")
scheduler: BackgroundScheduler | None = None


def _safe_ingest(reason: str, do_purge: bool = False) -> None:
    try:
        res = ingest.run_ingest(do_purge=do_purge, reason=reason)
        log.info("Ingesta %s: %s", reason, res.get("status"))
        auth.sync_wincc_users()
    except Exception:  # noqa: BLE001
        log.exception("Fallo en la ingesta (%s); los lotes quedan en cola para reintento", reason)


def start_scheduler() -> BackgroundScheduler:
    cfg = get_settings().ingest
    sch = BackgroundScheduler(timezone=None, job_defaults={"coalesce": True, "max_instances": 1,
                                                           "misfire_grace_time": 3600})
    hh, mm = (int(x) for x in cfg.daily_time.split(":"))
    sch.add_job(_safe_ingest, "cron", hour=hh, minute=mm, args=["diaria", True], id="diaria")
    if cfg.interval_minutes > 0:
        sch.add_job(_safe_ingest, "interval", minutes=cfg.interval_minutes, args=["periodica"], id="periodica")
    sch.start()
    return sch


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global scheduler
    settings = get_settings()
    init_db(settings)
    for p in settings.projects:
        limits.restore_limits_from_backup(p.id)
    scheduler = start_scheduler()
    # al arrancar: vaciar la cola pendiente y leer lo nuevo, sin bloquear el inicio del servidor
    threading.Thread(target=_safe_ingest, args=["arranque"], daemon=True).start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Gestor de Registros WinCC", version=__version__, lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=Path(BASE_DIR / "static"), html=True), name="static")


def main() -> None:
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = get_settings().server
    uvicorn.run(app, host=s.host, port=s.port, log_level="info")


if __name__ == "__main__":
    main()
