"""Re-muestreo, estadistica y deteccion de anomalias por variable.

Los datos se guardan tal como los registra WinCC; el muestreo pedido (1 min, 5 min, ...)
se aplica al consultar, con agregados por intervalo (media, minimo, maximo, n).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import select

from .config import get_settings
from .db import session_scope
from .limits import limits_dict, parse_alarm_text
from .models import Alarm, RuntimeEvent, Sample, Variable, VariableLimits

MAX_EPISODES = 300
SAMPLING_PRESETS_S = [60, 300, 600, 900, 1800, 3600, 86400]


# -------------------------------------------------------------- carga de datos
def load_raw(variable_id: int, start_ms: int, end_ms: int) -> pd.DataFrame:
    with session_scope() as s:
        rows = s.execute(select(Sample.t_ms, Sample.value, Sample.validity).where(
            Sample.variable_id == variable_id, Sample.t_ms >= start_ms, Sample.t_ms < end_ms)
            .order_by(Sample.t_ms)).all()
    df = pd.DataFrame(rows, columns=["t_ms", "value", "validity"])
    df["t_ms"] = df["t_ms"].astype("int64")
    df["value"] = pd.to_numeric(df["value"], errors="coerce").astype("float64")
    return df


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Indices (inicio, fin) inclusivos de tramos contiguos donde mask es True."""
    if mask.size == 0 or not mask.any():
        return []
    m = np.concatenate([[False], mask, [False]]).astype(np.int8)
    d = np.diff(m)
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1) - 1
    return list(zip(starts.tolist(), ends.tolist()))


def _episode(kind: str, severity: str, t: np.ndarray, v: np.ndarray, i0: int, i1: int,
             peak: str | None = None, **extra) -> dict:
    seg = v[i0:i1 + 1]
    ep = {"kind": kind, "severity": severity, "start_ms": int(t[i0]), "end_ms": int(t[i1]),
          "duration_s": round((int(t[i1]) - int(t[i0])) / 1000), "samples": int(i1 - i0 + 1)}
    finite = seg[np.isfinite(seg)]
    if finite.size:
        if peak == "max":
            ep["peak"] = float(finite.max())
        elif peak == "min":
            ep["peak"] = float(finite.min())
    ep.update(extra)
    return ep


# ----------------------------------------------------------------- analisis
def resample(df: pd.DataFrame, start_ms: int, end_ms: int, step_s: int) -> list[list]:
    if df.empty:
        return []
    step_ms = step_s * 1000
    b = ((df["t_ms"].to_numpy() - start_ms) // step_ms) * step_ms + start_ms
    g = pd.DataFrame({"b": b, "v": df["value"].to_numpy()}).groupby("b")["v"]
    agg = pd.DataFrame({"mean": g.mean(), "min": g.min(), "max": g.max(), "n": g.count()})
    # intervalos vacios -> null para que la grafica muestre el hueco
    full = np.arange(start_ms, end_ms, step_ms, dtype="int64")
    if full.size <= 200_000:
        agg = agg.reindex(full)
    out = []
    for t, row in agg.iterrows():
        n = 0 if pd.isna(row["n"]) else int(row["n"])
        if n == 0:
            out.append([int(t), None, None, None, 0])
        else:
            out.append([int(t), round(float(row["mean"]), 4), round(float(row["min"]), 4),
                        round(float(row["max"]), 4), n])
    return out


def statistics_for(t: np.ndarray, v: np.ndarray, nominal_ms: float | None, start_ms: int, end_ms: int,
                   total_rows: int) -> dict:
    if v.size == 0:
        return {"n": 0, "total_rows": total_rows}
    imin, imax = int(np.argmin(v)), int(np.argmax(v))
    st = {
        "n": int(v.size), "total_rows": total_rows,
        "pct_valid": round(100 * v.size / total_rows, 2) if total_rows else None,
        "min": float(v[imin]), "min_t_ms": int(t[imin]),
        "max": float(v[imax]), "max_t_ms": int(t[imax]),
        "mean": float(v.mean()),                 # media aritmetica (promedio)
        "median": float(np.median(v)),
        "std": float(v.std(ddof=1)) if v.size > 1 else 0.0,
        "p05": float(np.percentile(v, 5)), "p95": float(np.percentile(v, 95)),
        "first_t_ms": int(t[0]), "last_t_ms": int(t[-1]),
    }
    if v.size > 1:  # promedio ponderado en el tiempo (robusto a muestreo irregular)
        dt = np.diff(t).astype("float64")
        if nominal_ms:
            dt = np.minimum(dt, 3 * nominal_ms)  # no ponderar huecos largos
        st["time_weighted_mean"] = float(np.sum(v[:-1] * dt) / dt.sum()) if dt.sum() > 0 else st["mean"]
    if nominal_ms:
        expected = (end_ms - start_ms) / nominal_ms
        st["coverage_pct"] = round(min(100.0, 100 * total_rows / expected), 2) if expected > 0 else None
    for k in ("min", "max", "mean", "median", "std", "p05", "p95", "time_weighted_mean"):
        if k in st:
            st[k] = round(st[k], 4)
    return st


def detect_anomalies(df: pd.DataFrame, limits: dict | None, nominal_ms: float | None,
                     rt_off_ms: list[int]) -> list[dict]:
    cfg = get_settings().analysis
    eps: list[dict] = []
    if df.empty:
        return eps
    t = df["t_ms"].to_numpy()
    v = df["value"].to_numpy()
    valid = (df["validity"].to_numpy() == 1) & np.isfinite(v)

    # 1. datos invalidos segun WinCC (Validity != 1)
    for i0, i1 in _runs(~valid):
        eps.append(_episode("dato_invalido", "info", t, v, i0, i1))

    lim = limits or {}
    # 2. fuera del rango del instrumento => fallo de senal
    rmin, rmax = lim.get("range_min"), lim.get("range_max")
    out_range = np.zeros(v.size, bool)
    if rmin is not None:
        out_range |= valid & (v < rmin)
    if rmax is not None:
        out_range |= valid & (v > rmax)
    for i0, i1 in _runs(out_range):
        eps.append(_episode("fallo_senal", "alarma", t, v, i0, i1, peak="max"))
    good = valid & ~out_range

    # 3. limites de alarma / pre-alarma
    def lvl_mask(key, op):
        val = lim.get(key)
        if val is None:
            return np.zeros(v.size, bool)
        return good & (v > val if op == ">" else v < val)
    hh, h, l, ll = lvl_mask("hh", ">"), lvl_mask("h", ">"), lvl_mask("l", "<"), lvl_mask("ll", "<")
    for mask, worst, label, peak, key in ((h | hh, hh, "alta", "max", "h"), (l | ll, ll, "baja", "min", "l")):
        for i0, i1 in _runs(mask):
            is_alarm = bool(worst[i0:i1 + 1].any())
            level = (key * 2).upper() if is_alarm else key.upper()
            limit_val = lim.get(key * 2) if is_alarm else lim.get(key)
            if limit_val is None:  # solo existe HH (o LL) configurado
                limit_val = lim.get(key * 2)
            eps.append(_episode("limite_" + label, "alarma" if is_alarm else "pre_alarma", t, v, i0, i1,
                                peak=peak, level=level, limit=limit_val))

    # 4. huecos en el registro
    if nominal_ms and t.size > 1:
        dt = np.diff(t)
        for i in np.flatnonzero(dt > cfg.gap_factor * nominal_ms)[:MAX_EPISODES]:
            a, b = int(t[i]), int(t[i + 1])
            rt = any(a <= r <= b for r in rt_off_ms)
            eps.append({"kind": "hueco", "severity": "info", "start_ms": a, "end_ms": b,
                        "duration_s": round((b - a) / 1000), "samples": 0,
                        "cause": "runtime WinCC detenido" if rt else "sin registros"})

    # 5. valor congelado (sensor pegado)
    if good.sum() > 2 and cfg.flatline_minutes > 0:
        tv, vv = t[good], v[good]
        same = np.concatenate([[False], np.isclose(np.diff(vv), 0.0, atol=1e-9)])
        for i0, i1 in _runs(same):
            i0 -= 1
            if tv[i1] - tv[i0] >= cfg.flatline_minutes * 60_000:
                eps.append(_episode("valor_congelado", "pre_alarma", tv, vv, i0, i1, peak="max"))

    # 6. valores atipicos estadisticos (z robusto con MAD)
    if good.sum() >= 20:
        vv = v[good]
        med = np.median(vv)
        mad = np.median(np.abs(vv - med))
        if mad > 0:
            z = 0.6745 * (v - med) / mad
            outl = good & (np.abs(z) > cfg.outlier_mad_z)
            for i0, i1 in _runs(outl):
                eps.append(_episode("atipico", "pre_alarma", t, v, i0, i1, peak="max",
                                    z=round(float(np.max(np.abs(z[i0:i1 + 1]))), 1)))

    eps.sort(key=lambda e: e["start_ms"])
    return eps


def summarize_episodes(eps: list[dict]) -> dict:
    out: dict = {"alarma": 0, "pre_alarma": 0, "info": 0, "by_kind": {}}
    for e in eps:
        out[e["severity"]] = out.get(e["severity"], 0) + 1
        k = out["by_kind"].setdefault(e["kind"], {"count": 0, "duration_s": 0})
        k["count"] += 1
        k["duration_s"] += e["duration_s"]
    return out


def wincc_alarms_for(project_id: str, tag: str | None, start_ms: int, end_ms: int) -> dict:
    if not tag:
        return {"tag": None, "activations": 0, "by_level": {}, "events": []}
    with session_scope() as s:
        rows = s.execute(select(Alarm.t_ms, Alarm.text, Alarm.state_after, Alarm.msg_number).where(
            Alarm.project_id == project_id, Alarm.t_ms >= start_ms, Alarm.t_ms < end_ms,
            Alarm.text.like(f"{tag}%")).order_by(Alarm.t_ms)).all()
    by_level: dict[str, int] = {}
    events = []
    for t, text, st, num in rows:
        p = parse_alarm_text(text)
        if not p or p[0].upper() != tag.upper():
            continue
        if st == 1:
            by_level[p[1]] = by_level.get(p[1], 0) + 1
        if len(events) < MAX_EPISODES:
            events.append({"t_ms": t, "level": p[1], "state_after": st, "text": text, "msg_number": num})
    return {"tag": tag, "activations": sum(by_level.values()), "by_level": by_level, "events": events}


def analyze_variable(variable_id: int, start_ms: int, end_ms: int, step_s: int, include_series: bool = True) -> dict:
    with session_scope() as s:
        var = s.get(Variable, variable_id)
        if var is None:
            raise KeyError(variable_id)
        lim = limits_dict(s.get(VariableLimits, variable_id))
        rt_off = [t for (t,) in s.execute(select(RuntimeEvent.t_ms).where(
            RuntimeEvent.project_id == var.project_id, RuntimeEvent.group_name == var.group_name,
            RuntimeEvent.t_ms >= start_ms, RuntimeEvent.t_ms < end_ms))]
        meta = {"id": var.id, "project_id": var.project_id, "var_name": var.var_name, "group": var.group_name,
                "alias": var.alias, "unit": var.unit, "tag": var.tag}
    df = load_raw(variable_id, start_ms, end_ms)
    t = df["t_ms"].to_numpy()
    nominal = float(np.median(np.diff(t))) if t.size > 2 else None

    eps = detect_anomalies(df, lim, nominal, rt_off)
    bad_idx = np.zeros(len(df), bool)
    if lim and not df.empty:
        vv = df["value"].to_numpy()
        if lim.get("range_min") is not None:
            bad_idx |= vv < lim["range_min"]
        if lim.get("range_max") is not None:
            bad_idx |= vv > lim["range_max"]
    good = df[(df["validity"] == 1) & df["value"].notna() & ~bad_idx]
    stats = statistics_for(good["t_ms"].to_numpy(), good["value"].to_numpy(), nominal, start_ms, end_ms, len(df))
    stats["nominal_period_s"] = round(nominal / 1000, 1) if nominal else None

    notes = []
    if nominal and step_s * 1000 < nominal * 0.95:
        notes.append(f"El muestreo pedido ({step_s} s) es menor que el periodo de registro de WinCC "
                     f"(~{round(nominal / 1000)} s): los intervalos sin dato aparecen vacios.")
    if not lim or all(lim.get(k) is None for k in ("ll", "l", "h", "hh")):
        notes.append("Sin limites de alarma configurados para esta variable.")

    res = {"variable": meta, "limits": lim, "stats": stats, "episodes": eps[:MAX_EPISODES],
           "episodes_total": len(eps), "summary": summarize_episodes(eps),
           "wincc_alarms": wincc_alarms_for(meta["project_id"], meta["tag"], start_ms, end_ms), "notes": notes}
    if include_series:
        res["series"] = resample(good, start_ms, end_ms, step_s)
    return res
