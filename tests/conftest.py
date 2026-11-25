import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml
from hypothesis import HealthCheck, settings

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Perfiles de hypothesis:  normal (dia a dia)  |  exhaustivo:  $env:GESTOR_HYPOTHESIS="exhaustivo"
settings.register_profile("normal", max_examples=60, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("exhaustivo", max_examples=1000, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(os.environ.get("GESTOR_HYPOTHESIS", "normal"))

HEADER = '"VarName";"TimeString";"VarValue";"Validity";"Time_ms";"Checksum"\n'
DATOS = Path(__file__).resolve().parent / "datos"   # extractos reales de CSV de WinCC (golden files)


def oa(dt_str: str) -> str:
    """'19/11/2026 11:51:25' -> campo Time_ms de WinCC (fecha OLE * 1e6, coma decimal)."""
    from datetime import datetime
    dt = datetime.strptime(dt_str, "%d/%m/%Y %H:%M:%S")
    days = (dt - datetime(1899, 12, 30)).total_seconds() / 86400
    return f"{days * 1e6:.4f}".replace(".", ",")


def log_line(var: str, ts: str, value: float, validity: int = 1, chk: str = "AbCdEf") -> str:
    return f'"{var}";"{ts}";{str(value).replace(".", ",")};{validity};{oa(ts)};{chk}\n'


@contextmanager
def make_env(tmp_path: Path, folders: dict | None = None):
    """Proyecto WinCC simulado + configuracion y BD temporales, apuntados con GESTOR_CONFIG.

    Se usa desde la fixture 'env' y tambien desde pruebas de hypothesis, que necesitan un
    entorno limpio por cada ejemplo generado."""
    folders = folders or {"registros": ["Registros"], "alarmas": ["Alarmas"], "audit": ["AuditTrail"]}
    proj = tmp_path / "Proyecto"
    for sub in folders.values():
        for f in sub:
            (proj / f).mkdir(parents=True, exist_ok=True)
    cfg = {
        "database": {"url": "sqlite:///" + (tmp_path / "t.db").as_posix()},
        "data_dir": str(tmp_path / "data"),
        "retention_days": 0,
        "projects": [{"id": "p", "name": "Prueba", "root": str(proj), "folders": folders}],
    }
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    old = os.environ.get("GESTOR_CONFIG")
    os.environ["GESTOR_CONFIG"] = str(cfg_path)

    from app import config, db
    config.get_settings.cache_clear()
    db._engine = None
    db._SessionLocal = None
    db.init_engine(config.get_settings())
    db.init_db(config.get_settings())
    try:
        yield {"proj": proj, "tmp": tmp_path, "settings": config.get_settings()}
    finally:
        if db._engine is not None:
            db._engine.dispose()
        db._engine = None
        db._SessionLocal = None
        config.get_settings.cache_clear()
        if old is None:
            os.environ.pop("GESTOR_CONFIG", None)
        else:
            os.environ["GESTOR_CONFIG"] = old


@pytest.fixture()
def env(tmp_path):
    with make_env(tmp_path) as e:
        yield e
