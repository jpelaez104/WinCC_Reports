"""Operacion continua: retencion, ingestas simultaneas y BD recreada (limites + historico)."""
import gzip
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import db, ingest, limits
from app.db import session_scope
from app.models import AppAudit, Sample, Variable, VariableLimits

from .conftest import HEADER, log_line


def count(model=Sample):
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(model))


def lines(var, start: datetime, n, step=timedelta(minutes=1), value=20.0):
    return [log_line(var, (start + i * step).strftime("%d/%m/%Y %H:%M:%S"), value) for i in range(n)]


def write(path, rows):
    path.write_text(HEADER + "".join(rows), encoding="cp1252")


def var_id(name="T1"):
    with session_scope() as s:
        return s.scalar(select(Variable.id).where(Variable.var_name == name))


# ------------------------------------------------------------------ retencion
def test_purge_respects_retention_and_keeps_regulatory_data(env):
    st, paths = env["settings"], ingest.Paths(env["settings"])
    today = datetime.now().replace(second=0, microsecond=0)
    old, recent = today - timedelta(days=100), today - timedelta(days=10)
    write(env["proj"] / "Registros" / "File_T0.csv", lines("T1", old, 30) + lines("T1", recent, 30))
    ingest.run_ingest()                                   # retention_days = 0: entra todo
    assert count() == 60
    limits.set_limits(var_id(), {"h": 25, "hh": 30}, "tester")
    audit_before = count(AppAudit)

    # lotes 'done': uno de hace 10 dias (se borra) y el recien cargado (se conserva)
    done = sorted(paths.done.glob("*.json.gz"))
    assert done
    viejo = paths.done / "20000101-000000-viejo.json.gz"
    viejo.write_bytes(gzip.compress(json.dumps({"x": 1}).encode()))
    hace_10_dias = time.time() - 10 * 86400
    os.utime(viejo, (hace_10_dias, hace_10_dias))
    informe = st.data_path / "reports" / "INF-prueba" / "datos.json.gz"
    informe.parent.mkdir(parents=True)
    informe.write_bytes(b"congelado")

    st.retention_days = 92
    r = ingest.purge(st, paths)

    assert r["samples_deleted"] == 30 and count() == 30          # solo lo de hace 100 dias
    with session_scope() as s:
        t_min = s.scalar(select(func.min(Sample.t_ms)))
    assert t_min >= ingest.retention_cutoff_ms(st)
    assert r["batches_deleted"] == 1 and not viejo.exists() and all(f.exists() for f in done)
    assert count(VariableLimits) == 1                            # limites intactos
    assert count(AppAudit) == audit_before                       # audit trail intacto
    assert informe.read_bytes() == b"congelado"                  # informes intactos


def test_retention_does_not_reload_purged_data(env):
    """Tras purgar, releer un CSV con datos viejos no los vuelve a meter en la BD."""
    st = env["settings"]
    today = datetime.now().replace(second=0, microsecond=0)
    f = env["proj"] / "Registros" / "File_T0.csv"
    write(f, lines("T1", today - timedelta(days=100), 20))
    st.retention_days = 92
    ingest.run_ingest(do_purge=True)
    assert count() == 0
    write(f, lines("T1", today - timedelta(days=100), 20) + lines("T1", today - timedelta(hours=1), 10))
    ingest.run_ingest(do_purge=True)                             # WinCC reinicia el fichero
    assert count() == 10


# ------------------------------------------------------- ingestas simultaneas
def test_two_ingests_at_once_do_not_overlap(env, monkeypatch):
    write(env["proj"] / "Registros" / "File_T0.csv", lines("T1", datetime(2026, 11, 20, 10), 50))
    real_collect, inside, release = ingest.collect, threading.Event(), threading.Event()

    def slow_collect(*a, **k):
        inside.set()
        release.wait(10)
        return real_collect(*a, **k)
    monkeypatch.setattr(ingest, "collect", slow_collect)

    results = {}
    t = threading.Thread(target=lambda: results.setdefault("primera", ingest.run_ingest(reason="periodica")))
    t.start()
    assert inside.wait(10)
    results["segunda"] = ingest.run_ingest(reason="manual")      # llega mientras la primera esta dentro
    release.set()
    t.join(30)

    assert results["segunda"] == {"status": "busy"}
    assert results["primera"]["status"] == "ok"
    assert count() == 50


@pytest.mark.skipif(sys.platform != "win32", reason="bloqueo de fichero de Windows (msvcrt)")
def test_ingest_lock_between_processes(env):
    """Otro proceso (p. ej. scripts\\ingestar.py) con la ingesta en curso: no se pisan."""
    write(env["proj"] / "Registros" / "File_T0.csv", lines("T1", datetime(2026, 11, 20, 10), 20))
    paths = ingest.Paths(env["settings"])
    with ingest._process_lock(paths.lock):                       # simula el otro proceso
        with pytest.raises(RuntimeError, match="Otra ingesta"):
            ingest.run_ingest()
    assert count() == 0 and not list(paths.pending.glob("*.json.gz"))
    assert ingest.run_ingest()["status"] == "ok"                 # liberado: se lee todo
    assert count() == 20


# ------------------------------------------------------------- BD recreada
def test_db_down_before_collect_still_queues_data(env, monkeypatch):
    """Con la BD caida desde el principio, la recoleccion sigue guardando los lotes en disco."""
    f = env["proj"] / "Registros" / "File_T0.csv"
    write(f, lines("T1", datetime(2026, 11, 20, 10), 10))
    ingest.run_ingest()
    write(f, lines("T1", datetime(2026, 11, 20, 10), 25))     # WinCC sigue escribiendo

    def caida():
        raise RuntimeError("BD caida")
    monkeypatch.setattr(ingest, "session_scope", caida)
    r = ingest.run_ingest()
    assert r["reread_projects"] == [] and r["collect"]["batches"] == 1 and r["load"]["pending"] == 1
    monkeypatch.undo()
    ingest.run_ingest()
    assert count() == 25



def _recreate_db(env, name):
    """Simula borrar gestor.db o cambiar de motor SQL: BD vacia, data\\ intacta."""
    st = env["settings"]
    db._engine.dispose()
    st.database.url = "sqlite:///" + (env["tmp"] / name).as_posix()
    db._engine, db._SessionLocal = None, None
    db.init_engine(st)


def test_db_recreated_restores_limits_and_history(env):
    write(env["proj"] / "Registros" / "File_T0.csv", lines("T1", datetime(2026, 11, 20, 10), 40))
    ingest.run_ingest()
    vid = var_id()
    limits.set_limits(vid, {"ll": 5, "l": 10, "h": 25, "hh": 30, "range_min": 0, "range_max": 50,
                            "note": "validado"}, "tester")
    with session_scope() as s:
        v = s.get(Variable, vid)
        v.alias, v.unit, v.tag = "Sala 1", "°C", "TT-001"
    limits.backup_limits("p")
    with session_scope() as s:
        before = limits.limits_dict(s.get(VariableLimits, vid))

    _recreate_db(env, "nueva.db")
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app):                                        # arranque real: init_db + restaurar + ingesta
        deadline = time.time() + 20
        while time.time() < deadline and not count(VariableLimits):
            time.sleep(0.2)

    assert count() == 40                                         # el historico de los CSV vuelve a la BD
    with session_scope() as s:
        v = s.scalar(select(Variable).where(Variable.var_name == "T1"))
        after = limits.limits_dict(s.get(VariableLimits, v.id))
        assert (v.alias, v.unit, v.tag) == ("Sala 1", "°C", "TT-001")
        assert "LIMITES_RESTAURADOS" in [a for (a,) in s.execute(select(AppAudit.action))]
    for k in limits.LIMIT_FIELDS:
        assert after[k] == before[k], k
    assert after["note"] == "validado"
