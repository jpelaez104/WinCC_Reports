"""Seguridad: matriz endpoint x rol y registro en el audit trail de todos los cambios."""
import json

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import audit, auth, ingest
from app.db import session_scope
from app.models import AppAudit, Variable

from .conftest import HEADER, log_line

PW = "Clave12345"
ROLES = ["lector", "operador", "supervisor", "admin", "desarrollador"]
LEVEL = auth.ROLE_LEVEL

# (metodo, ruta, rol minimo). None = cualquier usuario con sesion; "publico" = sin sesion.
# Cada endpoint nuevo de la API debe agregarse aqui (test_matrix_covers_every_endpoint lo exige).
MATRIX = [
    ("POST", "/api/auth/login", "publico"),
    ("POST", "/api/auth/logout", "publico"),
    ("GET", "/api/auth/me", None),
    ("POST", "/api/auth/change-password", None),
    ("GET", "/api/users", "admin"),
    ("POST", "/api/users", "admin"),
    ("PATCH", "/api/users/{user_id}", "admin"),
    ("POST", "/api/users/sync-wincc", "admin"),
    ("GET", "/api/meta", None),
    ("GET", "/api/projects", None),
    ("GET", "/api/projects/{project_id}/variables", None),
    ("PATCH", "/api/variables/{variable_id}", "supervisor"),
    ("GET", "/api/variables/{variable_id}/limits/suggest", None),
    ("PUT", "/api/variables/{variable_id}/limits", "supervisor"),
    ("GET", "/api/projects/{project_id}/alarm-tags", None),
    ("POST", "/api/analysis", None),
    ("POST", "/api/reports", "operador"),
    ("GET", "/api/reports", None),
    ("GET", "/api/reports/verify", None),
    ("GET", "/api/reports/certificate", None),
    ("GET", "/api/reports/{report_id}", None),
    ("GET", "/api/reports/{report_id}/pdf", None),
    ("GET", "/api/reports/{report_id}/xlsx", None),
    ("GET", "/api/reports/{report_id}/p7s", None),
    ("POST", "/api/reports/{report_id}/sign", "supervisor"),
    ("GET", "/api/projects/{project_id}/alarms", None),
    ("GET", "/api/projects/{project_id}/wincc-audit", None),
    ("GET", "/api/ingest/status", None),
    ("POST", "/api/ingest/run", "admin"),
    ("GET", "/api/audit/app", "supervisor"),
]
# valores de ruta que no existen: lo que se prueba es el control de acceso, no la operacion
FILL = {"user_id": "999999", "project_id": "p", "variable_id": "999999", "report_id": "INF-NO-EXISTE"}


def _url(path):
    for k, v in FILL.items():
        path = path.replace("{" + k + "}", v)
    return path


def _seed(env):
    rows = [log_line("TT-101.Proc_Var", f"20/11/2026 10:{i:02d}:00", 21.0) for i in range(30)]
    (env["proj"] / "Registros" / "File_TT0.csv").write_text(HEADER + "".join(rows), encoding="cp1252")
    ingest.run_ingest()
    for r in ROLES:
        auth.create_user(f"u_{r}", PW, r, "desarrollador", "test", must_change=False)


def _call(c, method, path):
    # cuerpo vacio: con permiso la API responde 422/404/400; sin permiso, 401/403 antes de validar nada
    return c.request(method, _url(path), json={} if method in ("POST", "PUT", "PATCH") else None)


def test_matrix_covers_every_endpoint():
    from app.api import router
    routes = {(m, r.path) for r in router.routes if isinstance(r, APIRoute) for m in r.methods}
    assert len(routes) > 20
    declared = {(m, p) for m, p, _ in MATRIX}
    assert routes - declared == set(), "endpoint sin rol declarado en la matriz de permisos"
    assert declared - routes == set(), "la matriz declara endpoints que ya no existen"


def test_without_session_everything_is_401(env):
    _seed(env)
    from app.main import app
    with TestClient(app) as c:
        for method, path, minimo in MATRIX:
            if minimo == "publico":
                continue
            assert _call(c, method, path).status_code == 401, (method, path)


@pytest.mark.parametrize("role", ROLES)
def test_role_matrix(env, role):
    _seed(env)
    from app.main import app
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": f"u_{role}", "password": PW}).status_code == 200
        for method, path, minimo in MATRIX:
            if minimo == "publico" or path == "/api/auth/logout":
                continue
            code = _call(c, method, path).status_code
            if minimo is None or LEVEL[role] >= LEVEL[minimo]:
                assert code not in (401, 403), f"{role} deberia poder: {method} {path} -> {code}"
            else:
                assert code == 403, f"{role} NO deberia poder: {method} {path} -> {code}"


def test_inactive_or_locked_user_loses_session(env):
    _seed(env)
    from app.main import app
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "u_lector", "password": PW})
        assert c.get("/api/auth/me").status_code == 200
        with session_scope() as s:
            uid = s.scalar(select(auth.User.id).where(auth.User.username == "u_lector"))
        auth.update_user(uid, "test", active=False)
        assert c.get("/api/auth/me").status_code == 401


def _actions():
    with session_scope() as s:
        return [(a.action, a.username, a.entity, json.loads(a.details) if a.details else None)
                for a in s.scalars(select(AppAudit).order_by(AppAudit.id))]


def test_every_change_is_audited_with_before_and_after(env):
    _seed(env)
    from app.main import app
    with session_scope() as s:
        vid = s.scalar(select(Variable.id))
    with TestClient(app) as c:
        assert c.post("/api/auth/login", json={"username": "u_admin", "password": "mala"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "u_admin", "password": PW}).status_code == 200
        r = c.post("/api/users", json={"username": "nuevo", "password": PW, "role": "lector"})
        assert r.status_code == 200
        assert c.patch(f"/api/users/{r.json()['id']}", json={"role": "operador"}).status_code == 200
        assert c.patch(f"/api/variables/{vid}", json={"alias": "Sala 1", "unit": "°C"}).status_code == 200
        assert c.put(f"/api/variables/{vid}/limits", json={"h": 25, "hh": 30}).status_code == 200
        assert c.put(f"/api/variables/{vid}/limits", json={"h": 26, "hh": 30}).status_code == 200
        assert c.post("/api/ingest/run").status_code == 200
        assert c.post("/api/auth/change-password",
                      json={"old_password": PW, "new_password": "OtraClave99"}).status_code == 200
        assert c.post("/api/auth/logout").status_code == 200

    acts = _actions()
    names = [a[0] for a in acts]
    for esperado in ("LOGIN_FALLIDO", "LOGIN", "USUARIO_CREADO", "USUARIO_MODIFICADO", "VARIABLE_MODIFICADA",
                     "LIMITES_MODIFICADOS", "INGESTA_MANUAL", "CONTRASENA_CAMBIADA", "LOGOUT"):
        assert esperado in names, f"falta en el audit trail: {esperado}"

    var = next(a for a in acts if a[0] == "VARIABLE_MODIFICADA")
    assert var[1] == "u_admin" and var[3]["antes"]["alias"] is None and var[3]["despues"]["alias"] == "Sala 1"
    rol = next(a for a in acts if a[0] == "USUARIO_MODIFICADO")
    assert rol[3]["rol"] == ["lector", "operador"]
    lim = [a for a in acts if a[0] == "LIMITES_MODIFICADOS"][-1]
    assert lim[3]["antes"]["h"] == 25 and lim[3]["despues"]["h"] == 26     # antes/despues del segundo cambio
    assert audit.verify_chain()["ok"]


def test_rejected_change_is_not_applied_nor_audited_as_done(env):
    _seed(env)
    from app.main import app
    with session_scope() as s:
        vid = s.scalar(select(Variable.id))
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"username": "u_supervisor", "password": PW})
        assert c.put(f"/api/variables/{vid}/limits", json={"h": 40, "hh": 30}).status_code == 422
    assert "LIMITES_MODIFICADOS" not in [a[0] for a in _actions()]
