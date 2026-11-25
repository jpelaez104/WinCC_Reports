"""Modelo de datos (SQLAlchemy 2). Solo tipos genericos para poder cambiar de motor SQL.

Tiempos: enteros 'epoch ms' en hora LOCAL de planta (igual que WinCC) -> columnas *_ms.
"""
from __future__ import annotations

from sqlalchemy import (BigInteger, Boolean, Float, ForeignKey, Index, Integer, SmallInteger,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ proyectos
class Project(Base):
    __tablename__ = "projects"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    root: Mapped[str] = mapped_column(String(500))


class SourceFile(Base):
    """Cada CSV (incluidos los .keep) con su estado de lectura."""
    __tablename__ = "source_files"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    category: Mapped[str] = mapped_column(String(20))          # registros/analizador/alarmas/audit
    rel_path: Mapped[str] = mapped_column(String(500))
    group_name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str | None] = mapped_column(String(10))       # log/alarm/audit
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    loaded_t_ms: Mapped[int | None] = mapped_column(BigInteger)  # ultimo tiempo cargado en BD
    rt_count: Mapped[int | None] = mapped_column(Integer)
    last_size: Mapped[int | None] = mapped_column(BigInteger)
    last_mtime: Mapped[float | None] = mapped_column(Float)
    last_load_ms: Mapped[int | None] = mapped_column(BigInteger)
    last_error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("project_id", "rel_path"),)


# ------------------------------------------------------------------ variables
class Variable(Base):
    __tablename__ = "variables"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    var_name: Mapped[str] = mapped_column(String(300))          # nombre en WinCC
    group_name: Mapped[str] = mapped_column(String(200))        # fichero / sensor
    category: Mapped[str] = mapped_column(String(20))
    alias: Mapped[str | None] = mapped_column(String(200))      # nombre legible (p. ej. "TT-101 Sala 1")
    unit: Mapped[str | None] = mapped_column(String(30))
    tag: Mapped[str | None] = mapped_column(String(100))        # tag de alarma asociado (p. ej. "TT-101")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    first_t_ms: Mapped[int | None] = mapped_column(BigInteger)
    last_t_ms: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("project_id", "var_name"),)


class Sample(Base):
    __tablename__ = "samples"
    variable_id: Mapped[int] = mapped_column(ForeignKey("variables.id"), primary_key=True)
    t_ms: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    value: Mapped[float | None] = mapped_column(Float)
    validity: Mapped[int | None] = mapped_column(SmallInteger)  # 1 = valido
    checksum: Mapped[str | None] = mapped_column(String(16))    # checksum original de WinCC


class RuntimeEvent(Base):
    """$RT_OFF$: el runtime de WinCC estuvo detenido (hueco legitimo en los datos)."""
    __tablename__ = "runtime_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    group_name: Mapped[str] = mapped_column(String(200))
    t_ms: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(20))
    __table_args__ = (UniqueConstraint("project_id", "group_name", "t_ms", "kind"),)


# -------------------------------------------------------------------- alarmas
class Alarm(Base):
    __tablename__ = "alarms"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    group_name: Mapped[str] = mapped_column(String(200))        # FileAlarms HVAC, SystemAlarms...
    t_ms: Mapped[int] = mapped_column(BigInteger)
    msg_number: Mapped[int | None] = mapped_column(Integer)
    msg_class: Mapped[int | None] = mapped_column(Integer)
    state_after: Mapped[int | None] = mapped_column(SmallInteger)  # 1 = entra, 0 = sale, 2/3 acuse...
    msg_proc: Mapped[int | None] = mapped_column(SmallInteger)
    text: Mapped[str] = mapped_column(String(500))
    plc: Mapped[str | None] = mapped_column(String(100))
    params: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str | None] = mapped_column(String(16))
    __table_args__ = (
        UniqueConstraint("project_id", "group_name", "t_ms", "msg_number", "state_after", "msg_class"),
        Index("ix_alarms_project_t", "project_id", "t_ms"),
    )


class AlarmTag(Base):
    """Tags con alarmas analogicas detectadas en FileAlarms (niveles HH/H/L/LL/Fallo)."""
    __tablename__ = "alarm_tags"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    tag: Mapped[str] = mapped_column(String(100))
    levels: Mapped[str] = mapped_column(String(100))            # "HH,H,L,LL,FALLO"
    occurrences: Mapped[int] = mapped_column(Integer, default=0)
    first_t_ms: Mapped[int | None] = mapped_column(BigInteger)
    last_t_ms: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("project_id", "tag"),)


class VariableLimits(Base):
    """Limites de alarma (HH/LL) y pre-alarma (H/L) por variable. Persisten hasta que se cambien."""
    __tablename__ = "variable_limits"
    variable_id: Mapped[int] = mapped_column(ForeignKey("variables.id"), primary_key=True)
    ll: Mapped[float | None] = mapped_column(Float)
    l: Mapped[float | None] = mapped_column(Float)
    h: Mapped[float | None] = mapped_column(Float)
    hh: Mapped[float | None] = mapped_column(Float)
    range_min: Mapped[float | None] = mapped_column(Float)      # rango del instrumento (fuera => fallo de senal)
    range_max: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(30), default="manual")  # manual / sugerido_alarmas
    note: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(String(100))
    updated_ms: Mapped[int | None] = mapped_column(BigInteger)


# ------------------------------------------------------------- audit trail WinCC
class WinccAudit(Base):
    __tablename__ = "wincc_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    record_id: Mapped[int | None] = mapped_column(Integer)
    t_ms: Mapped[int] = mapped_column(BigInteger)
    delta_utc: Mapped[str | None] = mapped_column(String(10))
    user_id: Mapped[str | None] = mapped_column(String(100))
    object_id: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str | None] = mapped_column(String(16))
    __table_args__ = (
        UniqueConstraint("project_id", "t_ms", "record_id", "checksum"),
        Index("ix_wincc_audit_project_t", "project_id", "t_ms"),
    )


# ---------------------------------------------------------- usuarios y sesiones
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(20))             # wincc / desarrollador
    wincc_group: Mapped[str | None] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default="lector")
    password_hash: Mapped[str | None] = mapped_column(String(300))
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_ms: Mapped[int | None] = mapped_column(BigInteger)
    last_login_ms: Mapped[int | None] = mapped_column(BigInteger)


class UserSession(Base):
    __tablename__ = "user_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_ms: Mapped[int] = mapped_column(BigInteger)
    expires_ms: Mapped[int] = mapped_column(BigInteger)


# ----------------------------------------------------------- informes firmados
class Report(Base):
    """Informe PDF. Los datos se congelan al generarlo (payload) y su hash SHA-256 se imprime y firma."""
    __tablename__ = "reports"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    title: Mapped[str] = mapped_column(String(300))
    comment: Mapped[str | None] = mapped_column(Text)
    start_ms: Mapped[int] = mapped_column(BigInteger)
    end_ms: Mapped[int] = mapped_column(BigInteger)
    step_s: Mapped[int] = mapped_column(Integer)
    variable_ids: Mapped[str] = mapped_column(Text)             # JSON
    data_hash: Mapped[str] = mapped_column(String(64))
    payload_path: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20))             # elaborado / revisado / aprobado
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_ms: Mapped[int] = mapped_column(BigInteger)
    created_by: Mapped[str] = mapped_column(String(100))


class ReportSignature(Base):
    """Firma electronica: usuario + contrasena re-ingresada + significado (Elaborado/Revisado/Aprobado)."""
    __tablename__ = "report_signatures"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"))
    username: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(20))
    meaning: Mapped[str] = mapped_column(String(20))
    comment: Mapped[str | None] = mapped_column(Text)
    signed_ms: Mapped[int] = mapped_column(BigInteger)
    data_hash: Mapped[str] = mapped_column(String(64))
    record_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("report_id", "meaning"), UniqueConstraint("report_id", "username"))


class ReportFile(Base):
    """Cada version del PDF emitida (una por firma), con su SHA-256 para verificar copias."""
    __tablename__ = "report_files"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"))
    version: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(String(500))
    created_ms: Mapped[int] = mapped_column(BigInteger)
    pades: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("report_id", "version"),)


# ------------------------------------------------------- audit trail de la app
class ReportExport(Base):
    """Otros formatos de cada version del informe (hoy: Excel .xlsx con su sello .p7s separado).

    Tabla aparte de report_files para no alterar tablas existentes (sin migraciones todavia)."""
    __tablename__ = "report_exports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"))
    version: Mapped[int] = mapped_column(Integer)
    fmt: Mapped[str] = mapped_column(String(10))                 # "xlsx"
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(String(500))
    sig_sha256: Mapped[str | None] = mapped_column(String(64), index=True)   # del .p7s
    sig_path: Mapped[str | None] = mapped_column(String(500))
    created_ms: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (UniqueConstraint("report_id", "version", "fmt"),)


class AppAudit(Base):
    """Registro de auditoria propio, solo-agregar, encadenado por hash (deteccion de manipulacion)."""
    __tablename__ = "app_audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str | None] = mapped_column(String(200))
    details: Mapped[str | None] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
