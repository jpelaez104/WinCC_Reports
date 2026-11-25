"""Gestion de usuarios y sesiones, casos de error de informes/firmas y lectura de CSV defectuosos."""
import gzip
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from app import auth, ingest, reports
from app.db import session_scope
from app.models import AppAudit, User, UserSession, Variable, WinccAudit
from app.wincc_csv import dt_to_epoch_ms, parse_file

from .conftest import HEADER, log_line

PW = "Clave12345"


def uid(name):
    with session_scope() as s:
        return s.scalar(select(User.id).where(User.username == name))


# -------------------------------------------------------------------- usuarios
def test_create_user_validations(env):
    auth.create_user("ana", PW, "lector", "desarrollador", "admin", must_change=False)
    for kw, msg in ((dict(role="jefe"), "Rol invalido"), (dict(source="ldap"), "Origen invalido"),
                    (dict(password="corta"), "al menos"), (dict(username="ana"), "ya existe")):
        args = dict(username="nuevo", password=PW, role="lector", source="desarrollador")
        args.update(kw)
        with pytest.raises(auth.AuthError, match=msg):
            auth.create_user(args["username"], args["password"], args["role"], args["source"], "admin")
    sin_clave = auth.create_user("wincc1", None, "operador", "wincc", "admin")
    assert sin_clave["active"] is False                          # sin contrasena no puede quedar activo


def test_update_user_all_fields(env):
    auth.create_user("ana", PW, "lector", "desarrollador", "admin", must_change=False)
    i = uid("ana")
    with pytest.raises(auth.AuthError, match="Rol invalido"):
        auth.update_user(i, "admin", role="jefe")
    with pytest.raises(auth.AuthError, match="no encontrado"):
        auth.update_user(999999, "admin", role="lector")
    token, _ = auth.login("ana", PW)
    u = auth.update_user(i, "admin", role="operador", display_name="Ana Pérez", new_password="NuevaClave1")
    assert (u["role"], u["display_name"]) == ("operador", "Ana Pérez")
    assert auth.user_from_token(token) is None                   # restablecer clave cierra sus sesiones
    with pytest.raises(auth.AuthError):
        auth.login("ana", PW)                                    # la clave vieja ya no vale
    auth.login("ana", "NuevaClave1")

    for _ in range(5):
        with pytest.raises(auth.AuthError):
            auth.login("ana", "mala")
    with pytest.raises(auth.AuthError, match="bloqueado"):
        auth.login("ana", "NuevaClave1")
    auth.update_user(i, "admin", unlock=True)
    auth.login("ana", "NuevaClave1")


def test_activate_wincc_user_requires_password(env):
    auth.create_user("op1", None, "operador", "wincc", "admin")
    with pytest.raises(auth.AuthError, match="contrasena inicial"):
        auth.update_user(uid("op1"), "admin", active=True)
    auth.update_user(uid("op1"), "admin", new_password=PW)
    auth.update_user(uid("op1"), "admin", active=True)
    with pytest.raises(auth.AuthError):
        auth.login("inexistente", PW)


def test_sessions_logout_expiry_and_password_change(env):
    auth.create_user("ana", PW, "lector", "desarrollador", "admin", must_change=False)
    auth.logout("token-que-no-existe")                           # no falla
    assert auth.user_from_token(None) is None
    token, _ = auth.login("ana", PW)
    with session_scope() as s:
        s.scalar(select(UserSession)).expires_ms = 0             # sesion caducada
    assert auth.user_from_token(token) is None

    with pytest.raises(auth.AuthError, match="distinta"):
        auth.change_password("ana", PW, PW)
    with pytest.raises(auth.AuthError):
        auth.change_password("ana", "MalaClave9", "OtraClave9")
    auth.change_password("ana", PW, "OtraClave9")
    with session_scope() as s:
        assert s.scalar(select(User).where(User.username == "ana")).must_change_password is False


def test_sync_wincc_users_updates_group(env):
    def login_row(rid, user, group):
        with session_scope() as s:
            s.add(WinccAudit(project_id="p", record_id=rid, t_ms=rid, user_id=user, object_id="x", checksum=str(rid),
                             description=f"El usuario '{user}' ha iniciado una sesión con el grupo '{group}'."))
    login_row(1, "Pedro", "Operador")
    assert auth.sync_wincc_users()["created"] == ["Pedro"]
    login_row(2, "Pedro", "Supervisor")
    assert auth.sync_wincc_users()["updated"] == ["Pedro"]
    with session_scope() as s:
        u = s.scalar(select(User).where(User.username == "Pedro"))
        assert (u.wincc_group, u.role) == ("Supervisor", "operador")    # el rol no se eleva solo


# -------------------------------------------------------------------- informes
@pytest.fixture()
def rep_env(env):
    env["settings"].report.pades = False                         # mas rapido; PAdES ya se prueba aparte
    rows = [log_line("TT-101.Proc_Var", f"20/11/2026 10:{i:02d}:00", 21.0) for i in range(30)]
    (env["proj"] / "Registros" / "File_TT0.csv").write_text(HEADER + "".join(rows), encoding="cp1252")
    ingest.run_ingest()
    for u, r in (("oper1", "operador"), ("oper2", "operador"), ("sup1", "supervisor"), ("sup2", "supervisor")):
        auth.create_user(u, PW, r, "desarrollador", "test", must_change=False)
    with session_scope() as s:
        env["vid"] = s.scalar(select(Variable.id))
    a = dt_to_epoch_ms(datetime(2026, 11, 20, 10))
    env["args"] = dict(project_id="p", variable_ids=[env["vid"]], start_ms=a, end_ms=a + 1800_000, step_s=60,
                       title=None, comment=None, username="oper1", password=PW)
    return env


def test_report_creation_errors(rep_env):
    args = rep_env["args"]
    for cambio, code in ((dict(end_ms=args["start_ms"]), 422), (dict(project_id="no"), 404),
                         (dict(variable_ids=[999999]), 422)):
        with pytest.raises(reports.ReportError) as e:
            reports.create_report(**dict(args, **cambio))
        assert e.value.code == code


def test_signature_errors_and_queries(rep_env):
    rep = reports.create_report(**rep_env["args"])
    rid = rep["id"]
    assert rep["files"][0]["pades"] is False
    assert rep["title"] == "Informe de registros — Prueba"     # titulo por defecto

    def sign_err(meaning, user, code, rid=rid):
        with pytest.raises(reports.ReportError) as e:
            reports.sign_report(rid, meaning=meaning, username=user, password=PW, comment=None)
        assert e.value.code == code

    sign_err("Elaborado", "sup1", 422)                           # significado no admitido
    sign_err("Revisado", "sup1", 404, rid="INF-NO")
    sign_err("Revisado", "oper2", 403)                           # operador no revisa
    reports.sign_report(rid, meaning="Revisado", username="sup1", password=PW, comment="ok")
    sign_err("Revisado", "sup2", 409)                            # ya revisado

    # datos congelados manipulados: no se puede firmar y la verificacion lo avisa
    actual = reports.get_report(rid)
    with session_scope() as s:
        payload_path = s.get(reports.Report, rid).payload_path
    with gzip.open(payload_path, "rt", encoding="utf-8") as fh:
        p = json.load(fh)
    p["results"][0]["stats"]["max"] = 999
    with gzip.open(payload_path, "wt", encoding="utf-8") as fh:
        json.dump(p, fh)
    sign_err("Aprobado", "sup2", 409)
    v = reports.verify_sha256(actual["files"][-1]["sha256"])
    assert v["found"] and not v["data_intact"]

    assert [r["id"] for r in reports.list_reports("p")] == [rid] and reports.list_reports("otro") == []
    with pytest.raises(reports.ReportError):
        reports.get_report("INF-NO")
    with pytest.raises(reports.ReportError):
        reports.pdf_path(rid, version=99)
    assert reports.pdf_path(rid)[1] == 2
    assert not reports.verify_sha256("0" * 64)["found"]
    with session_scope() as s:
        firmas = s.scalars(select(AppAudit.action).where(AppAudit.action == "FIRMA_ELECTRONICA")).all()
    assert len(firmas) == 2                                      # Elaborado + Revisado; los rechazos no firman


# ----------------------------------------------------------- CSV defectuosos
def test_csv_edge_cases(tmp_path):
    vacio = tmp_path / "vacio.csv"
    vacio.write_text("", encoding="cp1252")
    with pytest.raises(ValueError, match="vacio"):
        parse_file(vacio)

    f = tmp_path / "File_X0.csv"
    f.write_text(HEADER
                 + '"A";"19/11/2026 10:00:00";1,5;1;;c1\n'          # sin Time_ms: usa TimeString
                 + '"A";"sin fecha";1,5;1;;c2\n'                    # sin ningun tiempo: se descarta
                 + '"A";"19/11/2026 09:00:00";2;1;;c3\n'            # anterior a min_t: se ignora
                 + '"$RT_COUNT$";no-numero;x;;;y\n', encoding="cp1252")
    r = parse_file(f, min_t_ms=dt_to_epoch_ms(datetime(2026, 11, 19, 9, 30)))
    assert [(s.value, s.checksum) for s in r.samples] == [(1.5, "c1")] and r.skipped == 1 and r.rt_count is None

    al = tmp_path / "FileAlarms0.csv"
    al.write_text('"Time_ms";"MsgProc";"StateAfter";"MsgClass";"MsgNumber";"Var1";"Var2";"Var3";"Var4";"Var5";'
                  '"Var6";"Var7";"Var8";"TimeString";"MsgText";"PLC";"Checksum"\n'
                  '1;2;3\n'                                                        # corta
                  ';2;1;68;4034;;;;;;;;;"01/01/2020 10:00:00";"Vieja";"PLC";c\n'    # anterior a min_t
                  ';2;1;68;4035;;;;;;;;;"20/11/2026 18:23:39";"Nueva";"PLC";d\n', encoding="cp1252")
    r = parse_file(al, min_t_ms=dt_to_epoch_ms(datetime(2026, 1, 1)))
    assert [a.text for a in r.alarms] == ["Nueva"] and r.skipped == 1

    au = tmp_path / "AuditTrail0.csv"
    au.write_text('"RecordID";"TimeStamp";"DeltaToUTC";"UserID";"ObjectID";"Description";"Comment";"Checksum"\n'
                  '1;"x"\n'
                  '2;"01/01/2020 0:00:00";"-1:00";"A";"B";"Vieja";;c\n'
                  '3;"03/11/2026 0:34:40";"-1:00";"A";"B";"Nueva";;d\n', encoding="cp1252")
    r = parse_file(au, min_t_ms=dt_to_epoch_ms(datetime(2026, 1, 1)))
    assert [a.description for a in r.audit] == ["Nueva"] and r.skipped == 1


def test_bad_file_is_closed_even_if_error_is_kept(tmp_path):
    """Regresion: parse_file dejaba el CSV abierto al fallar y en Windows no se podia borrar la copia,
    lo que abortaba TODA la ingesta (falla solo si algo retiene la traza, como un handler de logging)."""
    bad = tmp_path / "File_Malo0.csv"
    bad.write_text('"Fecha";"Valor"\n"x";1\n', encoding="cp1252")
    with pytest.raises(ValueError) as info:
        parse_file(bad)
    retenida = info.value.__traceback__                     # la traza sigue viva, como en un log
    bad.unlink()                                             # PermissionError si el fichero siguiera abierto
    assert retenida is not None and not bad.exists()


def test_leftover_tmp_copies_are_cleaned(env):
    import os
    import time
    paths = ingest.Paths(env["settings"])
    viejo, nuevo = paths.tmp / "viejo.csv", paths.tmp / "nuevo.csv"
    viejo.write_text("x")
    nuevo.write_text("x")
    hace_2h = time.time() - 7200
    os.utime(viejo, (hace_2h, hace_2h))
    ingest.collect(env["settings"], paths)
    assert not viejo.exists() and nuevo.exists()             # la reciente podria estar en uso


def test_ingest_survives_bad_files_and_corrupt_batches(env):
    reg = env["proj"] / "Registros"
    (reg / "File_Malo0.csv").write_text('"Fecha";"Valor"\n"x";1\n', encoding="cp1252")     # cabecera desconocida
    (reg / "File_T0.csv").write_text(HEADER + log_line("T1", "20/11/2026 10:00:00", 1), encoding="cp1252")
    paths = ingest.Paths(env["settings"])
    (paths.pending / "00000000-roto.json.gz").write_bytes(b"esto no es gzip")
    r = ingest.run_ingest()
    assert r["status"] == "con_errores"
    assert any("File_Malo0.csv" in e for e in r["collect"]["errors"])
    assert any("corrupto" in e for e in r["load_before"]["errors"])
    assert (paths.pending.parent / "corrupt" / "00000000-roto.json.gz").exists()   # apartado, no borrado
    from sqlalchemy import func
    from app.models import Sample
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Sample)) == 1             # lo bueno entra igual
    st = ingest.last_status(env["settings"])
    assert st["status"] == "con_errores" and st["pending_batches"] == 0
