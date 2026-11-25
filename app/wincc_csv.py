"""Lectura de los ficheros CSV que genera WinCC Runtime Advanced (TIA Portal).

Formato observado:
  * separador ';', comillas '"', coma decimal, codificacion ANSI (Windows-1252)
  * Time_ms = fecha OLE (dias desde 30/12/1899) * 1e6, en hora LOCAL del panel
  * filas especiales: "$RT_OFF$" (runtime detenido) y "$RT_COUNT$" (ultima fila)
  * cada fila lleva un Checksum generado por WinCC

Tres tipos de fichero:
  * registro de variables:  VarName;TimeString;VarValue;Validity;Time_ms;Checksum
  * avisos / alarmas:       Time_ms;MsgProc;StateAfter;MsgClass;MsgNumber;Var1..Var8;TimeString;MsgText;PLC;Checksum
  * audit trail:            RecordID;TimeStamp;DeltaToUTC;UserID;ObjectID;Description;Comment;Checksum
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ENCODING = "cp1252"
OLE_EPOCH_OFFSET_DAYS = 25569  # 1970-01-01 en fecha OLE
MS_PER_DAY = 86_400_000

KIND_LOG = "log"
KIND_ALARM = "alarm"
KIND_AUDIT = "audit"


# ----------------------------------------------------------------- utilidades
def parse_number(text: str | None) -> float | None:
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    try:
        return float(t.replace(",", "."))
    except ValueError:
        return None


def time_ms_to_epoch_ms(text: str) -> int | None:
    """Convierte el campo Time_ms de WinCC a milisegundos epoch (hora local de planta)."""
    v = parse_number(text)
    if v is None:
        return None
    return round((v / 1e6 - OLE_EPOCH_OFFSET_DAYS) * MS_PER_DAY)


def parse_timestring(text: str) -> int | None:
    """'19/11/2026 11:51:25' -> epoch ms (hora local de planta, sin zona)."""
    try:
        dt = datetime.strptime(text.strip(), "%d/%m/%Y %H:%M:%S")
    except (ValueError, AttributeError):
        return None
    return round((dt - datetime(1970, 1, 1)).total_seconds() * 1000)


def epoch_ms_to_dt(ms: int) -> datetime:
    from datetime import timedelta
    return datetime(1970, 1, 1) + timedelta(milliseconds=int(ms))


def dt_to_epoch_ms(dt: datetime) -> int:
    return round((dt.replace(tzinfo=None) - datetime(1970, 1, 1)).total_seconds() * 1000)


def row_fingerprint(fields: list[str]) -> str:
    return hashlib.sha1(";".join(fields).encode("utf-8", "replace")).hexdigest()


# ---------------------------------------------------------------- estructuras
@dataclass(slots=True)
class LogSample:
    var_name: str
    t_ms: int
    value: float | None
    validity: int | None
    checksum: str


@dataclass(slots=True)
class RtEvent:
    t_ms: int
    kind: str  # "RT_OFF"
    checksum: str


@dataclass(slots=True)
class AlarmRow:
    t_ms: int
    msg_proc: int | None
    state_after: int | None
    msg_class: int | None
    msg_number: int | None
    text: str
    plc: str
    params: list[str]
    checksum: str


@dataclass(slots=True)
class AuditRow:
    record_id: int | None
    t_ms: int
    delta_utc: str
    user_id: str
    object_id: str
    description: str
    comment: str
    checksum: str


@dataclass
class ParseResult:
    kind: str
    path: Path
    fingerprint: str | None = None      # huella de la primera fila de datos (detecta reinicio del fichero)
    rt_count: int | None = None          # valor de la fila $RT_COUNT$
    samples: list[LogSample] = field(default_factory=list)
    events: list[RtEvent] = field(default_factory=list)
    alarms: list[AlarmRow] = field(default_factory=list)
    audit: list[AuditRow] = field(default_factory=list)
    skipped: int = 0                     # filas ilegibles (p. ej. ultima linea a medio escribir)

    @property
    def max_t_ms(self) -> int | None:
        ts = [s.t_ms for s in self.samples] + [e.t_ms for e in self.events] + \
             [a.t_ms for a in self.alarms] + [a.t_ms for a in self.audit]
        return max(ts) if ts else None


# ------------------------------------------------------------------- lectura
def detect_kind(header: list[str]) -> str:
    h = [c.strip().lower() for c in header]
    if h[:2] == ["varname", "timestring"]:
        return KIND_LOG
    if h and h[0] == "time_ms" and "msgtext" in h:
        return KIND_ALARM
    if h and h[0] == "recordid":
        return KIND_AUDIT
    raise ValueError(f"Cabecera CSV no reconocida: {header}")


def _rows(path: Path):
    with open(path, encoding=ENCODING, errors="replace", newline="") as fh:
        yield from csv.reader(fh, delimiter=";", quotechar='"')


def parse_file(path: str | Path, min_t_ms: int | None = None) -> ParseResult:
    """Lee un CSV de WinCC completo. Si min_t_ms se indica, descarta filas mas antiguas."""
    path = Path(path)
    it = _rows(path)
    try:  # cerrar SIEMPRE el fichero: en Windows un fichero abierto no se puede borrar ni rotar
        return _parse_rows(it, path, min_t_ms)
    finally:
        it.close()


def _parse_rows(it, path: Path, min_t_ms: int | None) -> ParseResult:
    try:
        header = next(it)
    except StopIteration:
        raise ValueError(f"Fichero vacio: {path}")
    kind = detect_kind(header)
    res = ParseResult(kind=kind, path=path)
    parser = {KIND_LOG: _parse_log_row, KIND_ALARM: _parse_alarm_row, KIND_AUDIT: _parse_audit_row}[kind]
    for fields in it:
        if not fields or all(not f for f in fields):
            continue
        first = fields[0].strip()
        if first == "$RT_COUNT$":
            try:
                res.rt_count = int(fields[1])
            except (IndexError, ValueError):
                pass
            continue
        if res.fingerprint is None:
            res.fingerprint = row_fingerprint(fields)
        try:
            parser(fields, res, min_t_ms)
        except (IndexError, ValueError, TypeError):
            res.skipped += 1
    return res


def _keep(t_ms: int | None, min_t_ms: int | None) -> bool:
    return t_ms is not None and (min_t_ms is None or t_ms >= min_t_ms)


def _parse_log_row(f: list[str], res: ParseResult, min_t_ms: int | None) -> None:
    if len(f) < 6:
        raise ValueError("fila corta")
    name = f[0].strip()
    t_ms = time_ms_to_epoch_ms(f[4])
    if t_ms is None:
        t_ms = parse_timestring(f[1])
    if t_ms is None:
        raise ValueError("sin tiempo")
    if not _keep(t_ms, min_t_ms):
        return
    if name.startswith("$RT_"):
        res.events.append(RtEvent(t_ms=t_ms, kind=name.strip("$"), checksum=f[5]))
        return
    validity = int(f[3]) if f[3].strip() else None
    res.samples.append(LogSample(name, t_ms, parse_number(f[2]), validity, f[5]))


def _int(s: str) -> int | None:
    s = s.strip()
    return int(s) if s.lstrip("-").isdigit() else None


def _parse_alarm_row(f: list[str], res: ParseResult, min_t_ms: int | None) -> None:
    if len(f) < 17:
        raise ValueError("fila corta")
    t_ms = time_ms_to_epoch_ms(f[0]) or parse_timestring(f[13])
    if not _keep(t_ms, min_t_ms):
        return
    res.alarms.append(AlarmRow(
        t_ms=t_ms, msg_proc=_int(f[1]), state_after=_int(f[2]), msg_class=_int(f[3]),
        msg_number=_int(f[4]), params=[p for p in f[5:13]], text=f[14].strip(),
        plc=f[15].strip(), checksum=f[16]))


def _parse_audit_row(f: list[str], res: ParseResult, min_t_ms: int | None) -> None:
    if len(f) < 8:
        raise ValueError("fila corta")
    t_ms = parse_timestring(f[1])
    if not _keep(t_ms, min_t_ms):
        return
    res.audit.append(AuditRow(
        record_id=_int(f[0]), t_ms=t_ms, delta_utc=f[2].strip(), user_id=f[3].strip(),
        object_id=f[4].strip(), description=f[5].strip(), comment=f[6].strip(), checksum=f[7]))


def file_group(path: Path) -> str:
    """Nombre del sensor/grupo a partir del nombre del fichero.

    'File_S1_Temperatura0.csv' -> 'S1_Temperatura'; 'File_TT-S6_0.csv' -> 'TT-S6';
    'FileAlarms HVAC0.csv.keep.1' -> 'Alarms HVAC'.
    """
    name = path.name
    for suffix in (".keep",):
        if suffix in name:
            name = name[: name.index(suffix)]
    stem = Path(name).stem
    if stem.lower().startswith("file"):
        stem = stem[4:]
    stem = stem.lstrip("_ ")
    # quitar el indice de segmento final de WinCC (0, 1, ...)
    while stem and stem[-1].isdigit():
        stem = stem[:-1]
    return stem.rstrip("_ ") or path.stem
