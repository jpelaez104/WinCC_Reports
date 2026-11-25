"""Informes firmados (PDF + Excel): generacion bajo demanda, firmas electronicas y verificacion.

Flujo (nada es automatico: solo se genera al pulsar "Generar reporte"):
  1. Elaborado  (rol operador o superior): congela los datos del analisis (payload + SHA-256), firma
     con usuario + contrasena re-ingresada y emite la version 1.
  2. Revisado   (supervisor o superior, otro usuario): nueva firma -> version 2 con los MISMOS datos.
  3. Aprobado   (supervisor o superior, otro usuario, tras la revision) -> version 3.
Cada version se emite en PDF (con sello PAdES) y en Excel (.xlsx con sello .p7s separado). Se registra
el SHA-256 de los tres ficheros para verificar copias.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import secrets
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from . import audit, auth
from .analytics import analyze_variable
from .config import get_settings
from .db import session_scope
from .models import Project, Report, ReportExport, ReportFile, ReportSignature, User, Variable, WinccAudit
from .wincc_csv import dt_to_epoch_ms

MEANINGS = ("Elaborado", "Revisado", "Aprobado")
MEANING_ROLE = {"Elaborado": "operador", "Revisado": "supervisor", "Aprobado": "supervisor"}
MEANING_STATUS = {"Elaborado": "elaborado", "Revisado": "revisado", "Aprobado": "aprobado"}


class ReportError(Exception):
    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.code = code


def now_ms() -> int:
    return dt_to_epoch_ms(datetime.now())


def _clean(obj):
    """NaN/inf -> None para que el payload sea JSON estricto y su hash reproducible."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def canonical_hash(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def reports_dir() -> Path:
    return get_settings().data_path / "reports"


def table_rows(start_ms: int, end_ms: int, step_s: int) -> int:
    return math.ceil((end_ms - start_ms) / (step_s * 1000))


def _check_signer(s, username: str, password: str, meaning: str) -> User:
    try:
        u = auth.verify_credentials(s, username, password, "FIRMA")
    except auth.AuthError as e:
        s.commit()  # persistir intentos fallidos y su registro de auditoria
        raise ReportError(f"Firma rechazada: {e}", 401)
    if not auth.has_role({"role": u.role}, MEANING_ROLE[meaning]):
        raise ReportError(f"Para firmar como '{meaning}' se requiere rol {MEANING_ROLE[meaning]} o superior", 403)
    return u


def _signature_record(report_id: str, u: User, meaning: str, comment: str | None, data_hash: str) -> ReportSignature:
    t = now_ms()
    rec = canonical_hash({"report": report_id, "user": u.username, "meaning": meaning, "t": t, "data": data_hash,
                          "comment": comment or ""})
    return ReportSignature(report_id=report_id, username=u.username, display_name=u.display_name, role=u.role,
                           meaning=meaning, comment=comment or None, signed_ms=t, data_hash=data_hash, record_hash=rec)


def _sig_dict(x: ReportSignature) -> dict:
    return {"meaning": x.meaning, "username": x.username, "display_name": x.display_name, "role": x.role,
            "comment": x.comment, "signed_ms": x.signed_ms, "record_hash": x.record_hash}


def _load_payload(path: str) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def _version_signatures(s, report_id: str, version: int) -> list[dict]:
    """Firmas vigentes en una version: la v1 tiene la 1.a firma, la v2 las dos primeras, etc."""
    sigs = s.scalars(select(ReportSignature).where(ReportSignature.report_id == report_id)
                     .order_by(ReportSignature.signed_ms, ReportSignature.id)).all()
    return [_sig_dict(x) for x in sigs[:version]]


def _emit_pdf(s, rep: Report, payload: dict) -> ReportFile:
    """Renderiza la version actual (con todas sus firmas), aplica PAdES y la registra. Luego el Excel."""
    from .report_pdf import render_pdf
    sigs = _version_signatures(s, rep.id, rep.version)
    cfg = get_settings().report
    payload = dict(payload, pades=cfg.pades)
    pdf = render_pdf(payload, sigs, rep.version, rep.status)
    if cfg.pades:
        from .pades import sign_pdf
        last = sigs[-1]
        pdf = sign_pdf(pdf, f"Informe {rep.id} v{rep.version} - {last['meaning']} por {last['username']}")
    d = reports_dir() / rep.id
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{rep.id}_v{rep.version}.pdf"
    _write_atomic(out, pdf)
    f = ReportFile(report_id=rep.id, version=rep.version, sha256=hashlib.sha256(pdf).hexdigest(), path=str(out),
                   created_ms=now_ms(), pades=cfg.pades)
    s.add(f)
    if cfg.xlsx:
        _emit_xlsx(s, rep, payload, rep.version, sigs, f.sha256)
    return f


def _emit_xlsx(s, rep: Report, payload: dict, version: int, sigs: list[dict], pdf_sha: str | None) -> ReportExport:
    """Excel de una version + sello .p7s separado (si el sello esta activo). Registra ambos SHA-256."""
    from .report_xlsx import render_xlsx
    status = MEANING_STATUS[sigs[-1]["meaning"]]
    xlsx = render_xlsx(payload, sigs, version, status, pdf_sha256=pdf_sha)
    d = reports_dir() / rep.id
    d.mkdir(parents=True, exist_ok=True)
    out = d / f"{rep.id}_v{version}.xlsx"
    _write_atomic(out, xlsx)
    sig_path = sig_sha = None
    if get_settings().report.pades:
        from .pades import sign_detached
        p7s = sign_detached(xlsx)
        sig_path = out.with_name(out.name + ".p7s")
        _write_atomic(sig_path, p7s)
        sig_sha = hashlib.sha256(p7s).hexdigest()
    e = ReportExport(report_id=rep.id, version=version, fmt="xlsx", sha256=hashlib.sha256(xlsx).hexdigest(),
                     path=str(out), sig_sha256=sig_sha, sig_path=str(sig_path) if sig_path else None,
                     created_ms=now_ms())
    s.add(e)
    s.flush()
    return e


# --------------------------------------------------------------------- crear
def create_report(*, project_id: str, variable_ids: list[int], start_ms: int, end_ms: int, step_s: int,
                  title: str | None, comment: str | None, username: str, password: str,
                  sign_comment: str | None = None) -> dict:
    cfg = get_settings().report
    if end_ms <= start_ms:
        raise ReportError("La fecha final debe ser posterior a la inicial", 422)
    n = table_rows(start_ms, end_ms, step_s)
    if n > cfg.max_table_rows:
        min_step = math.ceil((end_ms - start_ms) / 1000 / cfg.max_table_rows)
        raise ReportError(f"El periodo con ese muestreo genera {n} filas de valores tabulados (máximo "
                          f"{cfg.max_table_rows}). Use un muestreo de al menos {min_step} s o acorte el periodo.", 422)
    ids = list(dict.fromkeys(variable_ids))

    # 1. autenticar al firmante ANTES de hacer trabajo
    with session_scope() as s:
        u = _check_signer(s, username, password, "Elaborado")
        proj = s.get(Project, project_id)
        if proj is None:
            raise ReportError("Proyecto no encontrado", 404)
        bad = [i for i in ids if (v := s.get(Variable, i)) is None or v.project_id != project_id]
        if bad:
            raise ReportError(f"Variables no pertenecen al proyecto: {bad}", 422)
        signer_name = u.display_name or u.username
        n_audit = s.scalar(select(func.count(WinccAudit.id)).where(
            WinccAudit.project_id == project_id, WinccAudit.t_ms >= start_ms, WinccAudit.t_ms < end_ms))
        wa = [{"t_ms": r.t_ms, "user": r.user_id, "object": r.object_id, "description": r.description,
               "comment": r.comment} for r in s.scalars(
            select(WinccAudit).where(WinccAudit.project_id == project_id, WinccAudit.t_ms >= start_ms,
                                     WinccAudit.t_ms < end_ms)
            .order_by(WinccAudit.t_ms.desc()).limit(cfg.max_wincc_audit_rows))]
        proj_meta = {"id": proj.id, "name": proj.name}

    # 2. congelar los datos
    results = [analyze_variable(i, start_ms, end_ms, step_s, include_series=True) for i in ids]
    rid = f"INF-{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"
    payload = _clean({
        "schema": 1, "report_id": rid, "project": proj_meta, "organization": cfg.organization,
        "title": (title or "").strip() or f"Informe de registros — {proj_meta['name']}",
        "comment": (comment or "").strip() or None, "start_ms": start_ms, "end_ms": end_ms, "step_s": step_s,
        "generated_ms": now_ms(), "generated_by": signer_name, "generated_user": username,
        "max_episode_rows": cfg.max_episode_rows, "results": results,
        "wincc_audit": wa, "wincc_audit_total": n_audit,
    })
    data_hash = canonical_hash(payload)
    payload["data_hash"] = data_hash
    d = reports_dir() / rid
    d.mkdir(parents=True, exist_ok=True)
    ppath = d / "datos.json.gz"
    with gzip.open(ppath, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))

    # 3. registrar, firmar y emitir
    with session_scope() as s:
        u = s.scalar(select(User).where(User.username == username))
        rep = Report(id=rid, project_id=project_id, title=payload["title"], comment=payload["comment"],
                     start_ms=start_ms, end_ms=end_ms, step_s=step_s, variable_ids=json.dumps(ids),
                     data_hash=data_hash, payload_path=str(ppath), status="elaborado", version=1,
                     created_ms=payload["generated_ms"], created_by=username)
        s.add(rep)
        sig = _signature_record(rid, u, "Elaborado", sign_comment, data_hash)
        s.add(sig)
        s.flush()
        f = _emit_pdf(s, rep, payload)
        audit.record("INFORME_GENERADO", username, rid,
                     {"proyecto": project_id, "variables": ids, "inicio_ms": start_ms, "fin_ms": end_ms,
                      "muestreo_s": step_s, "hash_datos": data_hash}, session=s)
        audit.record("FIRMA_ELECTRONICA", username, rid,
                     {"significado": "Elaborado", "version": 1, "registro": sig.record_hash, "pdf_sha256": f.sha256,
                      **_xlsx_audit(s, rid, 1)}, session=s)
        s.flush()
        return report_dict(s, rep)


# --------------------------------------------------------------------- firmar
def sign_report(report_id: str, *, meaning: str, username: str, password: str, comment: str | None) -> dict:
    if meaning not in ("Revisado", "Aprobado"):
        raise ReportError("Significado de firma no valido (Revisado o Aprobado)", 422)
    with session_scope() as s:
        rep = s.get(Report, report_id)
        if rep is None:
            raise ReportError("Informe no encontrado", 404)
        u = _check_signer(s, username, password, meaning)
        sigs = s.scalars(select(ReportSignature).where(ReportSignature.report_id == report_id)).all()
        done = {x.meaning for x in sigs}
        if meaning in done:
            raise ReportError(f"El informe ya tiene la firma '{meaning}'", 409)
        if any(x.username == username for x in sigs):
            raise ReportError("Un mismo usuario no puede firmar dos veces el mismo informe", 409)
        if meaning == "Aprobado" and "Revisado" not in done:
            raise ReportError("El informe debe estar revisado antes de aprobarse", 409)
        payload = _load_payload(rep.payload_path)
        if payload.get("data_hash") != rep.data_hash or canonical_hash(
                {k: v for k, v in payload.items() if k != "data_hash"}) != rep.data_hash:
            raise ReportError("Los datos congelados del informe no coinciden con su hash: no se puede firmar", 409)
        sig = _signature_record(report_id, u, meaning, comment, rep.data_hash)
        s.add(sig)
        rep.version += 1
        rep.status = MEANING_STATUS[meaning]
        s.flush()
        f = _emit_pdf(s, rep, payload)
        audit.record("FIRMA_ELECTRONICA", username, report_id,
                     {"significado": meaning, "version": rep.version, "registro": sig.record_hash,
                      "pdf_sha256": f.sha256, **_xlsx_audit(s, report_id, rep.version)}, session=s)
        s.flush()
        return report_dict(s, rep)


# -------------------------------------------------------------------- consulta
def report_dict(s, rep: Report) -> dict:
    sigs = s.scalars(select(ReportSignature).where(ReportSignature.report_id == rep.id)
                     .order_by(ReportSignature.signed_ms)).all()
    files = s.scalars(select(ReportFile).where(ReportFile.report_id == rep.id).order_by(ReportFile.version)).all()
    exports = s.scalars(select(ReportExport).where(ReportExport.report_id == rep.id)
                        .order_by(ReportExport.version)).all()
    done = {x.meaning for x in sigs}
    nxt = next((m for m in MEANINGS if m not in done), None)
    return {"id": rep.id, "project_id": rep.project_id, "title": rep.title, "comment": rep.comment,
            "start_ms": rep.start_ms, "end_ms": rep.end_ms, "step_s": rep.step_s,
            "variable_ids": json.loads(rep.variable_ids), "data_hash": rep.data_hash, "status": rep.status,
            "version": rep.version, "created_ms": rep.created_ms, "created_by": rep.created_by,
            "next_signature": nxt, "signatures": [_sig_dict(x) for x in sigs],
            "files": [{"version": f.version, "sha256": f.sha256, "created_ms": f.created_ms, "pades": f.pades}
                      for f in files],
            "exports": [{"version": e.version, "fmt": e.fmt, "sha256": e.sha256, "sig_sha256": e.sig_sha256,
                         "created_ms": e.created_ms} for e in exports]}


def _xlsx_audit(s, report_id: str, version: int) -> dict:
    e = s.scalar(select(ReportExport).where(ReportExport.report_id == report_id, ReportExport.version == version,
                                            ReportExport.fmt == "xlsx"))
    return {"xlsx_sha256": e.sha256, "p7s_sha256": e.sig_sha256} if e else {}


def list_reports(project_id: str | None, limit: int = 200) -> list[dict]:
    with session_scope() as s:
        q = select(Report).order_by(Report.created_ms.desc()).limit(limit)
        if project_id:
            q = q.where(Report.project_id == project_id)
        return [report_dict(s, r) for r in s.scalars(q)]


def get_report(report_id: str) -> dict:
    with session_scope() as s:
        rep = s.get(Report, report_id)
        if rep is None:
            raise ReportError("Informe no encontrado", 404)
        return report_dict(s, rep)


def pdf_path(report_id: str, version: int | None = None) -> tuple[Path, int]:
    with session_scope() as s:
        rep = s.get(Report, report_id)
        if rep is None:
            raise ReportError("Informe no encontrado", 404)
        v = rep.version if version is None else version
        f = s.scalar(select(ReportFile).where(ReportFile.report_id == report_id, ReportFile.version == v))
        if f is None or not Path(f.path).exists():
            raise ReportError("PDF no encontrado", 404)
        return Path(f.path), v


def xlsx_path(report_id: str, version: int | None = None, seal: bool = False,
              username: str | None = None) -> tuple[Path, int]:
    """Ruta del Excel (o de su sello .p7s) de una version.

    Los informes anteriores a la exportacion Excel no lo tienen: se genera en ese momento a partir de los
    MISMOS datos congelados y de las firmas vigentes en esa version, y queda en el audit trail."""
    with session_scope() as s:
        rep = s.get(Report, report_id)
        if rep is None:
            raise ReportError("Informe no encontrado", 404)
        v = rep.version if version is None else version
        if not 1 <= v <= rep.version:
            raise ReportError("Versión no encontrada", 404)
        e = s.scalar(select(ReportExport).where(ReportExport.report_id == report_id, ReportExport.version == v,
                                                ReportExport.fmt == "xlsx"))
        if e is None or not Path(e.path).exists():
            if e is not None:
                s.delete(e)
                s.flush()
            payload = _load_payload(rep.payload_path)
            if canonical_hash({k: x for k, x in payload.items() if k != "data_hash"}) != rep.data_hash:
                raise ReportError("Los datos congelados del informe no coinciden con su hash: no se genera el Excel",
                                  409)
            pdf = s.scalar(select(ReportFile).where(ReportFile.report_id == report_id, ReportFile.version == v))
            payload = dict(payload, pades=get_settings().report.pades)
            e = _emit_xlsx(s, rep, payload, v, _version_signatures(s, report_id, v), pdf.sha256 if pdf else None)
            audit.record("INFORME_EXCEL_GENERADO", username or "sistema", report_id,
                         {"version": v, "xlsx_sha256": e.sha256, "p7s_sha256": e.sig_sha256}, session=s)
        path = e.sig_path if seal else e.path
        if not path or not Path(path).exists():
            raise ReportError("Sello .p7s no encontrado (el sello digital está desactivado)" if seal
                              else "Excel no encontrado", 404)
        return Path(path), v


def verify_sha256(sha: str) -> dict:
    """¿Este fichero lo emitio el gestor? Compara su SHA-256 con todas las versiones registradas:
    PDF, Excel (.xlsx) o sello del Excel (.p7s)."""
    from .pades import verify_detached
    sha = sha.strip().lower()
    with session_scope() as s:
        f = s.scalar(select(ReportFile).where(ReportFile.sha256 == sha))
        e = None
        if f is not None:
            fmt, version, rid = "pdf", f.version, f.report_id
        else:
            e = s.scalar(select(ReportExport).where((ReportExport.sha256 == sha) | (ReportExport.sig_sha256 == sha)))
            if e is None:
                return {"found": False, "message": "Este fichero no coincide con ningún informe emitido por el gestor "
                                                   "(fue modificado o no proviene de este sistema)."}
            fmt = "p7s" if e.sig_sha256 == sha else e.fmt
            version, rid = e.version, e.report_id
        rep = s.get(Report, rid)
        data_ok = False
        try:
            p = _load_payload(rep.payload_path)
            data_ok = canonical_hash({k: v for k, v in p.items() if k != "data_hash"}) == rep.data_hash
        except (OSError, ValueError):
            pass
        out = {"found": True, "format": fmt, "version": version, "is_latest": version == rep.version,
               "data_intact": data_ok, "report": report_dict(s, rep)}
        if e is not None:   # Excel: comprobar ademas que el par .xlsx + .p7s guardado es valido
            seal = None
            try:
                if e.sig_path:
                    seal = verify_detached(Path(e.path).read_bytes(), Path(e.sig_path).read_bytes())
            except OSError:
                seal = {"valid": False}
            out["seal"] = seal
        label = {"pdf": "PDF", "xlsx": "Excel", "p7s": "Sello .p7s del Excel"}[fmt]
        out["message"] = (f"{label} auténtico: informe {rep.id}, versión {version}"
                          + ("" if version == rep.version else f" (existe una versión posterior v{rep.version})") + ".")
        return out
