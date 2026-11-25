"""Informe en Excel: mismo contenido que el PDF, sello .p7s, registro por SHA-256 y verificacion."""
import hashlib
from datetime import datetime
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

from app import auth, ingest, limits, pades, reports
from app.db import session_scope
from app.models import AppAudit, ReportExport, Variable
from app.report_xlsx import RED_FILL, AMBER_FILL, SHEETS, limit_color
from app.wincc_csv import dt_to_epoch_ms

from .conftest import HEADER, log_line

PW = "Clave12345"
A = dt_to_epoch_ms(datetime(2026, 11, 20, 10))


@pytest.fixture()
def rep_env(env):
    rows_a, rows_b = [], []
    for i in range(120):
        ts = f"20/11/2026 {10 + i // 60:02d}:{i % 60:02d}:00"
        val = 40.0 if 30 <= i < 35 else 27.0 if 60 <= i < 62 else 21.0 + (i % 3) * 0.1
        rows_a.append(log_line("TT-101.Proc_Var", ts, val))
        if i < 100:
            rows_b.append(log_line("HT-101.Proc_Var", ts, 45.0 + (i % 5)))
    (env["proj"] / "Registros" / "File_TT0.csv").write_text(HEADER + "".join(rows_a), encoding="cp1252")
    (env["proj"] / "Registros" / "File_HT0.csv").write_text(HEADER + "".join(rows_b), encoding="cp1252")
    ingest.run_ingest()
    with session_scope() as s:
        ids = {v.var_name: v.id for v in s.query(Variable)}
        tt = s.get(Variable, ids["TT-101.Proc_Var"])
        tt.alias, tt.unit = "Sala 1", "°C"
    limits.set_limits(ids["TT-101.Proc_Var"], {"l": 15, "h": 25, "hh": 35}, "tester")
    for u, r in (("oper1", "operador"), ("sup1", "supervisor"), ("sup2", "supervisor"), ("lect1", "lector")):
        auth.create_user(u, PW, r, "desarrollador", "test", must_change=False, display_name=f"Nombre {u}")
    env["ids"] = [ids["TT-101.Proc_Var"], ids["HT-101.Proc_Var"]]
    return env


def _create(env, **kw):
    args = dict(project_id="p", variable_ids=env["ids"], start_ms=A, end_ms=A + 2 * 3600_000, step_s=60,
                title="Informe Excel", comment="Prueba", username="oper1", password=PW)
    args.update(kw)
    return reports.create_report(**args)


def _xlsx(rep_id, version=None, seal=False):
    path, _ = reports.xlsx_path(rep_id, version, seal=seal)
    return path.read_bytes()


def test_limit_color_rule():
    lim = {"ll": 5, "l": 15, "h": 25, "hh": 35}
    assert [limit_color(v, lim) for v in (40, 30, 20, 10, 1)] == ["red", "amber", None, "amber", "red"]
    assert limit_color(None, lim) is None and limit_color(50, None) is None and limit_color(50, {}) is None


def test_excel_content_matches_report(rep_env):
    rep = _create(rep_env)
    assert [e["fmt"] for e in rep["exports"]] == ["xlsx"] and rep["exports"][0]["sig_sha256"]
    wb = load_workbook(BytesIO(_xlsx(rep["id"])))
    assert tuple(wb.sheetnames) == SHEETS

    ident = {r[0]: r[1] for r in wb["Identificación"].iter_rows(min_row=3, max_row=14, values_only=True) if r[0]}
    assert ident["Informe"] == rep["id"] and ident["Hash SHA-256 de los datos"] == rep["data_hash"]
    assert ident["Periodo desde"] == datetime(2026, 11, 20, 10) and ident["Versión / estado"] == "v1 · ELABORADO"
    assert ident["Comentario"] == "Prueba"

    tab = wb["Valores tabulados"]
    head = [c.value for c in tab[1]][:3]
    assert head == ["Fecha / hora", "Sala 1 [°C]", "HT-101.Proc_Var"]
    rows = list(tab.iter_rows(min_row=2, max_col=3))
    assert len(rows) == 120                                        # 2 h a 1 min
    assert rows[0][0].value == datetime(2026, 11, 20, 10) and rows[0][1].value == 21.0
    assert rows[30][1].value == 40.0 and rows[30][1].fill.fgColor.rgb.endswith(RED_FILL)     # > HH
    assert rows[60][1].value == 27.0 and rows[60][1].fill.fgColor.rgb.endswith(AMBER_FILL)   # > H
    assert rows[0][1].fill.fill_type is None
    assert rows[110][2].value is None and rows[0][2].value == 45.0  # hueco tras dejar de registrar
    assert isinstance(rows[0][1].value, (int, float))             # numeros, no texto (Excel guarda 21.0 como 21)

    ev = [r for r in wb["Eventos"].iter_rows(min_row=2, values_only=True) if r[0]]
    assert ("Sala 1", "Alarma", "Límite alto") in [r[:3] for r in ev]
    assert len(wb["Gráficas"]._charts) == 2

    firmas = [r for r in wb["Firmas"].iter_rows(min_row=5, max_row=7, values_only=True)]
    assert firmas[0][:4] == ("Elaborado", "Nombre oper1", "oper1", "operador")
    assert firmas[1][:2] == ("Revisado", "pendiente") and firmas[2][:2] == ("Aprobado", "pendiente")
    valores = [c.value for row in wb["Firmas"].iter_rows() for c in row if c.value]
    assert rep["files"][0]["sha256"] in valores                    # enlaza con el PDF de la misma version

    assert all(ws.protection.sheet for ws in wb.worksheets) and wb.security.lockStructure
    assert wb.properties.keywords == rep["id"]


def test_p7s_seal_and_registered_hashes(rep_env):
    rep = _create(rep_env)
    xlsx, p7s = _xlsx(rep["id"]), _xlsx(rep["id"], seal=True)
    e = rep["exports"][0]
    assert hashlib.sha256(xlsx).hexdigest() == e["sha256"] and hashlib.sha256(p7s).hexdigest() == e["sig_sha256"]

    ok = pades.verify_detached(xlsx, p7s)
    assert ok["valid"] and ok["trusted"] and ok["signer"].startswith("Gestor de Registros WinCC") and ok["signed_at"]
    tampered = bytearray(xlsx)
    tampered[len(xlsx) // 2] ^= 0x01
    bad = pades.verify_detached(bytes(tampered), p7s)
    assert not bad["valid"] and not bad["digest_ok"] and bad["signature_ok"]
    assert not pades.verify_detached(xlsx, b"no es un p7s")["valid"]
    forged = bytearray(p7s)
    forged[-10] ^= 0x01                                            # firma alterada
    assert not pades.verify_detached(xlsx, bytes(forged))["valid"]

    v = reports.verify_sha256(e["sha256"])
    assert (v["found"], v["format"], v["version"], v["data_intact"]) == (True, "xlsx", 1, True)
    assert v["seal"]["valid"] and v["message"].startswith("Excel auténtico")
    v = reports.verify_sha256(e["sig_sha256"])
    assert v["format"] == "p7s" and v["message"].startswith("Sello .p7s")
    assert not reports.verify_sha256(hashlib.sha256(bytes(tampered)).hexdigest())["found"]


def test_each_signature_emits_new_excel_version(rep_env):
    rep = _create(rep_env)
    reports.sign_report(rep["id"], meaning="Revisado", username="sup1", password=PW, comment="ok")
    fin = reports.sign_report(rep["id"], meaning="Aprobado", username="sup2", password=PW, comment=None)
    assert [e["version"] for e in fin["exports"]] == [1, 2, 3]
    assert len({e["sha256"] for e in fin["exports"]}) == 3
    wb = load_workbook(BytesIO(_xlsx(rep["id"])))
    firmas = [r[:3] for r in wb["Firmas"].iter_rows(min_row=5, max_row=7, values_only=True)]
    assert firmas == [("Elaborado", "Nombre oper1", "oper1"), ("Revisado", "Nombre sup1", "sup1"),
                      ("Aprobado", "Nombre sup2", "sup2")]
    wb1 = load_workbook(BytesIO(_xlsx(rep["id"], version=1)))
    assert [r[1] for r in wb1["Firmas"].iter_rows(min_row=6, max_row=7, values_only=True)] == ["pendiente"] * 2
    v1 = reports.verify_sha256(fin["exports"][0]["sha256"])
    assert v1["version"] == 1 and not v1["is_latest"]
    with session_scope() as s:
        det = [a.details for a in s.scalars(select(AppAudit).where(AppAudit.action == "FIRMA_ELECTRONICA"))]
    assert all("xlsx_sha256" in d and "p7s_sha256" in d for d in det) and len(det) == 3


def test_old_report_without_excel_is_generated_on_demand(rep_env):
    rep = _create(rep_env)
    reports.sign_report(rep["id"], meaning="Revisado", username="sup1", password=PW, comment=None)
    with session_scope() as s:                                     # simula un informe anterior a esta funcion
        for e in s.scalars(select(ReportExport)):
            Path(e.path).unlink()
            Path(e.sig_path).unlink()
            s.delete(e)
    assert reports.get_report(rep["id"])["exports"] == []
    path, v = reports.xlsx_path(rep["id"], 1, username="sup1")
    assert v == 1 and path.exists()
    wb = load_workbook(path)
    assert [r[0] for r in wb["Firmas"].iter_rows(min_row=5, max_row=5, values_only=True)] == ["Elaborado"]
    assert [r[1] for r in wb["Firmas"].iter_rows(min_row=6, max_row=6, values_only=True)] == ["pendiente"]
    p7s, _ = reports.xlsx_path(rep["id"], 1, seal=True)
    assert pades.verify_detached(path.read_bytes(), p7s.read_bytes())["valid"]
    # un fichero borrado del disco tambien se regenera (mismos datos, nuevo registro)
    Path(path).unlink()
    path2, _ = reports.xlsx_path(rep["id"], 1)
    assert path2.exists()
    with session_scope() as s:
        acts = [a for (a,) in s.execute(select(AppAudit.action).where(AppAudit.action == "INFORME_EXCEL_GENERADO"))]
        assert len(acts) == 2 and len(s.scalars(select(ReportExport)).all()) == 1
    for version in (0, 3):
        with pytest.raises(reports.ReportError):
            reports.xlsx_path(rep["id"], version)
        with pytest.raises(reports.ReportError):
            reports.pdf_path(rep["id"], version)
    with pytest.raises(reports.ReportError):
        reports.xlsx_path("INF-NO")


def test_excel_disabled_or_without_seal(rep_env):
    rep_env["settings"].report.pades = False
    rep = _create(rep_env)
    assert rep["exports"][0]["sig_sha256"] is None
    with pytest.raises(reports.ReportError, match="Sello"):
        reports.xlsx_path(rep["id"], seal=True)
    assert reports.verify_sha256(rep["exports"][0]["sha256"])["seal"] is None
    rep_env["settings"].report.xlsx = False
    assert _create(rep_env)["exports"] == []


def test_excel_refuses_tampered_frozen_data(rep_env):
    import gzip
    import json
    rep = _create(rep_env)
    with session_scope() as s:
        for e in s.scalars(select(ReportExport)):
            s.delete(e)
        payload_path = s.get(reports.Report, rep["id"]).payload_path
    with gzip.open(payload_path, "rt", encoding="utf-8") as fh:
        p = json.load(fh)
    p["results"][0]["stats"]["max"] = 999
    with gzip.open(payload_path, "wt", encoding="utf-8") as fh:
        json.dump(p, fh)
    with pytest.raises(reports.ReportError) as err:
        reports.xlsx_path(rep["id"])
    assert err.value.code == 409


def test_empty_period_and_many_events(rep_env):
    rep = _create(rep_env, start_ms=A + 30 * 86_400_000, end_ms=A + 30 * 86_400_000 + 3600_000)
    wb = load_workbook(BytesIO(_xlsx(rep["id"])))
    assert wb["Valores tabulados"].max_row == 61                  # rejilla de 60 intervalos vacios
    assert wb["Eventos"]["A2"].value == "Sin eventos en el periodo."
    assert wb["Gráficas"]._charts == [] or len(wb["Gráficas"]._charts) == 2


def test_download_endpoints_and_audit(rep_env):
    from app.main import app
    rep = _create(rep_env, end_ms=A + 3600_000, step_s=90)        # muestreo con segundos
    with TestClient(app) as c:
        assert c.get(f"/api/reports/{rep['id']}/xlsx").status_code == 401
        c.post("/api/auth/login", json={"username": "lect1", "password": PW})
        r = c.get(f"/api/reports/{rep['id']}/xlsx")
        assert r.status_code == 200 and r.content[:2] == b"PK"
        assert r.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml")
        assert f"{rep['id']}_v1.xlsx" in r.headers["content-disposition"]
        p = c.get(f"/api/reports/{rep['id']}/p7s")
        assert p.status_code == 200 and p.headers["content-type"] == "application/pkcs7-signature"
        assert pades.verify_detached(r.content, p.content)["valid"]
        assert c.get(f"/api/reports/{rep['id']}/xlsx", params={"version": 9}).status_code == 404
        v = c.get("/api/reports/verify", params={"sha256": hashlib.sha256(r.content).hexdigest()}).json()
        assert v["format"] == "xlsx" and v["seal"]["valid"]
        wb = load_workbook(BytesIO(r.content))
        assert wb["Valores tabulados"]["A2"].number_format == "dd/mm/yyyy hh:mm:ss"
    with session_scope() as s:
        fmts = [a.details for a in s.scalars(select(AppAudit).where(AppAudit.action == "INFORME_DESCARGADO"))]
    assert any('"xlsx"' in d for d in fmts) and any('"p7s"' in d for d in fmts)
