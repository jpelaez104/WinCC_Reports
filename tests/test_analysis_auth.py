from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import audit, auth, ingest, limits
from app.analytics import analyze_variable
from app.db import session_scope
from app.models import AppAudit, Variable
from app.wincc_csv import dt_to_epoch_ms

from .conftest import HEADER, log_line


def _seed(env):
    rows = []
    for i in range(120):  # 2 h a 1 min; un pico a 40 entre 30 y 34 min
        ts = f"20/11/2026 {10 + i // 60:02d}:{i % 60:02d}:00"
        rows.append(log_line("TT-101.Proc_Var", ts, 40.0 if 30 <= i < 35 else 21.0 + (i % 3) * 0.1))
    (env["proj"] / "Registros" / "File_TT0.csv").write_text(HEADER + "".join(rows), encoding="cp1252")
    ingest.run_ingest()
    with session_scope() as s:
        return s.query(Variable).one().id


def test_limits_episodes_and_resample(env):
    vid = _seed(env)
    limits.set_limits(vid, {"l": 15, "h": 25, "hh": 35}, "tester")
    a = dt_to_epoch_ms(datetime(2026, 11, 20, 10))
    r = analyze_variable(vid, a, a + 2 * 3600_000, 300)
    assert r["stats"]["max"] == 40.0 and r["stats"]["n"] == 120
    eps = [e for e in r["episodes"] if e["kind"] == "limite_alta"]
    assert len(eps) == 1 and eps[0]["severity"] == "alarma" and eps[0]["level"] == "HH"
    assert len(r["series"]) == 24  # 2 h / 5 min


def test_limits_validation_and_backup(env):
    vid = _seed(env)
    with pytest.raises(ValueError):
        limits.set_limits(vid, {"h": 30, "hh": 20}, "tester")
    limits.set_limits(vid, {"h": 25}, "tester")
    assert (env["settings"].data_path / "limits_backup" / "p.json").exists()


def test_audit_chain_detects_tampering(env):
    audit.record("A", "u")
    audit.record("B", "u", details={"x": 1})
    assert audit.verify_chain()["ok"]
    with session_scope() as s:
        s.query(AppAudit).filter(AppAudit.action == "A").one().username = "otro"
    assert not audit.verify_chain()["ok"]


def test_login_lockout_and_api(env):
    _seed(env)
    auth.create_user("dev1", "Clave12345", "desarrollador", "desarrollador", "test", must_change=False)
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/projects").status_code == 401
        assert c.post("/api/auth/login", json={"username": "dev1", "password": "mala"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "dev1", "password": "Clave12345"}).status_code == 200
        vars_ = c.get("/api/projects/p/variables").json()
        r = c.post("/api/analysis", json={"variable_ids": [vars_[0]["id"]], "start": "2026-11-20T10:00",
                                          "end": "2026-11-20T12:00", "step_s": 60})
        assert r.status_code == 200 and len(r.json()["results"][0]["series"]) == 120
    for _ in range(5):
        with pytest.raises(auth.AuthError):
            auth.login("dev1", "mala")
    with pytest.raises(auth.AuthError, match="bloqueado"):
        auth.login("dev1", "Clave12345")
