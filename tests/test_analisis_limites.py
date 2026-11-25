"""Analisis: cada tipo de anomalia por separado, notas, alarmas WinCC del tag y sugerencia de limites."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app import ingest, limits
from app.analytics import analyze_variable, statistics_for
from app.db import session_scope
from app.models import Alarm, RuntimeEvent, Variable
from app.wincc_csv import dt_to_epoch_ms

from .conftest import HEADER, log_line

T0 = datetime(2026, 11, 20, 10, 0)
A = dt_to_epoch_ms(T0)


def seed(env, values, validity=None, var="TT-101.Proc_Var", step_min=1, skip=()):
    """Una fila por minuto con los valores dados; 'skip' = minutos sin registro (huecos)."""
    validity = validity or [1] * len(values)
    rows = [log_line(var, (T0 + timedelta(minutes=i * step_min)).strftime("%d/%m/%Y %H:%M:%S"), v, q)
            for i, (v, q) in enumerate(zip(values, validity)) if i not in skip]
    (env["proj"] / "Registros" / "File_TT0.csv").write_text(HEADER + "".join(rows), encoding="cp1252")
    ingest.run_ingest()
    with session_scope() as s:
        return s.scalar(select(Variable.id).where(Variable.var_name == var))


def analyze(vid, minutes=180, step=60):
    return analyze_variable(vid, A, A + minutes * 60_000, step)


def kinds(r):
    return [e["kind"] for e in r["episodes"]]


def noisy(n, base=21.0):
    return [base + ((i * 7) % 5) * 0.1 for i in range(n)]


def test_invalid_data_episode(env):
    vals = noisy(60)
    q = [1] * 60
    q[10:13] = [0, 0, 0]
    r = analyze(seed(env, vals, q))
    ep = next(e for e in r["episodes"] if e["kind"] == "dato_invalido")
    assert ep["samples"] == 3 and ep["severity"] == "info"
    assert r["stats"]["n"] == 57 and r["stats"]["total_rows"] == 60 and r["stats"]["pct_valid"] == 95.0


def test_out_of_instrument_range_is_signal_failure(env):
    vals = noisy(60)
    vals[20:23] = [999.0] * 3
    vid = seed(env, vals)
    limits.set_limits(vid, {"range_min": 0, "range_max": 100, "h": 50}, "t")
    r = analyze(vid)
    ep = next(e for e in r["episodes"] if e["kind"] == "fallo_senal")
    assert ep["severity"] == "alarma" and ep["peak"] == 999.0
    assert r["stats"]["max"] < 100                      # el fallo de senal no contamina la estadistica
    assert "limite_alta" not in kinds(r)                # ni se cuenta como alarma de proceso


def test_low_limits_pre_alarm_and_alarm(env):
    vals = noisy(60)
    vals[10:13] = [14.0] * 3      # bajo L=15 -> pre-alarma
    vals[30:33] = [4.0] * 3       # bajo LL=5 -> alarma
    vid = seed(env, vals)
    limits.set_limits(vid, {"ll": 5, "l": 15}, "t")
    eps = [e for e in analyze(vid)["episodes"] if e["kind"] == "limite_baja"]
    assert [(e["severity"], e["level"], e["peak"], e["limit"]) for e in eps] == [
        ("pre_alarma", "L", 14.0, 15), ("alarma", "LL", 4.0, 5)]


def test_only_hh_configured_uses_hh_as_limit(env):
    vals = noisy(60)
    vals[5] = 50.0
    vid = seed(env, vals)
    limits.set_limits(vid, {"hh": 40}, "t")
    ep = next(e for e in analyze(vid)["episodes"] if e["kind"] == "limite_alta")
    assert (ep["level"], ep["limit"], ep["severity"]) == ("HH", 40, "alarma")


def test_gap_with_and_without_runtime_off(env):
    vid = seed(env, noisy(120), skip=set(range(20, 40)) | set(range(80, 100)))
    with session_scope() as s:
        v = s.get(Variable, vid)
        s.add(RuntimeEvent(project_id="p", group_name=v.group_name, t_ms=A + 25 * 60_000, kind="RT_OFF"))
    huecos = [e for e in analyze(vid)["episodes"] if e["kind"] == "hueco"]
    assert [h["cause"] for h in huecos] == ["runtime WinCC detenido", "sin registros"]
    assert huecos[0]["duration_s"] == 21 * 60


def test_frozen_value(env):
    vals = noisy(150)
    vals[20:100] = [22.5] * 80       # 80 min identico > 60 min
    eps = [e for e in analyze(seed(env, vals))["episodes"] if e["kind"] == "valor_congelado"]
    assert len(eps) == 1 and eps[0]["duration_s"] >= 60 * 60


def test_statistical_outlier(env):
    vals = noisy(100)
    vals[50] = 60.0
    ep = next(e for e in analyze(seed(env, vals))["episodes"] if e["kind"] == "atipico")
    assert ep["peak"] == 60.0 and ep["z"] > 6


def test_notes_and_empty_period(env):
    vid = seed(env, noisy(30), step_min=2)                # WinCC registra cada 2 min
    r = analyze(vid, step=60)
    assert any("menor que el periodo de registro" in n for n in r["notes"])
    assert any("Sin limites" in n for n in r["notes"])
    vacio = analyze_variable(vid, A + 10 * 86_400_000, A + 11 * 86_400_000, 60)
    assert vacio["stats"] == {"n": 0, "total_rows": 0, "nominal_period_s": None} and vacio["series"] == []
    with pytest.raises(KeyError):
        analyze_variable(999999, A, A + 1, 60)


def test_statistics_single_value():
    import numpy as np
    st = statistics_for(np.array([1000]), np.array([5.0]), None, 0, 2000, 1)
    assert st["std"] == 0.0 and "time_weighted_mean" not in st and "coverage_pct" not in st


def _alarm(t_min, text, state=1, num=1):
    return Alarm(project_id="p", group_name="Alarms HVAC", t_ms=A + t_min * 60_000, msg_number=num,
                 msg_class=68, state_after=state, msg_proc=2, text=text, plc="HMI")


def test_suggest_limits_from_wincc_alarm_activations(env):
    vals = noisy(120)
    vals[30:35] = [36.0, 37.0, 38.0, 37.0, 36.0]      # HH se activa a 36
    vals[70:73] = [26.0, 27.0, 26.0]                  # H se activa a 26
    vals[90:93] = [9.0, 8.0, 9.0]                     # L se activa a 9
    vid = seed(env, vals)
    with session_scope() as s:
        s.add_all([_alarm(30, "TT-101 Alarma HH", num=1), _alarm(35, "TT-101 Alarma HH", 0, 1),
                   _alarm(70, "TT-101 Alarma H", num=2), _alarm(90, "TT-101 Alarma L", num=3),
                   _alarm(91, "TT-101 Alarma Fallo señal", num=4), _alarm(95, "TT-1010 Alarma HH", num=5)])
    res = limits.refresh_alarm_tags()
    assert res["matched_variables"] == 1               # TT-101.Proc_Var -> tag TT-101 (no TT-1010)
    tags = {t["tag"]: t for t in limits.list_alarm_tags("p")}
    assert tags["TT-101"]["levels"] == ["L", "H", "HH", "FALLO"] and tags["TT-101"]["variable_ids"] == [vid]

    sug = limits.suggest_limits(vid)
    lv = sug["levels"]
    assert sug["tag"] == "TT-101"
    assert (lv["HH"]["value"], lv["HH"]["activations"]) == (36.0, 1)
    assert lv["H"]["value"] == 26.0 and lv["L"]["value"] == 9.0
    assert "value" not in lv["FALLO"]                  # el fallo de senal no tiene limite numerico

    r = analyze(vid)
    assert r["wincc_alarms"]["activations"] == 4 and r["wincc_alarms"]["by_level"]["HH"] == 1
    assert len(r["wincc_alarms"]["events"]) == 5       # incluye la salida (state 0) de HH


def test_suggest_without_tag_and_unknown_variable(env):
    vid = seed(env, noisy(10))
    assert limits.suggest_limits(vid)["levels"] == {}
    assert limits.suggest_limits(999999)["tag"] is None


def test_restore_skips_variables_missing_from_backup(env):
    vid = seed(env, noisy(10))
    limits.set_limits(vid, {"h": 25}, "t")
    assert limits.restore_limits_from_backup("p") == 0          # ya hay limites: no se toca nada
    assert limits.restore_limits_from_backup("otro") == 0       # sin respaldo
    with pytest.raises(KeyError):
        limits.set_limits(999999, {"h": 1}, "t")
