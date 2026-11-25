"""Carga y validacion de config/config.yaml."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent.parent
CATEGORIES = ("registros", "analizador", "alarmas", "audit")


class ProjectCfg(BaseModel):
    id: str
    name: str
    root: str
    folders: dict[str, list[str]] = Field(default_factory=dict)

    @property
    def root_path(self) -> Path:
        p = Path(self.root)
        return p if p.is_absolute() else (BASE_DIR / p).resolve()


class IngestCfg(BaseModel):
    interval_minutes: int = 15
    daily_time: str = "12:00"
    keep_done_batches_days: int = 7


class ServerCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8080


class SecurityCfg(BaseModel):
    session_minutes: int = 480
    max_failed_logins: int = 5


class AnalysisCfg(BaseModel):
    flatline_minutes: int = 60
    outlier_mad_z: float = 6.0
    gap_factor: float = 3.0


class ReportCfg(BaseModel):
    organization: str = "STE-EngiPharm"
    location: str = "Planta"
    max_table_rows: int = 20_000          # filas de la tabla de valores (periodo / muestreo)
    max_episode_rows: int = 60            # eventos por variable listados en el PDF
    max_wincc_audit_rows: int = 500
    pades: bool = True          # sello digital: PAdES en el PDF y .p7s en el Excel
    xlsx: bool = True           # emitir tambien el informe en Excel en cada version                    # sello digital PAdES del PDF
    cert_pfx: str | None = None           # certificado corporativo .pfx (si no, se genera uno propio)
    cert_password_env: str = "GESTOR_CERT_PASSWORD"


class DatabaseCfg(BaseModel):
    url: str = "sqlite:///data/gestor.db"


class Settings(BaseModel):
    database: DatabaseCfg = DatabaseCfg()
    data_dir: str = "data"
    retention_days: int = 92
    ingest: IngestCfg = IngestCfg()
    server: ServerCfg = ServerCfg()
    security: SecurityCfg = SecurityCfg()
    analysis: AnalysisCfg = AnalysisCfg()
    report: ReportCfg = ReportCfg()
    projects: list[ProjectCfg] = Field(default_factory=list)

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else (BASE_DIR / p).resolve()

    @property
    def db_url(self) -> str:
        url = self.database.url
        prefix = "sqlite:///"
        if url.startswith(prefix) and not url.startswith(prefix + "/") and ":memory:" not in url:
            rel = url[len(prefix):]
            if not Path(rel).is_absolute():
                url = prefix + (BASE_DIR / rel).resolve().as_posix()
        return url

    def project(self, project_id: str) -> ProjectCfg:
        for p in self.projects:
            if p.id == project_id:
                return p
        raise KeyError(project_id)


def load_settings(path: str | os.PathLike | None = None) -> Settings:
    path = Path(path or os.environ.get("GESTOR_CONFIG", BASE_DIR / "config" / "config.yaml"))
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Settings.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
