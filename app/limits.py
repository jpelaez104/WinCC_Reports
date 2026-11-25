"""Limites de alarma por variable.

FileAlarms de WinCC dice QUE niveles existen por tag (p. ej. "TT-001 Alarma HH"), pero no
el VALOR numerico del limite. Por eso:
  1. refresh_alarm_tags(): detecta tags y niveles desde los textos de FileAlarms.
  2. Si la variable esta asociada a un tag, suggest_limits() estima el limite a partir del
     valor registrado en el instante en que se activo cada alarma (aproximado).
  3. El usuario confirma/configura los valores UNA vez -> se guardan en la BD y en un
     respaldo JSON (data/limits_backup). Persisten hasta que alguien los cambie.
"""
from __future__ import annotations

import json
import re
import statistics
from datetime import datetime

from sqlalchemy import func, select

from . import audit
from .config import get_settings
from .db import session_scope
from .models import Alarm, AlarmTag, Sample, Variable, VariableLimits
from .wincc_csv import dt_to_epoch_ms

ALARM_RE = re.compile(r"^(?P<tag>[A-Za-z0-9][\w\-\.]*)\s+Alarma\s+(?P<lvl>HH|H|LL|L|Fallo\s+se.al)\s*$", re.I)
LEVEL_KEYS = {"HH": "hh", "H": "h", "L": "l", "LL": "ll"}
LEVEL_ORDER = ["LL", "L", "H", "HH", "FALLO"]


def _norm_level(lvl: str) -> str:
    lvl = lvl.upper()
    return "FALLO" if lvl.startswith("FALLO") else lvl


def parse_alarm_text(text: str) -> tuple[str, str] | None:
    m = ALARM_RE.match(text or "")
    return (m.group("tag"), _norm_level(m.group("lvl"))) if m else None


# ------------------------------------------------------------ deteccion de tags
def refresh_alarm_tags() -> dict:
    """Recalcula la tabla alarm_tags y asocia automaticamente variables <-> tags."""
    out = {"tags": 0, "matched_variables": 0}
    with session_scope() as s:
        agg: dict[tuple[str, str], dict] = {}
        rows = s.execute(select(Alarm.project_id, Alarm.text, func.count(), func.min(Alarm.t_ms), func.max(Alarm.t_ms))
                         .where(Alarm.state_after == 1)
                         .group_by(Alarm.project_id, Alarm.text))
        for pid, text, n, t0, t1 in rows:
            parsed = parse_alarm_text(text)
            if not parsed:
                continue
            tag, lvl = parsed
            a = agg.setdefault((pid, tag), {"levels": set(), "n": 0, "t0": t0, "t1": t1})
            a["levels"].add(lvl)
            a["n"] += n
            a["t0"], a["t1"] = min(a["t0"], t0), max(a["t1"], t1)
        existing = {(t.project_id, t.tag): t for t in s.scalars(select(AlarmTag))}
        for (pid, tag), a in agg.items():
            levels = ",".join(l for l in LEVEL_ORDER if l in a["levels"])
            t = existing.get((pid, tag))
            if t is None:
                s.add(AlarmTag(project_id=pid, tag=tag, levels=levels, occurrences=a["n"],
                               first_t_ms=a["t0"], last_t_ms=a["t1"]))
            else:
                t.levels, t.occurrences, t.first_t_ms, t.last_t_ms = levels, a["n"], a["t0"], a["t1"]
        out["tags"] = len(agg)

        # asociacion automatica: "TT-601.Proc_Var" -> tag "TT-601"
        tags_by_project: dict[str, set[str]] = {}
        for pid, tag in agg:
            tags_by_project.setdefault(pid, set()).add(tag.upper())
        for v in s.scalars(select(Variable).where(Variable.tag.is_(None))):
            head = re.split(r"[.\s]", v.var_name, maxsplit=1)[0].upper()
            if head in tags_by_project.get(v.project_id, set()):
                v.tag = head
                out["matched_variables"] += 1
    return out


def list_alarm_tags(project_id: str) -> list[dict]:
    with session_scope() as s:
        mapped = {}
        for v in s.scalars(select(Variable).where(Variable.project_id == project_id, Variable.tag.is_not(None))):
            mapped.setdefault(v.tag.upper(), []).append(v.id)
        return [{"tag": t.tag, "levels": t.levels.split(",") if t.levels else [], "occurrences": t.occurrences,
                 "first_t_ms": t.first_t_ms, "last_t_ms": t.last_t_ms, "variable_ids": mapped.get(t.tag.upper(), [])}
                for t in s.scalars(select(AlarmTag).where(AlarmTag.project_id == project_id).order_by(AlarmTag.tag))]


# --------------------------------------------------------------- sugerencias
def suggest_limits(variable_id: int, window_ms: int = 5 * 60_000) -> dict:
    """Estima HH/H/L/LL con el valor registrado justo antes de cada activacion de la alarma."""
    with session_scope() as s:
        v = s.get(Variable, variable_id)
        if v is None or not v.tag:
            return {"variable_id": variable_id, "tag": None, "levels": {}}
        result: dict[str, dict] = {}
        alarms = s.execute(select(Alarm.text, Alarm.t_ms).where(
            Alarm.project_id == v.project_id, Alarm.state_after == 1, Alarm.text.like(f"{v.tag}%"))).all()
        by_level: dict[str, list[int]] = {}
        for text, t in alarms:
            p = parse_alarm_text(text)
            if p and p[0].upper() == v.tag.upper():
                by_level.setdefault(p[1], []).append(t)
        for lvl, times in by_level.items():
            values = []
            for t in times[-200:]:  # ultimas 200 activaciones
                val = s.scalar(select(Sample.value).where(
                    Sample.variable_id == v.id, Sample.validity == 1,
                    Sample.t_ms <= t + 1000, Sample.t_ms >= t - window_ms).order_by(Sample.t_ms.desc()).limit(1))
                if val is not None:
                    values.append(val)
            entry = {"activations": len(times), "matched": len(values)}
            if values and lvl in LEVEL_KEYS:
                # para alarmas altas el limite es <= valor en la activacion; para bajas, >=
                entry["value"] = round(min(values) if lvl in ("H", "HH") else max(values), 4)
                entry["median_at_activation"] = round(statistics.median(values), 4)
            result[lvl] = entry
        return {"variable_id": v.id, "tag": v.tag, "levels": result}


# ------------------------------------------------------------ lectura/escritura
LIMIT_FIELDS = ("ll", "l", "h", "hh", "range_min", "range_max")


def limits_dict(lim: VariableLimits | None) -> dict | None:
    if lim is None:
        return None
    return {**{f: getattr(lim, f) for f in LIMIT_FIELDS}, "source": lim.source, "note": lim.note,
            "updated_by": lim.updated_by, "updated_ms": lim.updated_ms}


def validate_limits(d: dict) -> None:
    seq = [(k, d.get(k)) for k in ("ll", "l", "h", "hh") if d.get(k) is not None]
    for (k1, a), (k2, b) in zip(seq, seq[1:]):
        if a > b:
            raise ValueError(f"Orden invalido: {k1.upper()}={a} es mayor que {k2.upper()}={b} (debe ser LL <= L <= H <= HH)")
    if d.get("range_min") is not None and d.get("range_max") is not None and d["range_min"] >= d["range_max"]:
        raise ValueError("El rango del instrumento debe cumplir minimo < maximo")


def set_limits(variable_id: int, data: dict, username: str, source: str = "manual") -> dict:
    clean = {f: (None if data.get(f) in ("", None) else float(data[f])) for f in LIMIT_FIELDS}
    validate_limits(clean)
    with session_scope() as s:
        v = s.get(Variable, variable_id)
        if v is None:
            raise KeyError(variable_id)
        lim = s.get(VariableLimits, variable_id)
        before = limits_dict(lim)
        if lim is None:
            lim = VariableLimits(variable_id=variable_id)
            s.add(lim)
        for f, val in clean.items():
            setattr(lim, f, val)
        lim.source = source
        lim.note = data.get("note")
        lim.updated_by = username
        lim.updated_ms = dt_to_epoch_ms(datetime.now())
        s.flush()
        after = limits_dict(lim)
        audit.record("LIMITES_MODIFICADOS", username, f"{v.project_id}:{v.var_name}",
                     {"antes": before, "despues": after}, session=s)
        project_id = v.project_id
    backup_limits(project_id)
    return after


def backup_limits(project_id: str) -> None:
    """Respaldo JSON de todos los limites del proyecto (permite restaurar si se cambia de BD)."""
    path = get_settings().data_path / "limits_backup" / f"{project_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with session_scope() as s:
        rows = s.execute(select(Variable, VariableLimits).join(VariableLimits, VariableLimits.variable_id == Variable.id)
                         .where(Variable.project_id == project_id)).all()
        data = {"project_id": project_id, "saved": datetime.now().isoformat(timespec="seconds"),
                "variables": {v.var_name: {**limits_dict(l), "alias": v.alias, "unit": v.unit, "tag": v.tag}
                              for v, l in rows}}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def restore_limits_from_backup(project_id: str) -> int:
    """Si la BD no tiene limites para el proyecto pero existe respaldo, los restaura."""
    path = get_settings().data_path / "limits_backup" / f"{project_id}.json"
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    n = 0
    with session_scope() as s:
        has = s.scalar(select(func.count()).select_from(VariableLimits).join(Variable)
                       .where(Variable.project_id == project_id))
        if has:
            return 0
        for v in s.scalars(select(Variable).where(Variable.project_id == project_id)):
            d = data["variables"].get(v.var_name)
            if not d:
                continue
            s.add(VariableLimits(variable_id=v.id, **{f: d.get(f) for f in LIMIT_FIELDS},
                                 source=d.get("source") or "respaldo", note=d.get("note"),
                                 updated_by=d.get("updated_by"), updated_ms=d.get("updated_ms")))
            v.alias = v.alias or d.get("alias")
            v.unit = v.unit or d.get("unit")
            v.tag = v.tag or d.get("tag")
            n += 1
        if n:
            audit.record("LIMITES_RESTAURADOS", "sistema", project_id, {"desde": str(path), "variables": n}, session=s)
    return n
