"""API REST del gestor (prefijo /api)."""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from . import audit, auth, ingest, limits, reports
from .analytics import SAMPLING_PRESETS_S, analyze_variable
from .config import get_settings
from .db import session_scope
from .models import Alarm, Project, SourceFile, Variable, VariableLimits, WinccAudit
from .wincc_csv import dt_to_epoch_ms

router = APIRouter(prefix="/api")
COOKIE = "gestor_token"


# --------------------------------------------------------------- dependencias
def current_user(request: Request, gestor_token: str | None = Cookie(default=None)) -> dict:
    token = gestor_token
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        token = authz[7:]
    u = auth.user_from_token(token)
    if u is None:
        raise HTTPException(401, "Sesion no valida o expirada")
    return u


def require(role: str):
    def dep(user: dict = Depends(current_user)) -> dict:
        if not auth.has_role(user, role):
            raise HTTPException(403, f"Requiere rol '{role}' o superior")
        return user
    return dep


def parse_dt(text: str) -> int:
    try:
        return dt_to_epoch_ms(datetime.fromisoformat(text))
    except ValueError:
        raise HTTPException(422, f"Fecha invalida: {text} (use AAAA-MM-DDTHH:MM)")


# ---------------------------------------------------------------- autenticacion
class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(body: LoginIn, response: Response):
    try:
        token, user = auth.login(body.username.strip(), body.password)
    except auth.AuthError as e:
        raise HTTPException(401, str(e))
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                        max_age=get_settings().security.session_minutes * 60)
    return {"user": user}


@router.post("/auth/logout")
def logout(response: Response, gestor_token: str | None = Cookie(default=None)):
    if gestor_token:
        auth.logout(gestor_token)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/auth/me")
def me(user: dict = Depends(current_user)):
    return user


class ChangePwIn(BaseModel):
    old_password: str
    new_password: str


@router.post("/auth/change-password")
def change_password(body: ChangePwIn, user: dict = Depends(current_user)):
    try:
        auth.change_password(user["username"], body.old_password, body.new_password)
    except auth.AuthError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# -------------------------------------------------------------------- usuarios
@router.get("/users")
def users(_: dict = Depends(require("admin"))):
    return auth.list_users()


class UserCreateIn(BaseModel):
    username: str
    password: str
    role: str = "lector"
    source: str = "desarrollador"
    display_name: str | None = None


@router.post("/users")
def user_create(body: UserCreateIn, user: dict = Depends(require("admin"))):
    try:
        return auth.create_user(body.username.strip(), body.password, body.role, body.source, user["username"],
                                body.display_name)
    except auth.AuthError as e:
        raise HTTPException(400, str(e))


class UserPatchIn(BaseModel):
    role: str | None = None
    active: bool | None = None
    new_password: str | None = None
    unlock: bool = False
    display_name: str | None = None


@router.patch("/users/{user_id}")
def user_patch(user_id: int, body: UserPatchIn, user: dict = Depends(require("admin"))):
    if user_id == user["id"] and (body.active is False or (body.role and auth.ROLE_LEVEL[body.role] < 4)):
        raise HTTPException(400, "No puede desactivarse ni quitarse el rol de administrador a si mismo")
    try:
        return auth.update_user(user_id, user["username"], **body.model_dump())
    except auth.AuthError as e:
        raise HTTPException(400, str(e))


@router.post("/users/sync-wincc")
def users_sync(user: dict = Depends(require("admin"))):
    return auth.sync_wincc_users()


# ------------------------------------------------------------ proyectos/variables
@router.get("/meta")
def meta(_: dict = Depends(current_user)):
    return {"sampling_presets_s": SAMPLING_PRESETS_S, "default_step_s": 60,
            "retention_days": get_settings().retention_days}


@router.get("/projects")
def projects(_: dict = Depends(current_user)):
    with session_scope() as s:
        out = []
        for p in s.scalars(select(Project).order_by(Project.id)):
            n_vars, t0, t1 = s.execute(select(func.count(Variable.id), func.min(Variable.first_t_ms),
                                              func.max(Variable.last_t_ms)).where(Variable.project_id == p.id)).one()
            out.append({"id": p.id, "name": p.name, "root": p.root, "variables": n_vars,
                        "first_t_ms": t0, "last_t_ms": t1})
        return out


@router.get("/projects/{project_id}/variables")
def variables(project_id: str, _: dict = Depends(current_user)):
    with session_scope() as s:
        rows = s.execute(select(Variable, VariableLimits).outerjoin(VariableLimits)
                         .where(Variable.project_id == project_id)
                         .order_by(Variable.category, Variable.group_name, Variable.var_name)).all()
        return [{"id": v.id, "var_name": v.var_name, "group": v.group_name, "category": v.category,
                 "alias": v.alias, "unit": v.unit, "tag": v.tag, "enabled": v.enabled,
                 "first_t_ms": v.first_t_ms, "last_t_ms": v.last_t_ms, "limits": limits.limits_dict(l)}
                for v, l in rows]


class VariablePatchIn(BaseModel):
    alias: str | None = None
    unit: str | None = None
    tag: str | None = None
    enabled: bool | None = None


@router.patch("/variables/{variable_id}")
def variable_patch(variable_id: int, body: VariablePatchIn, user: dict = Depends(require("supervisor"))):
    data = body.model_dump(exclude_unset=True)
    with session_scope() as s:
        v = s.get(Variable, variable_id)
        if v is None:
            raise HTTPException(404, "Variable no encontrada")
        before = {k: getattr(v, k) for k in data}
        for k, val in data.items():
            setattr(v, k, (val.strip() or None) if isinstance(val, str) else val)
        audit.record("VARIABLE_MODIFICADA", user["username"], f"{v.project_id}:{v.var_name}",
                     {"antes": before, "despues": {k: getattr(v, k) for k in data}}, session=s)
        pid = v.project_id
    limits.backup_limits(pid)
    return {"ok": True}


@router.get("/variables/{variable_id}/limits/suggest")
def limits_suggest(variable_id: int, _: dict = Depends(current_user)):
    return limits.suggest_limits(variable_id)


class LimitsIn(BaseModel):
    ll: float | None = None
    l: float | None = None
    h: float | None = None
    hh: float | None = None
    range_min: float | None = None
    range_max: float | None = None
    note: str | None = None
    source: str = "manual"


@router.put("/variables/{variable_id}/limits")
def limits_put(variable_id: int, body: LimitsIn, user: dict = Depends(require("supervisor"))):
    try:
        return limits.set_limits(variable_id, body.model_dump(), user["username"], source=body.source)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except KeyError:
        raise HTTPException(404, "Variable no encontrada")


@router.get("/projects/{project_id}/alarm-tags")
def alarm_tags(project_id: str, _: dict = Depends(current_user)):
    return limits.list_alarm_tags(project_id)


# -------------------------------------------------------------------- analisis
class AnalysisIn(BaseModel):
    variable_ids: list[int] = Field(min_length=1, max_length=60)
    start: str
    end: str
    step_s: int = Field(default=60, ge=1, le=7 * 86400)
    include_series: bool = True


@router.post("/analysis")
def analysis(body: AnalysisIn, user: dict = Depends(current_user)):
    start, end = parse_dt(body.start), parse_dt(body.end)
    if end <= start:
        raise HTTPException(422, "La fecha final debe ser posterior a la inicial")
    results = []
    for vid in body.variable_ids:
        try:
            results.append(analyze_variable(vid, start, end, body.step_s, body.include_series))
        except KeyError:
            raise HTTPException(404, f"Variable {vid} no encontrada")
    audit.record("CONSULTA_ANALISIS", user["username"], None,
                 {"variables": body.variable_ids, "inicio": body.start, "fin": body.end, "muestreo_s": body.step_s})
    return {"start_ms": start, "end_ms": end, "step_s": body.step_s, "results": results}


# --------------------------------------------------------------------- informes
def _rep_call(fn, *a, **k):
    try:
        return fn(*a, **k)
    except reports.ReportError as e:
        raise HTTPException(e.code, str(e))


class ReportIn(BaseModel):
    project_id: str
    variable_ids: list[int] = Field(min_length=1, max_length=60)
    start: str
    end: str
    step_s: int = Field(default=60, ge=1, le=7 * 86400)
    title: str | None = Field(default=None, max_length=300)
    comment: str | None = Field(default=None, max_length=2000)
    password: str
    sign_comment: str | None = Field(default=None, max_length=500)


@router.post("/reports")
def report_create(body: ReportIn, user: dict = Depends(require("operador"))):
    """Solo bajo demanda (boton 'Generar reporte'): congela datos, firma 'Elaborado' y emite el PDF."""
    return _rep_call(reports.create_report, project_id=body.project_id, variable_ids=body.variable_ids,
                     start_ms=parse_dt(body.start), end_ms=parse_dt(body.end), step_s=body.step_s, title=body.title,
                     comment=body.comment, username=user["username"], password=body.password,
                     sign_comment=body.sign_comment)


@router.get("/reports")
def report_list(project_id: str | None = None, _: dict = Depends(current_user)):
    return reports.list_reports(project_id)


@router.get("/reports/verify")
def report_verify(sha256: str, user: dict = Depends(current_user)):
    res = reports.verify_sha256(sha256)
    audit.record("INFORME_VERIFICADO", user["username"], res["report"]["id"] if res["found"] else None,
                 {"sha256": sha256, "encontrado": res["found"]})
    return res


@router.get("/reports/certificate")
def report_certificate(_: dict = Depends(current_user)):
    from .pades import certificate_pem
    return Response(certificate_pem(), media_type="application/x-pem-file",
                    headers={"Content-Disposition": 'attachment; filename="gestor_sello.pem"'})


@router.get("/reports/{report_id}")
def report_get(report_id: str, _: dict = Depends(current_user)):
    return _rep_call(reports.get_report, report_id)


@router.get("/reports/{report_id}/pdf")
def report_pdf(report_id: str, version: int | None = None, user: dict = Depends(current_user)):
    path, v = _rep_call(reports.pdf_path, report_id, version)
    audit.record("INFORME_DESCARGADO", user["username"], report_id, {"version": v, "formato": "pdf"})
    return FileResponse(path, media_type="application/pdf", filename=path.name, content_disposition_type="inline")


XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/reports/{report_id}/xlsx")
def report_xlsx(report_id: str, version: int | None = None, user: dict = Depends(current_user)):
    path, v = _rep_call(reports.xlsx_path, report_id, version, username=user["username"])
    audit.record("INFORME_DESCARGADO", user["username"], report_id, {"version": v, "formato": "xlsx"})
    return FileResponse(path, media_type=XLSX_MEDIA, filename=path.name)


@router.get("/reports/{report_id}/p7s")
def report_p7s(report_id: str, version: int | None = None, user: dict = Depends(current_user)):
    path, v = _rep_call(reports.xlsx_path, report_id, version, seal=True, username=user["username"])
    audit.record("INFORME_DESCARGADO", user["username"], report_id, {"version": v, "formato": "p7s"})
    return FileResponse(path, media_type="application/pkcs7-signature", filename=path.name)


class SignIn(BaseModel):
    meaning: str
    password: str
    comment: str | None = Field(default=None, max_length=500)


@router.post("/reports/{report_id}/sign")
def report_sign(report_id: str, body: SignIn, user: dict = Depends(require("supervisor"))):
    return _rep_call(reports.sign_report, report_id, meaning=body.meaning, username=user["username"],
                     password=body.password, comment=body.comment)


@router.get("/projects/{project_id}/alarms")
def alarms(project_id: str, start: str, end: str, only_active: bool = False, limit: int = 2000,
           _: dict = Depends(current_user)):
    a, b = parse_dt(start), parse_dt(end)
    with session_scope() as s:
        q = select(Alarm).where(Alarm.project_id == project_id, Alarm.t_ms >= a, Alarm.t_ms < b)
        if only_active:
            q = q.where(Alarm.state_after == 1)
        rows = s.scalars(q.order_by(Alarm.t_ms.desc()).limit(min(limit, 20000))).all()
        return [{"t_ms": r.t_ms, "group": r.group_name, "msg_number": r.msg_number, "msg_class": r.msg_class,
                 "state_after": r.state_after, "text": r.text, "plc": r.plc,
                 "params": json.loads(r.params) if r.params else []} for r in rows]


@router.get("/projects/{project_id}/wincc-audit")
def wincc_audit(project_id: str, start: str, end: str, limit: int = 2000, _: dict = Depends(current_user)):
    a, b = parse_dt(start), parse_dt(end)
    with session_scope() as s:
        rows = s.scalars(select(WinccAudit).where(WinccAudit.project_id == project_id, WinccAudit.t_ms >= a,
                                                  WinccAudit.t_ms < b)
                         .order_by(WinccAudit.t_ms.desc()).limit(min(limit, 20000))).all()
        return [{"t_ms": r.t_ms, "record_id": r.record_id, "delta_utc": r.delta_utc, "user": r.user_id,
                 "object": r.object_id, "description": r.description, "comment": r.comment} for r in rows]


# ---------------------------------------------------------------------- ingesta
@router.get("/ingest/status")
def ingest_status(_: dict = Depends(current_user)):
    st = ingest.last_status()
    with session_scope() as s:
        st["files"] = [{"project_id": f.project_id, "rel_path": f.rel_path, "category": f.category,
                        "loaded_t_ms": f.loaded_t_ms, "rt_count": f.rt_count, "last_load_ms": f.last_load_ms}
                       for f in s.scalars(select(SourceFile).order_by(SourceFile.project_id, SourceFile.rel_path))]
    return st


@router.post("/ingest/run")
def ingest_run(user: dict = Depends(require("admin"))):
    res = ingest.run_ingest(reason=f"manual:{user['username']}")
    audit.record("INGESTA_MANUAL", user["username"], None, {"estado": res.get("status")})
    return res


@router.get("/audit/app")
def app_audit(limit: int = 500, _: dict = Depends(require("supervisor"))):
    from .models import AppAudit
    with session_scope() as s:
        rows = s.scalars(select(AppAudit).order_by(AppAudit.id.desc()).limit(min(limit, 5000))).all()
        return {"chain": audit.verify_chain(),
                "records": [{"id": r.id, "ts_ms": r.ts_ms, "user": r.username, "action": r.action,
                             "entity": r.entity, "details": json.loads(r.details) if r.details else None}
                            for r in rows]}
