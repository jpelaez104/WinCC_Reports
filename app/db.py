"""Motor de base de datos y sesiones. El motor se elige solo con la URL de config.yaml."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings
from .models import Base, Project

_engine: Engine | None = None
_SessionLocal: sessionmaker | None = None


def _sqlite_pragmas(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")      # lecturas concurrentes, robusto ante cortes
    cur.execute("PRAGMA synchronous=FULL")      # prioridad: no perder datos
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.close()


def init_engine(settings: Settings | None = None) -> Engine:
    global _engine, _SessionLocal
    settings = settings or get_settings()
    url = settings.db_url
    kwargs: dict = {"future": True}
    if url.startswith("sqlite"):
        if ":memory:" not in url:
            Path(url.replace("sqlite:///", "")).parent.mkdir(parents=True, exist_ok=True)
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs["pool_pre_ping"] = True
    _engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(_engine, "connect", _sqlite_pragmas)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    return _engine or init_engine()


def new_session() -> Session:
    if _SessionLocal is None:
        init_engine()
    return _SessionLocal()


@contextmanager
def session_scope():
    s = new_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def init_db(settings: Settings | None = None) -> None:
    """Crea las tablas que falten y registra los proyectos de config.yaml."""
    settings = settings or get_settings()
    engine = get_engine()
    Base.metadata.create_all(engine)
    with session_scope() as s:
        existing = {p.id: p for p in s.scalars(select(Project))}
        for pc in settings.projects:
            p = existing.get(pc.id)
            if p is None:
                s.add(Project(id=pc.id, name=pc.name, root=str(pc.root_path)))
            else:
                p.name, p.root = pc.name, str(pc.root_path)
