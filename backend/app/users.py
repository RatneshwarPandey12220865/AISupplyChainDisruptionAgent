"""User accounts, password hashing and dashboard login sessions.

* Passwords: scrypt (stdlib, memory-hard) with a per-user random salt; the stored string carries its own parameters so
  they can be raised later without breaking old hashes.
* Sessions: an opaque random token (`scs_...`) handed to the browser; only its SHA-256 is stored server-side, so sessions
  can be revoked (logout, disable user, password change) and a database leak cannot be replayed.
"""
import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User, UserSession, utcnow

SESSION_PREFIX = "scs_"
ROLES = ("viewer", "approver", "admin")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
_N, _R, _P = 2**14, 8, 1  # ~16 MB / ~50 ms per hash


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, expected = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(digest, base64.b64decode(expected))
    except Exception:
        return False


# Verified against when the username does not exist, so "unknown user" and "wrong password" take the same time.
_DUMMY_HASH = hash_password("not-a-real-password")


def normalize_username(raw: str) -> str:
    name = (raw or "").strip().lower()
    if not USERNAME_RE.match(name):
        raise ValueError("Username must be 3-32 characters: lowercase letters, digits, '.', '_' or '-'")
    return name


def validate_password(password: str) -> None:
    if len(password) < settings.password_min_length:
        raise ValueError(f"Password must be at least {settings.password_min_length} characters")
    if len(password) > 256:
        raise ValueError("Password is too long (max 256 characters)")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_user(s: Session, username: str, password: str, role: str, created_by: str) -> User:
    name = normalize_username(username)
    validate_password(password)
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
    if s.scalar(select(User).where(User.username == name)):
        raise ValueError("That username is already taken")
    user = User(username=name, password_hash=hash_password(password), role=role, is_active=True,
                created_by=created_by)
    s.add(user)
    s.flush()
    return user


def authenticate_password(s: Session, username: str, password: str) -> User | None:
    user = s.scalar(select(User).where(User.username == (username or "").strip().lower()))
    ok = verify_password(password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok or not user.is_active:
        return None
    user.last_login_at = utcnow()
    return user


def create_session(s: Session, user: User) -> tuple[str, datetime]:
    s.execute(delete(UserSession).where(UserSession.expires_at < utcnow()))  # opportunistic cleanup
    token = SESSION_PREFIX + secrets.token_urlsafe(32)
    expires = utcnow() + timedelta(hours=settings.session_ttl_hours)
    s.add(UserSession(token_hash=token_hash(token), user_id=user.id, expires_at=expires))
    return token, expires


def lookup_session(s: Session, token: str) -> tuple[User, str] | None:
    """Return (user, token_hash) for a live session of an active user, else None."""
    h = token_hash(token)
    row = s.scalar(select(UserSession).where(UserSession.token_hash == h))
    if row is None:
        return None
    if row.expires_at < utcnow():
        s.delete(row)
        return None
    user = s.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    return user, h


def revoke_session(s: Session, h: str) -> None:
    s.execute(delete(UserSession).where(UserSession.token_hash == h))


def revoke_user_sessions(s: Session, user_id: int, keep_hash: str | None = None) -> None:
    stmt = delete(UserSession).where(UserSession.user_id == user_id)
    if keep_hash:
        stmt = stmt.where(UserSession.token_hash != keep_hash)
    s.execute(stmt)
