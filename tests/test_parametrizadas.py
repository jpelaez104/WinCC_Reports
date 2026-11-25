"""Muchos casos con una sola funcion (pytest.mark.parametrize): formatos de WinCC, reglas y politicas."""
from datetime import datetime
from pathlib import Path

import pytest

from app import auth, limits
from app.wincc_csv import (KIND_ALARM, KIND_AUDIT, KIND_LOG, detect_kind, dt_to_epoch_ms, epoch_ms_to_dt,
                           file_group, parse_number, parse_timestring, time_ms_to_epoch_ms)


@pytest.mark.parametrize("texto, esperado", [
    ("21,5", 21.5), ("-59,25926", -59.25926), ("0", 0.0), ("1e3", 1000.0), (" 3,0 ", 3.0),
    ("19,72837", 19.72837), ("", None), ("   ", None), (None, None), ("abc", None), ("1.234,5", None),
])
def test_parse_number(texto, esperado):
    assert parse_number(texto) == esperado


@pytest.mark.parametrize("texto, esperado", [
    ("19/11/2026 11:51:25", datetime(2026, 11, 19, 11, 51, 25)),
    ("01/11/2026 1:10:16", datetime(2026, 11, 1, 1, 10, 16)),      # WinCC no rellena la hora con 0
    ("29/02/2028 23:59:59", datetime(2028, 2, 29, 23, 59, 59)),    # bisiesto
    ("31/12/2026 0:00:00", datetime(2026, 12, 31, 0, 0, 0)),
])
def test_parse_timestring(texto, esperado):
    assert epoch_ms_to_dt(parse_timestring(texto)) == esperado


@pytest.mark.parametrize("texto", ["", "2026-11-19 11:51:25", "32/11/2026 10:00:00", "19/11/2026", None])
def test_parse_timestring_invalid(texto):
    assert parse_timestring(texto) is None


@pytest.mark.parametrize("time_ms, timestring", [
    ("46327048795,7407", "01/11/2026 1:10:16"),       # filas reales de los CSV de planta
    ("46346766425,4514", "20/11/2026 18:23:39"),
    ("46345504711,412", "19/11/2026 12:06:47"),
])
def test_time_ms_matches_timestring(time_ms, timestring):
    assert abs(time_ms_to_epoch_ms(time_ms) - parse_timestring(timestring)) < 1000


@pytest.mark.parametrize("dt", [datetime(1970, 1, 1), datetime(2026, 11, 20, 10, 0, 0, 123000),
                                datetime(2030, 6, 15, 23, 59, 59)])
def test_epoch_roundtrip(dt):
    assert epoch_ms_to_dt(dt_to_epoch_ms(dt)) == dt


@pytest.mark.parametrize("cabecera, tipo", [
    (["VarName", "TimeString", "VarValue", "Validity", "Time_ms", "Checksum"], KIND_LOG),
    (["Time_ms", "MsgProc", "StateAfter", "MsgClass", "MsgNumber", "Var1", "TimeString", "MsgText", "PLC"], KIND_ALARM),
    (["RecordID", "TimeStamp", "DeltaToUTC", "UserID", "ObjectID", "Description", "Comment", "Checksum"], KIND_AUDIT),
    ([" varname ", "TIMESTRING"], KIND_LOG),
])
def test_detect_kind(cabecera, tipo):
    assert detect_kind(cabecera) == tipo


@pytest.mark.parametrize("cabecera", [[], ["Fecha", "Valor"], ["Time_ms", "Valor"]])
def test_detect_kind_rejects_unknown(cabecera):
    with pytest.raises(ValueError):
        detect_kind(cabecera)


@pytest.mark.parametrize("fichero, grupo", [
    ("File_S1_Temperatura0.csv", "S1_Temperatura"),
    ("File_S1_presion Salas0.csv", "S1_presion Salas"),
    ("File_AF_Presion_Agua Fria0.csv", "AF_Presion_Agua Fria"),
    ("File_TT-S6_0.csv", "TT-S6"),
    ("File_Potencia0.csv", "Potencia"),
    ("File_Potencia12.csv", "Potencia"),                 # segmento 12
    ("FileAlarms HVAC0.csv", "Alarms HVAC"),
    ("FileAlarms HVAC0.csv.keep", "Alarms HVAC"),
    ("FileAlarms HVAC0.csv.keep.2", "Alarms HVAC"),
    ("FileWarnings_HVAC_0.csv", "Warnings_HVAC"),
    ("FileSystemAlarms0.csv", "SystemAlarms"),
    ("AuditTrail0.csv", "AuditTrail"),
])
def test_file_group(fichero, grupo):
    assert file_group(Path(fichero)) == grupo


@pytest.mark.parametrize("lims", [
    {"ll": 1, "l": 2, "h": 3, "hh": 4}, {"h": 25}, {"l": 10, "hh": 30}, {"ll": 5, "l": 5, "h": 5, "hh": 5},
    {"range_min": 0, "range_max": 100}, {},
])
def test_limits_valid(lims):
    limits.validate_limits(lims)


@pytest.mark.parametrize("lims", [
    {"h": 30, "hh": 20}, {"ll": 10, "l": 5}, {"l": 30, "h": 20}, {"ll": 50, "hh": 40},
    {"range_min": 10, "range_max": 10}, {"range_min": 100, "range_max": 0},
])
def test_limits_invalid(lims):
    with pytest.raises(ValueError):
        limits.validate_limits(lims)


@pytest.mark.parametrize("texto, nivel", [
    ("AD-004 Alarma HH", ("AD-004", "HH")), ("TT-601 Alarma LL", ("TT-601", "LL")),
    ("TT-601 Alarma H", ("TT-601", "H")), ("PT-S6 Alarma L", ("PT-S6", "L")),
    ("TT-601 Alarma Fallo señal", ("TT-601", "FALLO")), ("tt-601 alarma hh", ("tt-601", "HH")),
    ("Captador de polvo  Alarma Supervision Motores", None), ("Texto libre", None),
])
def test_parse_alarm_text(texto, nivel):
    assert limits.parse_alarm_text(texto) == nivel


ROLES = ["lector", "operador", "supervisor", "admin", "desarrollador"]


@pytest.mark.parametrize("rol", ROLES)
@pytest.mark.parametrize("requerido", ["lector", "operador", "supervisor", "admin"])
def test_has_role(rol, requerido):
    assert auth.has_role({"role": rol}, requerido) == (auth.ROLE_LEVEL[rol] >= auth.ROLE_LEVEL[requerido])


def test_has_role_without_user():
    assert not auth.has_role(None, "lector")


@pytest.mark.parametrize("clave, valida", [
    ("Clave12345", True), ("abcdefg1", True), ("12345678", False), ("abcdefgh", False), ("Ab1", False),
    ("", False), ("Contraseña2026", True),
])
def test_password_policy(clave, valida):
    if valida:
        auth._check_policy(clave)
    else:
        with pytest.raises(auth.AuthError):
            auth._check_policy(clave)


@pytest.mark.parametrize("clave", ["Clave12345", "Contraseña con espacios 1", "x" * 200 + "1"])
def test_password_hash_roundtrip(clave):
    h = auth.hash_password(clave)
    assert h.startswith("pbkdf2_sha256$") and clave not in h
    assert auth.check_password(clave, h)
    assert not auth.check_password(clave + "x", h)
    assert auth.hash_password(clave) != h                        # sal distinta en cada hash


@pytest.mark.parametrize("almacenado", [None, "", "texto-plano", "md5$1$a$b", "pbkdf2_sha256$x$y$z"])
def test_check_password_rejects_bad_hashes(almacenado):
    assert not auth.check_password("Clave12345", almacenado)
