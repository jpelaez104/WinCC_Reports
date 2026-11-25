"""Pruebas por propiedades (hypothesis): miles de casos generados al azar que deben cumplir una regla.

Si hypothesis encuentra un caso que rompe la regla, lo reduce al ejemplo minimo y lo muestra.
"""
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from hypothesis import given, strategies as st
from sqlalchemy import func, select

from app import ingest
from app.db import session_scope
from app.models import Sample
from app.wincc_csv import dt_to_epoch_ms, epoch_ms_to_dt, parse_file, parse_number, time_ms_to_epoch_ms

from .conftest import HEADER, make_env

BASE = datetime(2026, 11, 20, 0, 0)


# --------------------------------------------------------------- conversiones
@given(st.floats(min_value=-1e9, max_value=1e9, allow_nan=False, allow_infinity=False))
def test_parse_number_reads_wincc_decimal_comma(x):
    assert parse_number(repr(x).replace(".", ",")) == x


@given(st.datetimes(min_value=datetime(2000, 1, 1), max_value=datetime(2099, 12, 31)))
def test_ole_time_ms_roundtrip_within_1ms(dt):
    dt = dt.replace(microsecond=(dt.microsecond // 1000) * 1000)
    ole = (dt - datetime(1899, 12, 30)).total_seconds() / 86400 * 1e6
    t = time_ms_to_epoch_ms(f"{ole:.4f}".replace(".", ","))
    assert abs(t - dt_to_epoch_ms(dt)) <= 1
    assert abs(epoch_ms_to_dt(t) - dt) <= timedelta(milliseconds=1)


def _row(i: int, var: str = "T1") -> str:
    """Fila de WinCC para el minuto i desde BASE; el valor depende del minuto (filas distintas)."""
    t = BASE + timedelta(minutes=i)
    ole = f"{(t - datetime(1899, 12, 30)).total_seconds() / 86400 * 1e6:.4f}".replace(".", ",")
    val = f"{20 + i / 100:.2f}".replace(".", ",")
    return f'"{var}";"{t:%d/%m/%Y %H:%M:%S}";{val};1;{ole};x{i}\n'


@given(st.lists(st.integers(0, 10_000), max_size=50), st.integers(0, 3))
def test_parser_never_loses_complete_rows(minutos, basura):
    """Cualquier fichero = cabecera + filas validas (+ una ultima linea a medio escribir)."""
    d = Path(tempfile.mkdtemp())
    try:
        f = d / "File_T0.csv"
        cola = ['"T1";"20/11/2026 10', '"T1";', '"', ''][basura]
        f.write_text(HEADER + "".join(_row(i) for i in minutos) + cola, encoding="cp1252")
        r = parse_file(f)
        assert [s.t_ms for s in r.samples] == [dt_to_epoch_ms(BASE + timedelta(minutes=i)) for i in minutos]
        # una linea a medio escribir con contenido se descarta y se cuenta; '"' solo es un campo vacio
        assert r.skipped == (1 if basura in (0, 1) else 0)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------ ingesta sin perdida
# Cada paso simula lo que hace WinCC con un fichero de registro entre dos ingestas:
OPS = st.one_of(
    st.tuples(st.just("agregar"), st.integers(1, 30)),            # escribe n filas nuevas al final
    st.tuples(st.just("reiniciar"), st.integers(-20, 20)),        # reescribe el fichero desde otro minuto
    st.tuples(st.just("rotar"), st.integers(1, 10)),              # .csv -> .csv.keep.N y sigue en uno nuevo
    st.tuples(st.just("ingerir"), st.just(0)),
    st.tuples(st.just("bd_caida"), st.just(0)),                    # una ingesta con la BD fallando
)


@given(st.lists(OPS, min_size=1, max_size=12))
def test_ingest_count_equals_distinct_rows_written(ops):
    """Regla: toda fila que exista en algun CSV cuando corre una ingesta termina en la BD, sin duplicados.

    (Una fila que WinCC escribe y sobrescribe ANTES de que el gestor lea el fichero no se puede
    recuperar: es el riesgo de dejar el gestor parado ~69 dias, no un fallo de la ingesta.)"""
    tmp = Path(tempfile.mkdtemp())
    try:
        with make_env(tmp) as env:
            reg = env["proj"] / "Registros"
            actual: list[int] = []
            ficheros: dict[str, list[int]] = {}     # contenido actual de cada fichero en disco
            escritos: set[int] = set()              # filas visibles en alguna ingesta
            siguiente, rotaciones = 0, 0

            def guardar():
                (reg / "File_T0.csv").write_text(HEADER + "".join(_row(i) for i in actual), encoding="cp1252")
                ficheros["File_T0.csv"] = list(actual)

            def visibles():
                for filas in ficheros.values():
                    escritos.update(filas)

            for op, n in ops:
                if op == "agregar":
                    actual += list(range(siguiente, siguiente + n))
                    siguiente += n
                    guardar()
                elif op == "reiniciar":
                    inicio = max(0, siguiente + n)
                    actual = list(range(inicio, inicio + 5))
                    siguiente = max(siguiente, inicio + 5)
                    guardar()
                elif op == "rotar" and (reg / "File_T0.csv").exists():
                    rotaciones += 1
                    (reg / "File_T0.csv").rename(reg / f"File_T0.csv.keep.{rotaciones}")
                    ficheros[f"File_T0.csv.keep.{rotaciones}"] = ficheros.pop("File_T0.csv")
                    actual = list(range(siguiente, siguiente + n))
                    siguiente += n
                    guardar()
                elif op == "ingerir":
                    visibles()
                    ingest.run_ingest()
                elif op == "bd_caida":
                    visibles()                      # la recoleccion corre aunque la BD falle
                    real = ingest._load_batch
                    ingest._load_batch = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("BD caida"))
                    try:
                        ingest.run_ingest()
                    finally:
                        ingest._load_batch = real

            visibles()
            ingest.run_ingest()
            with session_scope() as s:
                assert s.scalar(select(func.count()).select_from(Sample)) == len(escritos)
                ts = set(s.scalars(select(Sample.t_ms)))
            assert ts == {dt_to_epoch_ms(BASE + timedelta(minutes=i)) for i in escritos}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
