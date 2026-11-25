"""Usuarios, contrasenas y sesiones.

Origenes de usuario:
  * 'wincc'         -> descubiertos en el AuditTrail de WinCC ("El usuario 'X' ha iniciado una
                       sesion con el grupo 'Y'"). WinCC no expone las contrasenas, asi que el
                       usuario se crea INACTIVO y un administrador lo activa con una contrasena
                       inicial (que el usuario cambia en su primer ingreso).
  * 'desarrollador' -> cuentas propias del equipo externo de desarrollo / soporte, creadas con
                       scripts\\crear_usuario.py. Quedan separadas y trazables en el audit trail.

Roles (de menor a mayor): lector < operador < supervisor < admin. 'desarrollador' = admin.
Las contrasenas se guardan con PBKDF2-SHA256 (biblioteca estandar, sin dependencias).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime

from sqlalchemy import delete, select

from . import audit
from .config import get_settings
from .db import session_scope
from .models import User, UserSession, WinccAudit
from .wincc_csv import dt_to_epoch_ms

ROLE_LEVEL = {"lector": 1, "operador": 2, "supervisor": 3, "admin": 4, "desarrollador": 4}
WINCC_GROUP_ROLE = {"administrador": "admin", "administradores": "admin", "supervisor": "supervisor",
                    "supervisores": "supervisor", "operador": "operador", "operadores": "operador"}
LOGIN_RE = re.compile(r"El usuario '?(?P<u>[^' ]+)'? ha iniciado una sesi.n con el grupo '(?P<g>[^']+)'", re.I)
PBKDF2_ITERS = 390_000
MIN_PASSWORD = 8


class AuthError(Exception):
    pass


def now_ms() -> int:
    return dt_to_epoch_ms(datetime.now())


# ------------------------------------------------------------------ hashing
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        algo, iters, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iters))
        return algo == "pbkdf2_sha256" and hmac.compare_digest(calc, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def _check_policy(password: str) -> None:
    if len(password) < MIN_PASSWORD or not re.search(r"\d", password) or not re.search(r"[A-Za-z]", password):
        raise AuthError(f"La contrasena debe tener al menos {MIN_PASSWORD} caracteres, letras y numeros")


def user_dict(u: User) -> dict:
    return {"id": u.id, "username": u.username, "display_name": u.display_name, "source": u.source,
            "wincc_group": u.wincc_group, "role": u.role, "active": u.active, "locked": u.locked,
            "must_change_password": u.must_change_password, "has_password": bool(u.password_hash),
            "last_login_ms": u.last_login_ms}


# -------------------------------------------------------- usuarios de WinCC
def sync_wincc_users() -> dict:
    """Crea (inactivos) los usuarios de WinCC vistos en el AuditTrail y actualiza su grupo."""
    created, updated = [], []
    with session_scope() as s:
        found: dict[str, str] = {}
        for (desc,) in s.execute(select(WinccAudit.description).where(WinccAudit.description.like("%iniciado una sesi%"))):
            m = LOGIN_RE.search(desc or "")
            if m:
                found[m.group("u")] = m.group("g")
        existing = {u.username.lower(): u for u in s.scalars(select(User))}
        for name, group in found.items():
            u = existing.get(name.lower())
            if u is None:
                u = User(username=name, display_name=name, source="wincc", wincc_group=group,
                         role=WINCC_GROUP_ROLE.get(group.lower(), "lector"), active=False,
                         must_change_password=True, created_ms=now_ms())
                s.add(u)
                created.append(name)
            elif u.source == "wincc" and u.wincc_group != group:
                u.wincc_group = group
                updated.append(name)
        if created or updated:
            audit.record("USUARIOS_WINCC_SINCRONIZADOS", "sistema", None,
                         {"creados": created, "grupo_actualizado": updated}, session=s)
    return {"created": created, "updated": updated}


# ---------------------------------------------------------------- gestion
def create_user(username: str, password: str | None, role: str, source: str, actor: str,
                display_name: str | None = None, active: bool = True, must_change: bool = True) -> dict:
    if role not in ROLE_LEVEL:
        raise AuthError(f"Rol invalido: {role}")
    if source not in ("wincc", "desarrollador"):
        raise AuthError("Origen invalido")
    if password:
        _check_policy(password)
    with session_scope() as s:
        if s.scalar(select(User).where(User.username == username)):
            raise AuthError("El usuario ya existe")
        u = User(username=username, display_name=display_name or username, source=source, role=role,
                 password_hash=hash_password(password) if password else None, active=active and bool(password),
                 must_change_password=must_change, created_ms=now_ms())
        s.add(u)
        s.flush()
        audit.record("USUARIO_CREADO", actor, username, {"rol": role, "origen": source}, session=s)
        return user_dict(u)


def update_user(user_id: int, actor: str, role: str | None = None, active: bool | None = None,
                new_password: str | None = None, unlock: bool = False, display_name: str | None = None) -> dict:
    with session_scope() as s:
        u = s.get(User, user_id)
        if u is None:
            raise AuthError("Usuario no encontrado")
        changes = {}
        if role is not None:
            if role not in ROLE_LEVEL:
                raise AuthError(f"Rol invalido: {role}")
            changes["rol"] = [u.role, role]
            u.role = role
        if display_name is not None:
            u.display_name = display_name
        if new_password:
            _check_policy(new_password)
            u.password_hash = hash_password(new_password)
            u.must_change_password = True
            changes["contrasena"] = "restablecida"
        if active is not None:
            if active and not u.password_hash:
                raise AuthError("Asigne una contrasena inicial antes de activar el usuario")
            changes["activo"] = [u.active, active]
            u.active = active
        if unlock:
            u.locked, u.failed_logins = False, 0
            changes["desbloqueado"] = True
        audit.record("USUARIO_MODIFICADO", actor, u.username, changes, session=s)
        if active is False or new_password:
            s.execute(delete(UserSession).where(UserSession.user_id == u.id))
        return user_dict(u)


def list_users() -> list[dict]:
    with session_scope() as s:
        return [user_dict(u) for u in s.scalars(select(User).order_by(User.source, User.username))]


# ----------------------------------------------------------------- sesiones
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_credentials(s, username: str, password: str, purpose: str) -> User:
    """Valida usuario/contrasena aplicando bloqueo por intentos fallidos. Lanza AuthError."""
    cfg = get_settings().security
    u = s.scalar(select(User).where(User.username == username))
    if u is None or not u.active:
        audit.record(f"{purpose}_FALLIDO", username, None, {"motivo": "usuario inexistente o inactivo"}, session=s)
        raise AuthError("Usuario o contrasena incorrectos")
    if u.locked:
        audit.record(f"{purpose}_FALLIDO", username, None, {"motivo": "bloqueado"}, session=s)
        raise AuthError("Usuario bloqueado. Contacte a un administrador")
    if not check_password(password, u.password_hash):
        u.failed_logins += 1
        if u.failed_logins >= cfg.max_failed_logins:
            u.locked = True
        audit.record(f"{purpose}_FALLIDO", username, None,
                     {"intentos": u.failed_logins, "bloqueado": u.locked}, session=s)
        raise AuthError("Usuario o contrasena incorrectos")
    u.failed_logins = 0
    return u


def login(username: str, password: str) -> tuple[str, dict]:
    cfg = get_settings().security
    with session_scope() as s:
        try:
            u = verify_credentials(s, username, password, "LOGIN")
        except AuthError:
            s.commit()  # persistir contador de intentos y audit
            raise
        token = secrets.token_urlsafe(32)
        t = now_ms()
        s.add(UserSession(token_hash=_token_hash(token), user_id=u.id, created_ms=t,
                          expires_ms=t + cfg.session_minutes * 60_000))
        s.execute(delete(UserSession).where(UserSession.expires_ms < t))
        u.last_login_ms = t
        audit.record("LOGIN", u.username, None, None, session=s)
        return token, user_dict(u)


def logout(token: str) -> None:
    with session_scope() as s:
        sess = s.get(UserSession, _token_hash(token))
        if sess:
            u = s.get(User, sess.user_id)
            s.delete(sess)
            audit.record("LOGOUT", u.username if u else None, None, None, session=s)


def user_from_token(token: str | None) -> dict | None:
    if not token:
        return None
    with session_scope() as s:
        sess = s.get(UserSession, _token_hash(token))
        if sess is None or sess.expires_ms < now_ms():
            return None
        u = s.get(User, sess.user_id)
        if u is None or not u.active or u.locked:
            return None
        return user_dict(u)


def change_password(username: str, old: str, new: str) -> None:
    _check_policy(new)
    if old == new:
        raise AuthError("La nueva contrasena debe ser distinta")
    with session_scope() as s:
        try:
            u = verify_credentials(s, username, old, "CAMBIO_CONTRASENA")
        except AuthError:
            s.commit()
            raise
        u.password_hash = hash_password(new)
        u.must_change_password = False
        audit.record("CONTRASENA_CAMBIADA", username, None, None, session=s)


def has_role(user: dict | None, role: str) -> bool:
    return bool(user) and ROLE_LEVEL.get(user["role"], 0) >= ROLE_LEVEL[role]
