"""CSV reales de planta como referencia ("golden files").

tests/datos/ contiene las primeras filas, con los bytes intactos (cp1252), de ficheros reales de los
proyectos Archivos y Ejemplo. Si una nueva version de WinCC cambia el formato, estas pruebas fallan.
Los valores esperados se copiaron a mano de las filas originales, no de la salida del programa.
"""
import shutil
from datetime import datetime

import pytest
from sqlalchemy import func, select

from app import auth, ingest, limits
from app.db import session_scope
from app.models import Alarm, AlarmTag, RuntimeEvent, Sample, User, Variable, WinccAudit
from app.wincc_csv import KIND_ALARM, KIND_AUDIT, KIND_LOG, dt_to_epoch_ms, epoch_ms_to_dt, parse_file

from .conftest import DATOS, make_env


def ms(texto):
    return dt_to_epoch_ms(datetime.strptime(texto, "%d/%m/%Y %H:%M:%S"))


def near(t_ms, texto):
    """Time_ms trae milisegundos; TimeString esta redondeado al segundo."""
    return abs(t_ms - ms(texto)) < 1000


def test_registro_tt_s6():
    r = parse_file(DATOS / "File_TT-S6_0.csv")
    assert r.kind == KIND_LOG and r.skipped == 0 and len(r.samples) == 60
    assert sorted({s.var_name for s in r.samples}) == ["TT-601.Proc_Var", "TT-602.Proc_Var", "TT-603.Proc_Var"]
    s0, s1 = r.samples[0], r.samples[1]
    # "TT-601.Proc_Var";"01/11/2026 1:10:16";-59,25926;1;46327048795,7407;U4RMLo
    assert (s0.var_name, s0.value, s0.validity, s0.checksum) == ("TT-601.Proc_Var", -59.25926, 1, "U4RMLo")
    assert near(s0.t_ms, "01/11/2026 1:10:16")
    # "TT-602.Proc_Var";"01/11/2026 1:10:16";19,72837;1;46327048795,7407;GAd1gv
    assert (s1.value, s1.checksum) == (19.72837, "GAd1gv")
    # cada variable registra cada 2 minutos
    tt601 = [s.t_ms for s in r.samples if s.var_name == "TT-601.Proc_Var"]
    assert all(abs((b - a) - 120_000) < 1000 for a, b in zip(tt601, tt601[1:]))


def test_analizador_potencia_con_rt_off():
    r = parse_file(DATOS / "File_Potencia0.csv")
    assert r.kind == KIND_LOG and r.skipped == 0
    # fichero real completo: 16 paradas del runtime y la fila final "$RT_COUNT$";17
    assert len(r.events) == 16 and r.samples == [] and r.rt_count == 17
    # "$RT_OFF$";"19/11/2026 12:06:47";0;2;46345504711,412;h63bMU
    e0, e15 = r.events[0], r.events[-1]
    assert (e0.kind, e0.checksum) == ("RT_OFF", "h63bMU") and near(e0.t_ms, "19/11/2026 12:06:47")
    # "$RT_OFF$";"23/11/2026 14:05:52";0;2;46349587409,7338;N/FEa2
    assert e15.checksum == "N/FEa2" and near(e15.t_ms, "23/11/2026 14:05:52")


def test_alarmas_hvac():
    r = parse_file(DATOS / "FileAlarms HVAC0.csv")
    assert r.kind == KIND_ALARM and r.skipped == 0 and len(r.alarms) == 40
    a = r.alarms[1]
    # 46346766425,4514;2;1;68;4034;;;;;;;;;"20/11/2026 18:23:39";"AD-004 Alarma HH";"HMI_Conexión_1";U+adKZ
    assert (a.msg_proc, a.state_after, a.msg_class, a.msg_number) == (2, 1, 68, 4034)
    assert (a.text, a.plc, a.checksum) == ("AD-004 Alarma HH", "HMI_Conexión_1", "U+adKZ")
    assert near(a.t_ms, "20/11/2026 18:23:39") and a.params == [""] * 8
    assert r.alarms[0].text == "Captador de polvo  Alarma Supervision Motores"   # dos espacios, tal cual


def test_audit_trail():
    r = parse_file(DATOS / "AuditTrail0.csv")
    assert r.kind == KIND_AUDIT and r.skipped == 0 and len(r.audit) == 69
    a0 = r.audit[0]
    assert (a0.record_id, a0.delta_utc, a0.user_id, a0.object_id) == (0, "-1:00", "Sistema", "Aplicación")
    assert a0.t_ms == ms("01/11/2026 1:10:01") and "WinCC Runtime Advanced V17.0" in a0.description
    login = next(x for x in r.audit if x.record_id == 14130)
    assert login.user_id == "Admin" and login.t_ms == ms("03/11/2026 0:34:40")
    assert login.description == "El usuario 'Admin' ha iniciado una sesión con el grupo 'Administrador'."


def test_golden_project_end_to_end(tmp_path):
    """El extracto real montado como proyecto: ingesta, tags de alarma, $RT_OFF$ y usuarios WinCC."""
    folders = {"registros": ["Registros"], "analizador": ["Analizador de red"], "alarmas": ["Alarmas"],
               "audit": ["AuditTrail"]}
    with make_env(tmp_path, folders) as env:
        p = env["proj"]
        shutil.copy(DATOS / "File_TT-S6_0.csv", p / "Registros")
        shutil.copy(DATOS / "File_Potencia0.csv", p / "Analizador de red")
        shutil.copy(DATOS / "FileAlarms HVAC0.csv", p / "Alarmas")
        shutil.copy(DATOS / "AuditTrail0.csv", p / "AuditTrail")
        r = ingest.run_ingest()
        assert r["status"] == "ok", r
        with session_scope() as s:
            assert s.scalar(select(func.count()).select_from(Sample)) == 60
            assert s.scalar(select(func.count()).select_from(Alarm)) == 40
            assert s.scalar(select(func.count()).select_from(WinccAudit)) == 69
            assert s.scalar(select(func.count()).select_from(RuntimeEvent)) == 16
            tags = {t.tag: t.levels for t in s.scalars(select(AlarmTag))}
            assert set(tags["AD-004"].split(",")) >= {"H", "HH"}
            cats = dict(s.execute(select(Variable.var_name, Variable.category)).all())
            assert cats["TT-601.Proc_Var"] == "registros"

        # usuarios WinCC detectados en el AuditTrail: se crean inactivos, rol segun su grupo
        res = auth.sync_wincc_users()
        assert "Admin" in res["created"]
        with session_scope() as s:
            u = s.scalar(select(User).where(User.username == "Admin"))
            assert (u.source, u.role, u.active, u.wincc_group) == ("wincc", "admin", False, "Administrador")
        assert auth.sync_wincc_users() == {"created": [], "updated": []}     # sin duplicar
        assert limits.list_alarm_tags("p")
