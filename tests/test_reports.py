import hashlib
from io import BytesIO

import numpy as np
from fastapi.testclient import TestClient

from app import auth, ingest, limits
from app.db import session_scope
from app.models import AppAudit, Variable

from .conftest import HEADER, log_line

BODY = {"project_id": "p", "start": "2026-11-20T10:00", "end": "2026-11-20T12:00", "step_s": 60,
        "title": "Informe de prueba", "password": "Clave12345"}


def _seed(env):
    rows_a, rows_b = [], []
    for i in range(120):  # 2 h a 1 min; TT-101 con pico a 40 entre los min 30 y 34
        ts = f"20/11/2026 {10 + i // 60:02d}:{i % 60:02d}:00"
        rows_a.append(log_line("TT-101.Proc_Var", ts, 40.0 if 30 <= i < 35 else 21.0 + (i % 3) * 0.1))
        if i < 100:  # HT-101 deja de registrar a las 11:40
            rows_b.append(log_line("HT-101.Proc_Var", ts, 45.0 + (i % 5)))
    (env["proj"] / "Registros" / "File_TT0.csv").write_text(HEADER + "".join(rows_a), encoding="cp1252")
    (env["proj"] / "Registros" / "File_HT0.csv").write_text(HEADER + "".join(rows_b), encoding="cp1252")
    ingest.run_ingest()
    with session_scope() as s:
        ids = {v.var_name: v.id for v in s.query(Variable)}
    limits.set_limits(ids["TT-101.Proc_Var"], {"l": 15, "h": 25, "hh": 35}, "tester")
    for u, r in (("oper1", "operador"), ("sup1", "supervisor"), ("sup2", "supervisor"), ("lect1", "lector")):
        auth.create_user(u, "Clave12345", r, "desarrollador", "test", must_change=False)
    return [ids["TT-101.Proc_Var"], ids["HT-101.Proc_Var"]]


def _login(c, user):
    assert c.post("/api/auth/login", json={"username": user, "password": "Clave12345"}).status_code == 200


def test_tabulated_values_share_time_grid(env):
    from app.analytics import analyze_variable
    from app.report_pdf import tabulated_values
    from app.wincc_csv import dt_to_epoch_ms
    from datetime import datetime
    ids = _seed(env)
    a = dt_to_epoch_ms(datetime(2026, 11, 20, 10))
    res = [analyze_variable(i, a, a + 2 * 3600_000, 60) for i in ids]
    grid, cols = tabulated_values(res, a, a + 2 * 3600_000, 60)
    assert len(grid) == 120 and all(len(c) == 120 for c in cols)
    assert cols[0][30] == 40.0 and cols[0][0] == 21.0       # mismo valor que la grafica
    assert np.isnan(cols[1][110]) and cols[1][0] == 45.0    # hueco tras dejar de registrar


def test_report_generation_signatures_pades_and_verify(env):
    ids = _seed(env)
    from asn1crypto import pem, x509
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext

    from app import pades
    from app.main import app
    from app.report_pdf import page_count
    with TestClient(app) as c:
        # nada se genera solo: sin peticion explicita no hay informes
        _login(c, "oper1")
        assert c.get("/api/reports").json() == []

        _login(c, "lect1")  # lector no puede generar
        assert c.post("/api/reports", json=dict(BODY, variable_ids=ids)).status_code == 403
        _login(c, "oper1")
        assert c.post("/api/reports", json=dict(BODY, variable_ids=ids, password="mala")).status_code == 401
        r = c.post("/api/reports", json=dict(BODY, variable_ids=ids))
        assert r.status_code == 200, r.text
        rep = r.json()
        assert rep["status"] == "elaborado" and rep["next_signature"] == "Revisado"
        assert rep["signatures"][0]["meaning"] == "Elaborado" and rep["signatures"][0]["username"] == "oper1"
        pdf = c.get(f"/api/reports/{rep['id']}/pdf").content
        assert pdf.startswith(b"%PDF")
        # resumen + 2 variables + valores tabulados (120 filas) + audit trail + firmas
        assert page_count(pdf) >= 6

        # sello PAdES intacto y valido con el certificado del gestor
        _, _, der = pem.unarmor(pades.certificate_pem())
        vc = ValidationContext(trust_roots=[x509.Certificate.load(der)])
        st = validate_pdf_signature(PdfFileReader(BytesIO(pdf)).embedded_signatures[0], vc)
        assert st.intact and st.valid

        # verificacion por SHA-256: el original se reconoce, uno modificado no
        sha1 = hashlib.sha256(pdf).hexdigest()
        assert c.get("/api/reports/verify", params={"sha256": sha1}).json()["found"]
        tampered = bytearray(pdf)
        tampered[len(pdf) // 2] ^= 0x01
        assert not c.get("/api/reports/verify",
                         params={"sha256": hashlib.sha256(bytes(tampered)).hexdigest()}).json()["found"]

        # operador no puede revisar
        assert c.post(f"/api/reports/{rep['id']}/sign",
                      json={"meaning": "Revisado", "password": "Clave12345"}).status_code == 403
        _login(c, "sup1")
        assert c.post(f"/api/reports/{rep['id']}/sign",
                      json={"meaning": "Aprobado", "password": "Clave12345"}).status_code == 409  # falta revision
        r = c.post(f"/api/reports/{rep['id']}/sign",
                   json={"meaning": "Revisado", "password": "Clave12345", "comment": "Conforme"})
        assert r.status_code == 200 and r.json()["version"] == 2
        assert c.post(f"/api/reports/{rep['id']}/sign",
                      json={"meaning": "Aprobado", "password": "Clave12345"}).status_code == 409  # mismo usuario
        _login(c, "sup2")
        final = c.post(f"/api/reports/{rep['id']}/sign", json={"meaning": "Aprobado", "password": "Clave12345"}).json()
        assert final["status"] == "aprobado" and final["version"] == 3 and len(final["signatures"]) == 3
        assert final["data_hash"] == rep["data_hash"]  # mismos datos congelados en todas las versiones
        assert final["next_signature"] is None

        v1 = c.get("/api/reports/verify", params={"sha256": sha1}).json()
        assert v1["found"] and v1["version"] == 1 and not v1["is_latest"] and v1["data_intact"]

    with session_scope() as s:
        acts = [a for (a,) in s.query(AppAudit.action)]
    assert acts.count("FIRMA_ELECTRONICA") == 3 and "INFORME_GENERADO" in acts and "FIRMA_FALLIDO" in acts


def test_report_row_limit(env):
    ids = _seed(env)
    from app.main import app
    with TestClient(app) as c:
        _login(c, "oper1")
        r = c.post("/api/reports", json=dict(BODY, variable_ids=ids, start="2026-11-01T00:00", step_s=1))
        assert r.status_code == 422 and "muestreo" in r.json()["detail"]
