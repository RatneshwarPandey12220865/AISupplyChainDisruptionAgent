"""Authentication, authorisation, webhook verification and rate limiting.

* Dashboard / approver API: either an API key (`X-API-Key: <key>` or `Authorization: Bearer <key>`; keys come from
  API_KEYS as `name:key:role`) or a login session token from POST /api/auth/login (`Authorization: Bearer scs_...`).
  The key *name* / the *username* becomes the audited identity (e.g. the reviewer on an approval).
  Roles are ordered viewer < approver < admin. Admins create user accounts; the bootstrap admin is an API key.
* Ingestion webhooks: HMAC-SHA256 signature over the raw body (`X-Signature: sha256=<hex>`), optionally bound to a
  timestamp (`X-Timestamp`, signed as `"<ts>.<body>"`) to stop replays, or the plain secret in `X-Webhook-Secret`.
* With no API_KEYS / WEBHOOK_SECRET configured and ENVIRONMENT=development everything is open (local convenience);
  production refuses to start in that state (see Settings.validate_for_runtime).
"""
import hashlib
import hmac
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from app.config import settings
from app.metrics import RATE_LIMITED, AUTH_FAILURES

log = logging.getLogger(__name__)
ROLE_RANK = {"viewer": 1, "approver": 2, "admin": 3}


@dataclass(frozen=True)
class Principal:
    name: str
    role: str
    kind: str = "api_key"  # api_key | session | dev
    token_hash: str | None = None  # set for session logins (used by logout / change-password)

    def at_least(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]


DEV_PRINCIPAL = Principal("dev-user", "admin", kind="dev")


def _extract_key(request: Request) -> str:
    key = request.headers.get("x-api-key", "")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    return key


def lookup_key(key: str) -> Principal | None:
    found: Principal | None = None
    for candidate, (name, role) in settings.parsed_api_keys.items():  # no early exit: constant-ish time
        if key and hmac.compare_digest(candidate.encode(), key.encode()):
            found = Principal(name, role)
    return found


def lookup_session_token(token: str) -> Principal | None:
    from app.db import session_scope
    from app.users import lookup_session

    with session_scope() as s:
        found = lookup_session(s, token)
    return Principal(found[0].username, found[0].role, kind="session", token_hash=found[1]) if found else None


def authenticate(request: Request) -> Principal:
    if not settings.auth_enabled:
        return DEV_PRINCIPAL
    from app.users import SESSION_PREFIX

    credential = _extract_key(request)
    if credential.startswith(SESSION_PREFIX):  # a username/password login session
        principal = lookup_session_token(credential)
        if principal is None:
            AUTH_FAILURES.labels("session").inc()
            raise HTTPException(401, "Session expired or invalid; please sign in again",
                                headers={"WWW-Authenticate": "Bearer"})
        request.state.principal = principal
        return principal
    principal = lookup_key(credential)
    if principal is None:
        AUTH_FAILURES.labels("api_key").inc()
        raise HTTPException(401, "Missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})
    request.state.principal = principal
    return principal


def require(role: str):
    """Dependency factory: `Depends(require("approver"))` returns the authenticated Principal."""
    def dep(principal: Principal = Depends(authenticate)) -> Principal:
        if not principal.at_least(role):
            raise HTTPException(403, f"Requires role '{role}'")
        return principal
    return dep


# ----------------------------------------------------------------------------------------------------------
# Rate limiting (fixed window; per principal or client IP). In-memory per process: with several API replicas
# put a limiter in your reverse proxy / gateway too — the defaults here are a safety net, not a quota system.
# ----------------------------------------------------------------------------------------------------------
class RateLimiter:
    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(self.hits) > 50_000:  # bound memory under key-spraying
            self.hits.clear()
        return True


    # Failure-only counting (used for login throttling): check first, record only when the attempt failed.
    def blocked(self, key: str, limit: int, window: float = 300.0) -> bool:
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        return len(q) >= limit

    def hit(self, key: str) -> None:
        self.hits[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        self.hits.pop(key, None)


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def client_id(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    if principal:
        return f"user:{principal.name}"
    return "ip:" + client_ip(request)


def rate_limit(scope: str, per_minute_attr: str):
    async def dep(request: Request) -> None:
        limit = getattr(settings, per_minute_attr)
        if limit and not limiter.allow(f"{scope}:{client_id(request)}", limit):
            RATE_LIMITED.labels(scope).inc()
            raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": "60"})
    return dep


api_rate_limit = rate_limit("api", "rate_limit_per_minute")
ingest_rate_limit = rate_limit("ingest", "ingest_rate_limit_per_minute")


# ----------------------------------------------------------------------------------------------------------
# Webhook verification
# ----------------------------------------------------------------------------------------------------------
def sign_body(secret: str, body: bytes, timestamp: str | None = None) -> str:
    msg = (timestamp.encode() + b"." + body) if timestamp else body
    return "sha256=" + hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


async def verify_webhook(request: Request) -> None:
    secret = settings.webhook_secret
    if not secret:
        if settings.is_production:  # unreachable via startup validation; defence in depth
            raise HTTPException(503, "Webhook secret not configured")
        return
    # Dashboard operators with an approver key may also post events (handy for manual testing).
    key = _extract_key(request)
    if key and (p := lookup_key(key)) and p.at_least("approver"):
        request.state.principal = p
        return

    body = await request.body()
    signature = request.headers.get("x-signature", "")
    if signature:
        ts = request.headers.get("x-timestamp")
        if ts:
            try:
                skew = abs(time.time() - float(ts))
            except ValueError:
                raise HTTPException(401, "Bad X-Timestamp") from None
            if skew > settings.webhook_max_skew_seconds:
                AUTH_FAILURES.labels("webhook_stale").inc()
                raise HTTPException(401, "Stale webhook timestamp")
        if hmac.compare_digest(signature.encode(), sign_body(secret, body, ts).encode()):
            return
    elif settings.webhook_allow_plain_secret:
        plain = request.headers.get("x-webhook-secret", "")
        if plain and hmac.compare_digest(plain.encode(), secret.encode()):
            return
    AUTH_FAILURES.labels("webhook").inc()
    raise HTTPException(401, "Invalid webhook signature")
