"""Volumen real: 3 meses (92 dias) a 1 registro/min. Lenta: se salta con  pytest -m "not lento"."""
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from app import ingest
from app.analytics import analyze_variable
from app.db import session_scope
from app.models import Sample, Variable
from app.wincc_csv import dt_to_epoch_ms

from .conftest import HEADER

DAYS, VARS = 92, 3
END = datetime(2026, 11, 20)
START = END - timedelta(days=DAYS)
N = DAYS * 1440                       # 132.480 filas por variable (WinCC admite 500.000 por fichero)

# limites de tiempo en el PC de desarrollo, con margen para el PC de planta
MAX_INGEST_S = 180
MAX_ANALYSIS_7D_S = 5
MAX_ANALYSIS_92D_S = 10


def _fast_lines(var: str) -> str:
    """Mismo formato que conftest.log_line, sin strptime por fila (generar 400.000 filas rapido)."""
    out = []
    ole0 = (START - datetime(1899, 12, 30)).total_seconds() / 86400
    for i in range(N):
        t = START + timedelta(minutes=i)
        ole = f"{(ole0 + i / 1440) * 1e6:.4f}".replace(".", ",")
        val = f"{20 + (i % 600) / 100:.2f}".replace(".", ",")
        out.append(f'"{var}";"{t:%d/%m/%Y %H:%M:%S}";{val};1;{ole};AbCdEf\n')
    return HEADER + "".join(out)


@pytest.mark.lento
def test_three_months_ingest_and_analysis(env):
    for k in range(VARS):
        (env["proj"] / "Registros" / f"File_V{k}0.csv").write_text(_fast_lines(f"V{k}.Proc_Var"), encoding="cp1252")

    t0 = time.perf_counter()
    r = ingest.run_ingest()
    t_ingest = time.perf_counter() - t0
    assert r["status"] == "ok", r
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Sample)) == VARS * N
        vids = list(s.scalars(select(Variable.id).order_by(Variable.var_name)))

    t0 = time.perf_counter()
    assert ingest.run_ingest()["collect"]["rows"] == 0           # segunda pasada: nada que leer
    t_idle = time.perf_counter() - t0

    end_ms = dt_to_epoch_ms(END)
    t0 = time.perf_counter()
    res7 = [analyze_variable(v, end_ms - 7 * 86_400_000, end_ms, 60) for v in vids]
    t_7d = (time.perf_counter() - t0) / VARS
    assert all(x["stats"]["n"] == 7 * 1440 and len(x["series"]) == 7 * 1440 for x in res7)

    t0 = time.perf_counter()
    res92 = [analyze_variable(v, dt_to_epoch_ms(START), end_ms, 3600) for v in vids]
    t_92d = (time.perf_counter() - t0) / VARS
    assert all(x["stats"]["n"] == N and len(x["series"]) == DAYS * 24 for x in res92)

    print(f"\n  ingesta {VARS}x{N} filas: {t_ingest:.1f} s | pasada sin cambios: {t_idle:.2f} s"
          f" | analisis 7 d a 1 min: {t_7d:.2f} s/var | 92 d a 1 h: {t_92d:.2f} s/var")
    assert t_ingest < MAX_INGEST_S
    assert t_idle < 5
    assert t_7d < MAX_ANALYSIS_7D_S
    assert t_92d < MAX_ANALYSIS_92D_S
