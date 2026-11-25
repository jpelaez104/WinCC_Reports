"""La ingesta no debe perder ni duplicar datos en ningun escenario."""
import pytest
from sqlalchemy import func, select

from app import ingest
from app.db import session_scope
from app.models import Sample

from .conftest import HEADER, log_line


def count_samples():
    with session_scope() as s:
        return s.scalar(select(func.count()).select_from(Sample))


def write(path, lines, mode="w"):
    with open(path, mode, encoding="cp1252", newline="") as fh:
        if mode == "w":
            fh.write(HEADER)
        fh.writelines(lines)


def minutes(var, start_min, n, day="20/11/2026"):
    return [log_line(var, f"{day} {10 + (start_min + i) // 60:02d}:{(start_min + i) % 60:02d}:00", 20 + i)
            for i in range(n)]


def test_incremental_and_idempotent(env):
    f = env["proj"] / "Registros" / "File_T0.csv"
    write(f, minutes("T1", 0, 10))
    ingest.run_ingest()
    assert count_samples() == 10
    ingest.run_ingest()                     # sin cambios: nada nuevo
    assert count_samples() == 10
    write(f, minutes("T1", 10, 5), mode="a")  # WinCC agrega filas
    r = ingest.run_ingest()
    assert count_samples() == 15
    assert r["collect"]["rows"] == 6        # 5 nuevas + la ultima repetida (>= marca), ignorada al cargar


def test_file_restarted_by_wincc_is_reread(env):
    f = env["proj"] / "Registros" / "File_T0.csv"
    write(f, minutes("T1", 0, 10))
    ingest.run_ingest()
    # WinCC reinicia el fichero: contenido distinto desde el principio (otra primera fila)
    write(f, minutes("T1", 5, 20))
    ingest.run_ingest()
    assert count_samples() == 25            # 0..24, sin duplicados


def test_keep_files_are_ingested_without_duplicates(env):
    d = env["proj"] / "Registros"
    write(d / "File_T0.csv.keep", minutes("T1", 0, 10))
    write(d / "File_T0.csv", minutes("T1", 5, 10))
    ingest.run_ingest()
    assert count_samples() == 15


def test_db_failure_keeps_batch_in_queue(env, monkeypatch):
    f = env["proj"] / "Registros" / "File_T0.csv"
    write(f, minutes("T1", 0, 10))

    def boom(*a, **k):
        raise RuntimeError("BD caida")
    monkeypatch.setattr(ingest, "_load_batch", boom)
    r = ingest.run_ingest()
    assert r["load"]["pending"] == 1 and count_samples() == 0
    # el fichero se sigue escribiendo mientras la BD esta caida
    write(f, minutes("T1", 10, 5), mode="a")
    ingest.run_ingest()
    monkeypatch.undo()
    r = ingest.run_ingest()                 # BD recuperada: se vacia la cola en orden
    assert r["load_before"]["pending"] == 0
    assert count_samples() == 15


def test_crash_after_batch_before_state_is_safe(env, monkeypatch):
    """Si el proceso muere tras escribir el lote pero antes de guardar la marca, se repite (idempotente)."""
    f = env["proj"] / "Registros" / "File_T0.csv"
    write(f, minutes("T1", 0, 10))
    paths = ingest.Paths(env["settings"])
    real = ingest._atomic_write_bytes
    calls = {"n": 0}

    def flaky(path, data):
        real(path, data)
        if path == paths.state:
            calls["n"] += 1
            if calls["n"] == 1:
                raise SystemExit("corte de energia")
    monkeypatch.setattr(ingest, "_atomic_write_bytes", flaky)
    with pytest.raises(SystemExit):
        ingest.collect(env["settings"], paths)
    paths.state.unlink(missing_ok=True)      # la marca nunca llego a disco
    monkeypatch.undo()
    ingest.run_ingest()
    assert count_samples() == 10
