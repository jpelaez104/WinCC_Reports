"""Ingesta de los CSV de WinCC hacia la base de datos, sin perdida de datos.

Dos etapas desacopladas por una COLA PERSISTENTE en disco (data/queue):

  1. RECOLECCION  (collect)
     - copia cada CSV (WinCC lo mantiene abierto) a data/tmp y lo lee
     - toma solo las filas nuevas segun la marca de lectura de ese fichero
     - escribe un LOTE comprimido en data/queue/pending (escritura atomica + fsync)
     - SOLO DESPUES avanza la marca de lectura (data/state/read_state.json, escritura atomica)

  2. CARGA  (load_pending)
     - cada lote se inserta en la BD dentro de UNA transaccion
     - insercion idempotente: las filas que ya existen se ignoran (reintentar nunca duplica)
     - tras el commit el lote pasa a data/queue/done (se conserva N dias para auditoria)
     - si la BD falla, el lote queda en pending y se reintenta en la siguiente ejecucion

Si el proceso se corta en cualquier punto, en el peor caso se repite un lote (idempotente);
nunca se pierde: la marca de lectura no avanza hasta que el lote esta en disco.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, insert, select

from .config import CATEGORIES, ProjectCfg, Settings, get_settings
from .db import session_scope
from .models import Alarm, RuntimeEvent, Sample, SourceFile, Variable, WinccAudit
from .wincc_csv import KIND_ALARM, KIND_AUDIT, KIND_LOG, dt_to_epoch_ms, file_group, parse_file

log = logging.getLogger("gestor.ingest")
_run_lock = threading.Lock()
FILE_PATTERNS = ("*.csv", "*.csv.keep", "*.csv.keep.*")


# --------------------------------------------------------------- utilidades IO
def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


@contextmanager
def _process_lock(lock_path: Path):
    """Evita que dos procesos (servicio + script manual) ingesten a la vez."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+")
    try:
        try:
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            locked = "msvcrt"
        except ImportError:  # no-Windows (pruebas)
            locked = None
        except OSError:
            raise RuntimeError("Otra ingesta esta en curso (data/ingest.lock)")
        yield
    finally:
        try:
            if locked == "msvcrt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:
            pass
        fh.close()


class Paths:
    def __init__(self, settings: Settings):
        d = settings.data_path
        self.state = d / "state" / "read_state.json"
        self.status = d / "state" / "ingest_status.json"
        self.pending = d / "queue" / "pending"
        self.done = d / "queue" / "done"
        self.tmp = d / "tmp"
        self.lock = d / "ingest.lock"
        for p in (self.pending, self.done, self.tmp, self.state.parent):
            p.mkdir(parents=True, exist_ok=True)


# -------------------------------------------------------- etapa 1: recoleccion
def discover_files(project: ProjectCfg) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    root = project.root_path
    for cat in CATEGORIES:
        for folder in project.folders.get(cat, []):
            base = root / folder
            if not base.exists():
                log.warning("Carpeta no encontrada: %s", base)
                continue
            seen: set[Path] = set()
            for pat in FILE_PATTERNS:
                for f in base.rglob(pat):
                    if f.is_file() and f not in seen and not f.name.endswith(".tmp"):
                        seen.add(f)
                        out.append((cat, f))
    return sorted(out, key=lambda x: str(x[1]))


def _clean_tmp(paths: Paths, older_than_s: float = 3600) -> None:
    """Borra copias temporales que no se pudieron borrar en una ingesta anterior."""
    limit = time.time() - older_than_s
    for f in paths.tmp.glob("*.csv"):
        try:
            if f.stat().st_mtime < limit:
                f.unlink()
        except OSError:
            pass


def collect(settings: Settings | None = None, paths: Paths | None = None) -> dict:
    """Etapa 1. Devuelve un resumen. Nunca toca la base de datos."""
    settings = settings or get_settings()
    paths = paths or Paths(settings)
    state: dict = _read_json(paths.state, {})
    summary = {"files": 0, "changed": 0, "batches": 0, "rows": 0, "errors": []}
    _clean_tmp(paths)

    for project in settings.projects:
        for cat, f in discover_files(project):
            summary["files"] += 1
            rel = f.relative_to(project.root_path).as_posix()
            key = f"{project.id}|{rel}"
            st = state.get(key, {})
            try:
                stat = f.stat()
            except OSError as e:
                summary["errors"].append(f"{rel}: {e}")
                continue
            if st.get("size") == stat.st_size and st.get("mtime") == stat.st_mtime:
                continue  # sin cambios
            summary["changed"] += 1
            copy = paths.tmp / f"{uuid.uuid4().hex}.csv"
            try:
                shutil.copyfile(f, copy)
                min_t = st.get("read_t_ms")
                res = parse_file(copy, min_t_ms=min_t)
                if min_t is not None and res.fingerprint != st.get("fingerprint"):
                    # WinCC reinicio/reescribio el fichero: se relee completo (carga idempotente)
                    log.info("Fichero reiniciado, lectura completa: %s", rel)
                    res = parse_file(copy)
            except Exception as e:  # noqa: BLE001
                summary["errors"].append(f"{rel}: {e}")
                log.exception("Error leyendo %s", rel)
                continue
            finally:
                try:
                    copy.unlink(missing_ok=True)
                except OSError:  # p. ej. antivirus revisando la copia: se borra en la proxima limpieza
                    log.warning("No se pudo borrar la copia temporal %s", copy)

            n = len(res.samples) + len(res.events) + len(res.alarms) + len(res.audit)
            if n:
                batch = {
                    "version": 1,
                    "project_id": project.id,
                    "category": cat,
                    "rel_path": rel,
                    "group": file_group(f),
                    "kind": res.kind,
                    "fingerprint": res.fingerprint,
                    "rt_count": res.rt_count,
                    "created_ms": dt_to_epoch_ms(datetime.now()),
                    "samples": [asdict(x) for x in res.samples],
                    "events": [asdict(x) for x in res.events],
                    "alarms": [asdict(x) for x in res.alarms],
                    "audit": [asdict(x) for x in res.audit],
                }
                name = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.json.gz"
                _atomic_write_bytes(paths.pending / name,
                                    gzip.compress(json.dumps(batch, ensure_ascii=False).encode("utf-8")))
                summary["batches"] += 1
                summary["rows"] += n

            # el lote ya esta en disco: ahora si se avanza la marca de lectura
            new_t = res.max_t_ms if res.max_t_ms is not None else st.get("read_t_ms")
            state[key] = {"fingerprint": res.fingerprint, "read_t_ms": new_t,
                          "size": stat.st_size, "mtime": stat.st_mtime, "rt_count": res.rt_count}
            _atomic_write_bytes(paths.state, json.dumps(state, indent=1).encode("utf-8"))
    return summary


# ------------------------------------------------------------- etapa 2: carga
def _load_batch(session, batch: dict, cutoff_ms: int | None) -> int:
    pid, group = batch["project_id"], batch["group"]
    sf = session.scalar(select(SourceFile).where(SourceFile.project_id == pid,
                                                 SourceFile.rel_path == batch["rel_path"]))
    if sf is None:
        sf = SourceFile(project_id=pid, category=batch["category"], rel_path=batch["rel_path"],
                        group_name=group, kind=batch["kind"])
        session.add(sf)
    inserted = 0
    max_t = None

    # ---- muestras de variables
    if batch["samples"]:
        by_var: dict[str, list[dict]] = {}
        for s in batch["samples"]:
            if cutoff_ms is None or s["t_ms"] >= cutoff_ms:
                by_var.setdefault(s["var_name"], []).append(s)
        vars_db = {v.var_name: v for v in session.scalars(
            select(Variable).where(Variable.project_id == pid, Variable.var_name.in_(list(by_var))))}
        for name, rows in by_var.items():
            v = vars_db.get(name)
            if v is None:
                v = Variable(project_id=pid, var_name=name, group_name=group, category=batch["category"])
                session.add(v)
                session.flush()
            lo = min(r["t_ms"] for r in rows)
            hi = max(r["t_ms"] for r in rows)
            have = {t for (t,) in session.execute(select(Sample.t_ms).where(
                Sample.variable_id == v.id, Sample.t_ms >= lo, Sample.t_ms <= hi))}
            new, seen = [], set()
            for r in rows:
                t = r["t_ms"]
                if t in have or t in seen:
                    continue
                seen.add(t)
                new.append({"variable_id": v.id, "t_ms": t, "value": r["value"],
                            "validity": r["validity"], "checksum": r["checksum"]})
            if new:
                for i in range(0, len(new), 5000):
                    session.execute(insert(Sample), new[i:i + 5000])
                inserted += len(new)
            v.first_t_ms = lo if v.first_t_ms is None else min(v.first_t_ms, lo)
            v.last_t_ms = hi if v.last_t_ms is None else max(v.last_t_ms, hi)
            max_t = hi if max_t is None else max(max_t, hi)

    # ---- eventos de runtime ($RT_OFF$)
    if batch["events"]:
        lo = min(e["t_ms"] for e in batch["events"])
        hi = max(e["t_ms"] for e in batch["events"])
        have = set(session.execute(select(RuntimeEvent.t_ms, RuntimeEvent.kind).where(
            RuntimeEvent.project_id == pid, RuntimeEvent.group_name == group,
            RuntimeEvent.t_ms >= lo, RuntimeEvent.t_ms <= hi)).all())
        new = []
        for e in batch["events"]:
            k = (e["t_ms"], e["kind"])
            if k not in have:
                have.add(k)
                new.append({"project_id": pid, "group_name": group, "t_ms": e["t_ms"], "kind": e["kind"]})
        if new:
            session.execute(insert(RuntimeEvent), new)
            inserted += len(new)
        max_t = hi if max_t is None else max(max_t, hi)

    # ---- alarmas / avisos
    if batch["alarms"]:
        lo = min(a["t_ms"] for a in batch["alarms"])
        hi = max(a["t_ms"] for a in batch["alarms"])
        have = set(session.execute(select(Alarm.t_ms, Alarm.msg_number, Alarm.state_after, Alarm.msg_class).where(
            Alarm.project_id == pid, Alarm.group_name == group, Alarm.t_ms >= lo, Alarm.t_ms <= hi)).all())
        new = []
        for a in batch["alarms"]:
            k = (a["t_ms"], a["msg_number"], a["state_after"], a["msg_class"])
            if k in have:
                continue
            have.add(k)
            params = [p for p in a["params"] if p]
            new.append({"project_id": pid, "group_name": group, "t_ms": a["t_ms"],
                        "msg_number": a["msg_number"], "msg_class": a["msg_class"],
                        "state_after": a["state_after"], "msg_proc": a["msg_proc"],
                        "text": a["text"][:500], "plc": a["plc"],
                        "params": json.dumps(params, ensure_ascii=False) if params else None,
                        "checksum": a["checksum"]})
        for i in range(0, len(new), 5000):
            session.execute(insert(Alarm), new[i:i + 5000])
        inserted += len(new)
        max_t = hi if max_t is None else max(max_t, hi)

    # ---- audit trail de WinCC
    if batch["audit"]:
        lo = min(a["t_ms"] for a in batch["audit"])
        hi = max(a["t_ms"] for a in batch["audit"])
        have = set(session.execute(select(WinccAudit.t_ms, WinccAudit.record_id, WinccAudit.checksum).where(
            WinccAudit.project_id == pid, WinccAudit.t_ms >= lo, WinccAudit.t_ms <= hi)).all())
        new = []
        for a in batch["audit"]:
            k = (a["t_ms"], a["record_id"], a["checksum"])
            if k in have:
                continue
            have.add(k)
            new.append({"project_id": pid, **{c: a[c] for c in (
                "record_id", "t_ms", "delta_utc", "user_id", "object_id", "description", "comment", "checksum")}})
        if new:
            session.execute(insert(WinccAudit), new)
            inserted += len(new)
        max_t = hi if max_t is None else max(max_t, hi)

    sf.fingerprint = batch["fingerprint"]
    sf.rt_count = batch["rt_count"]
    sf.last_load_ms = dt_to_epoch_ms(datetime.now())
    sf.last_error = None
    if max_t is not None:
        sf.loaded_t_ms = max_t if sf.loaded_t_ms is None else max(sf.loaded_t_ms, max_t)
    return inserted


def retention_cutoff_ms(settings: Settings) -> int | None:
    if not settings.retention_days:
        return None
    return dt_to_epoch_ms(datetime.now()) - settings.retention_days * 86_400_000


def load_pending(settings: Settings | None = None, paths: Paths | None = None) -> dict:
    """Etapa 2. Carga en la BD todos los lotes pendientes, en orden."""
    settings = settings or get_settings()
    paths = paths or Paths(settings)
    cutoff = retention_cutoff_ms(settings)
    summary = {"loaded_batches": 0, "inserted": 0, "pending": 0, "errors": []}
    for f in sorted(paths.pending.glob("*.json.gz")):
        try:
            batch = json.loads(gzip.decompress(f.read_bytes()).decode("utf-8"))
        except Exception as e:  # noqa: BLE001  lote corrupto: se aparta, no se borra
            bad = paths.pending.parent / "corrupt"
            bad.mkdir(exist_ok=True)
            shutil.move(str(f), bad / f.name)
            summary["errors"].append(f"Lote corrupto apartado {f.name}: {e}")
            continue
        try:
            with session_scope() as s:
                summary["inserted"] += _load_batch(s, batch, cutoff)
        except Exception as e:  # noqa: BLE001  BD no disponible, etc.: queda en cola
            log.exception("Error cargando lote %s", f.name)
            summary["errors"].append(f"{f.name}: {e}")
            break  # se conserva el orden; se reintentara
        shutil.move(str(f), paths.done / f.name)
        summary["loaded_batches"] += 1
    summary["pending"] = len(list(paths.pending.glob("*.json.gz")))
    return summary


# ------------------------------------------------------- mantenimiento y ciclo
def purge(settings: Settings | None = None, paths: Paths | None = None) -> dict:
    """Borra muestras fuera de la ventana de retencion y lotes 'done' antiguos.

    Alarmas y audit trail NO se purgan (son pequenos y tienen valor regulatorio)."""
    settings = settings or get_settings()
    paths = paths or Paths(settings)
    out = {"samples_deleted": 0, "batches_deleted": 0}
    cutoff = retention_cutoff_ms(settings)
    if cutoff is not None:
        with session_scope() as s:
            r = s.execute(delete(Sample).where(Sample.t_ms < cutoff))
            out["samples_deleted"] = r.rowcount or 0
            s.execute(delete(RuntimeEvent).where(RuntimeEvent.t_ms < cutoff))
    limit = time.time() - settings.ingest.keep_done_batches_days * 86400
    for f in paths.done.glob("*.json.gz"):
        if f.stat().st_mtime < limit:
            f.unlink(missing_ok=True)
            out["batches_deleted"] += 1
    return out


def forget_state_if_db_empty(settings: Settings, paths: Paths) -> list[str]:
    """BD nueva o vaciada (gestor.db borrada, cambio de motor SQL) con data\\ intacta.

    La marca de lectura diria que los CSV ya se leyeron y el historico no volveria a la BD.
    Si un proyecto tiene marcas pero ningun fichero cargado en la BD y no quedan lotes en
    cola, se olvidan sus marcas para releer los CSV completos (la carga es idempotente)."""
    if any(paths.pending.glob("*.json.gz")):
        return []  # quedan lotes por cargar (p. ej. BD caida): no es una BD nueva
    state: dict = _read_json(paths.state, {})
    if not state:
        return []
    try:
        with session_scope() as s:
            known = set(s.scalars(select(SourceFile.project_id).distinct()))
    except Exception:  # noqa: BLE001  BD no disponible: no tocar nada, la recoleccion sigue
        log.exception("No se pudo comprobar la BD antes de recolectar")
        return []
    reset = sorted({k.split("|", 1)[0] for k in state} - known)
    if reset:
        log.warning("BD sin datos de %s: se releen sus CSV completos", ", ".join(reset))
        state = {k: v for k, v in state.items() if k.split("|", 1)[0] not in reset}
        _atomic_write_bytes(paths.state, json.dumps(state, indent=1).encode("utf-8"))
    return reset


def run_ingest(settings: Settings | None = None, do_purge: bool = False, reason: str = "manual") -> dict:
    """Ciclo completo: cargar pendientes -> recolectar -> cargar -> (purgar) -> asociar tags."""
    settings = settings or get_settings()
    paths = Paths(settings)
    if not _run_lock.acquire(blocking=False):
        return {"status": "busy"}
    started = time.time()
    try:
        with _process_lock(paths.lock):
            result = {"reason": reason, "started": datetime.now().isoformat(timespec="seconds")}
            result["load_before"] = load_pending(settings, paths)
            result["reread_projects"] = forget_state_if_db_empty(settings, paths)
            result["collect"] = collect(settings, paths)
            result["load"] = load_pending(settings, paths)
            if do_purge:
                result["purge"] = purge(settings, paths)
            from .limits import refresh_alarm_tags, restore_limits_from_backup  # import diferido (evita ciclo)
            result["alarm_tags"] = refresh_alarm_tags()
            # con BD nueva las variables aparecen recien ahora: restaurar sus limites del respaldo
            result["limits_restored"] = {p.id: n for p in settings.projects
                                         if (n := restore_limits_from_backup(p.id))}
            result["seconds"] = round(time.time() - started, 2)
            result["status"] = "ok" if not (result["collect"]["errors"] or result["load"]["errors"]) else "con_errores"
            _atomic_write_bytes(paths.status, json.dumps(result, indent=1, ensure_ascii=False).encode("utf-8"))
            return result
    finally:
        _run_lock.release()


def last_status(settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    paths = Paths(settings)
    st = _read_json(paths.status, {})
    st["pending_batches"] = len(list(paths.pending.glob("*.json.gz")))
    return st
