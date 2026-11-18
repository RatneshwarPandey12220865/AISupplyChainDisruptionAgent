"""Username/password login, logout, password change and (admin-only) user management.

There is deliberately no public sign-up: this tool approves purchase orders, so accounts are created by an admin.
The first admin is bootstrapped with an admin API key from API_KEYS (sign in with it once and create real accounts).
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session, session_scope
from app.metrics import AUTH_FAILURES
from app.models import User
from app.security import Principal, api_rate_limit, client_ip, limiter, require
from app.users import (ROLES, authenticate_password, create_session, create_user, hash_password, revoke_session,
                       revoke_user_sessions, validate_password, verify_password)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

LOGIN_WINDOW = 300.0
LOGIN_MAX_PER_IP = 40


def _iso(dt):
    return dt.isoformat() + "Z" if dt else None


def user_dict(u: User) -> dict:
    return {"id": u.id, "username": u.username, "role": u.role, "is_active": u.is_active,
            "created_by": u.created_by, "created_at": _iso(u.created_at), "last_login_at": _iso(u.last_login_at)}


# ---------------------------------------------------------------------------------------------------- login
class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


@router.post("/auth/login")
def login(body: LoginBody, request: Request):
    username = body.username.strip().lower()
    ip_key, user_key = f"login-ip:{client_ip(request)}", f"login-user:{username}"
    if limiter.blocked(ip_key, LOGIN_MAX_PER_IP, LOGIN_WINDOW) or \
            limiter.blocked(user_key, settings.login_max_failures, LOGIN_WINDOW):
        AUTH_FAILURES.labels("login_locked").inc()
        raise HTTPException(429, "Too many failed sign-in attempts. Try again in a few minutes.",
                            headers={"Retry-After": "300"})
    with session_scope() as s:
        user = authenticate_password(s, username, body.password)
        if user is None:
            limiter.hit(ip_key)
            limiter.hit(user_key)
            AUTH_FAILURES.labels("login").inc()
            log.warning("Failed login for %r from %s", username[:64], client_ip(request))
            # Same message whether the user is unknown, disabled or the password is wrong.
            raise HTTPException(401, "Invalid username or password")
        token, expires = create_session(s, user)
        result = {"token": token, "expires_at": _iso(expires), "user": {"username": user.username, "role": user.role}}
    limiter.reset(user_key)
    return result


@router.post("/auth/logout")
def logout(principal: Principal = Depends(require("viewer"))):
    if principal.token_hash:
        with session_scope() as s:
            revoke_session(s, principal.token_hash)
    return {"ok": True}


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


@router.post("/auth/password", dependencies=[Depends(api_rate_limit)])
def change_password(body: PasswordChange, principal: Principal = Depends(require("viewer"))):
    if principal.kind != "session":
        raise HTTPException(400, "Only accounts that signed in with a username can change a password")
    try:
        validate_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    with session_scope() as s:
        user = s.scalar(select(User).where(User.username == principal.name))
        if user is None or not verify_password(body.current_password, user.password_hash):
            raise HTTPException(403, "Current password is incorrect")
        user.password_hash = hash_password(body.new_password)
        revoke_user_sessions(s, user.id, keep_hash=principal.token_hash)  # sign out everywhere else
    return {"ok": True}


# ----------------------------------------------------------------------------------------- user management
admin = APIRouter(prefix="/api/users", dependencies=[Depends(require("admin")), Depends(api_rate_limit)])


class NewUser(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    role: str = "viewer"


class UserUpdate(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, max_length=256)


@admin.get("")
def list_users(s: Session = Depends(get_session)):
    return [user_dict(u) for u in s.scalars(select(User).order_by(User.username))]


@admin.post("", status_code=201)
def add_user(body: NewUser, principal: Principal = Depends(require("admin")), s: Session = Depends(get_session)):
    try:
        user = create_user(s, body.username, body.password, body.role, created_by=principal.name)
    except ValueError as exc:
        raise HTTPException(409 if "taken" in str(exc) else 400, str(exc)) from None
    log.info("User %s created by %s (role=%s)", user.username, principal.name, user.role)
    return user_dict(user)


@admin.patch("/{user_id}")
def update_user(user_id: int, body: UserUpdate, principal: Principal = Depends(require("admin")),
                s: Session = Depends(get_session)):
    user = s.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    is_self = principal.kind == "session" and principal.name == user.username
    if is_self and (body.is_active is False or (body.role and body.role != user.role)):
        raise HTTPException(400, "You cannot change your own role or deactivate yourself")
    if body.role is not None:
        if body.role not in ROLES:
            raise HTTPException(400, f"Role must be one of: {', '.join(ROLES)}")
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
        if not body.is_active:
            revoke_user_sessions(s, user.id)
    if body.password is not None:
        try:
            validate_password(body.password)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        user.password_hash = hash_password(body.password)
        revoke_user_sessions(s, user.id, keep_hash=principal.token_hash if is_self else None)
    log.info("User %s updated by %s", user.username, principal.name)
    return user_dict(user)
