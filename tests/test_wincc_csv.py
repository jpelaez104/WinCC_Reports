from datetime import datetime

from app.wincc_csv import KIND_ALARM, KIND_AUDIT, KIND_LOG, file_group, parse_file, time_ms_to_epoch_ms

from .conftest import HEADER, log_line


def test_time_ms_is_ole_date_times_1e6():
    ms = time_ms_to_epoch_ms("46345494034,5139")  # fila real: 19/11/2026 11:51:25
    dt = datetime(1970, 1, 1) + __import__("datetime").timedelta(milliseconds=ms)
    # Time_ms tiene precision de ms (11:51:24.538); TimeString lo muestra redondeado a :25
    assert abs((dt - datetime(2026, 11, 19, 11, 51, 25)).total_seconds()) < 1


def test_parse_log_with_rt_rows(tmp_path):
    f = tmp_path / "File_S1_Temp0.csv"
    f.write_text(HEADER + log_line("A", "19/11/2026 10:00:00", 21.5) + log_line("B", "19/11/2026 10:00:00", 3, 0)
                 + '"$RT_OFF$";"19/11/2026 10:05:00";0;2;46345420138,8889;xx\n'
                 + '"$RT_COUNT$";3;4aGU5x;;;ZZ\n', encoding="cp1252")
    r = parse_file(f)
    assert r.kind == KIND_LOG
    assert [(s.var_name, s.value, s.validity) for s in r.samples] == [("A", 21.5, 1), ("B", 3.0, 0)]
    assert len(r.events) == 1 and r.events[0].kind == "RT_OFF"
    assert r.rt_count == 3


def test_partial_last_line_is_skipped(tmp_path):
    f = tmp_path / "File_X0.csv"
    f.write_text(HEADER + log_line("A", "19/11/2026 10:00:00", 1) + '"A";"19/11/2026 10:01', encoding="cp1252")
    r = parse_file(f)
    assert len(r.samples) == 1 and r.skipped == 1


def test_detects_alarm_and_audit(tmp_path):
    a = tmp_path / "FileAlarms0.csv"
    a.write_text('"Time_ms";"MsgProc";"StateAfter";"MsgClass";"MsgNumber";"Var1";"Var2";"Var3";"Var4";"Var5";'
                 '"Var6";"Var7";"Var8";"TimeString";"MsgText";"PLC";"Checksum"\n'
                 '46346766425,4514;2;1;68;4034;;;;;;;;;"20/11/2026 18:23:39";"AD-004 Alarma HH";"HMI_Conexión_1";U+adKZ\n',
                 encoding="cp1252")
    r = parse_file(a)
    assert r.kind == KIND_ALARM and r.alarms[0].text == "AD-004 Alarma HH" and r.alarms[0].plc == "HMI_Conexión_1"
    t = tmp_path / "AuditTrail0.csv"
    t.write_text('"RecordID";"TimeStamp";"DeltaToUTC";"UserID";"ObjectID";"Description";"Comment";"Checksum"\n'
                 '5;"03/11/2026 0:34:40";"-1:00";"Admin";"Administración de usuarios";"Texto con\nsalto";;abc\n',
                 encoding="cp1252")
    r = parse_file(t)
    assert r.kind == KIND_AUDIT and r.audit[0].user_id == "Admin" and "salto" in r.audit[0].description


def test_file_group_names(tmp_path):
    from pathlib import Path
    assert file_group(Path("File_S1_Temperatura0.csv")) == "S1_Temperatura"
    assert file_group(Path("File_TT-S6_0.csv")) == "TT-S6"
    assert file_group(Path("FileAlarms HVAC0.csv.keep.1")) == "Alarms HVAC"
