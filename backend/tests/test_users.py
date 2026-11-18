"""Username/password accounts: hashing, login, sessions, RBAC, admin management, throttling, bootstrap via API key."""
from datetime import timedelta

from sqlalchemy import select

from app.config import settings
from app.db import session_scope
from app.models import User, UserSession, utcnow
from app.users import hash_password, verify_password
from app.worker import process_event
from tests.conftest import auth

PW = "correct-horse-battery"


def make_user(client, username="alice", role="approver", password=PW):
    r = client.post("/api/users", json={"username": username, "password": password, "role": role},
                    headers=auth("admin"))
    assert r.status_code == 201, r.text
    return r.json()


def login(client, username="alice", password=PW):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_password_hashing():
    h = hash_password("s3cret-pass-word")
    assert h.startswith("scrypt$") and "s3cret" not in h
    assert verify_password("s3cret-pass-word", h)
    assert not verify_password("wrong", h)
    assert hash_password("s3cret-pass-word") != h  # unique salt
    assert not verify_password("x", "garbage")


def test_bootstrap_admin_key_creates_user_who_can_sign_in(client, secured):
    assert client.post("/api/users", json={"username": "alice", "password": PW, "role": "approver"}).status_code == 401
    user = make_user(client)
    assert user["username"] == "alice" and "password" not in str(user)
    r = login(client)
    assert r.status_code == 200
    token = r.json()["token"]
    assert token.startswith("scs_") and r.json()["user"] == {"username": "alice", "role": "approver"}
    me = client.get("/api/me", headers=bearer(token)).json()
    assert me["name"] == "alice" and me["role"] == "approver" and me["auth_method"] == "session"


def test_token_is_not_stored_in_clear(client, secured):
    make_user(client)
    token = login(client).json()["token"]
    with session_scope() as s:
        assert all(token not in row.token_hash for row in s.scalars(select(UserSession)))
        assert s.scalar(select(User)).password_hash.startswith("scrypt$")


def test_wrong_password_and_unknown_user_look_identical(client, secured):
    make_user(client)
    a = login(client, password="nope-nope-nope")
    b = login(client, username="ghost")
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


def test_session_roles_are_enforced(client, secured, bus):
    make_user(client, "viv", "viewer")
    make_user(client, "alice", "approver")
    viewer, approver = login(client, "viv").json()["token"], login(client, "alice").json()["token"]
    assert client.get("/api/disruptions", headers=bearer(viewer)).status_code == 200
    assert client.post("/api/simulate/low_stock", headers=bearer(viewer)).status_code == 403
    assert client.post("/api/simulate/low_stock", headers=bearer(approver)).status_code == 200
    assert client.get("/api/users", headers=bearer(approver)).status_code == 403  # approver cannot manage users
    assert client.post("/api/admin/reset", headers=bearer(approver)).status_code == 403


def test_reviewer_recorded_is_the_username(client, secured, bus):
    make_user(client, "alice", "approver")
    token = login(client).json()["token"]
    client.post("/api/ingest/shipment/eta", json={"shipment_code": "SHP-1002", "delay_days": 10},
                headers=auth("approver"))
    process_event(bus.events[-1])
    did = client.get("/api/disruptions", headers=auth("viewer")).json()[0]["id"]
    r = client.post(f"/api/disruptions/{did}/decision", json={"approved": True, "reviewer": "mallory"},
                    headers=bearer(token))
    assert r.status_code == 200
    assert client.get(f"/api/disruptions/{did}", headers=auth("viewer")).json()["decision"]["reviewer"] == "alice"


def test_logout_revokes_the_session(client, secured):
    make_user(client)
    token = login(client).json()["token"]
    assert client.post("/api/auth/logout", headers=bearer(token)).status_code == 200
    assert client.get("/api/me", headers=bearer(token)).status_code == 401


def test_expired_session_is_rejected(client, secured):
    make_user(client)
    token = login(client).json()["token"]
    with session_scope() as s:
        row = s.scalar(select(UserSession))
        row.expires_at = utcnow() - timedelta(minutes=1)
    assert client.get("/api/me", headers=bearer(token)).status_code == 401


def test_deactivating_a_user_kills_sessions_and_blocks_login(client, secured):
    uid = make_user(client)["id"]
    token = login(client).json()["token"]
    assert client.patch(f"/api/users/{uid}", json={"is_active": False}, headers=auth("admin")).status_code == 200
    assert client.get("/api/me", headers=bearer(token)).status_code == 401
    assert login(client).status_code == 401
    client.patch(f"/api/users/{uid}", json={"is_active": True}, headers=auth("admin"))
    assert login(client).status_code == 200


def test_role_change_applies_immediately(client, secured):
    uid = make_user(client, role="viewer")["id"]
    token = login(client).json()["token"]
    assert client.post("/api/simulate/low_stock", headers=bearer(token)).status_code == 403
    client.patch(f"/api/users/{uid}", json={"role": "approver"}, headers=auth("admin"))
    assert client.post("/api/simulate/low_stock", headers=bearer(token)).status_code == 200


def test_admin_password_reset_signs_user_out(client, secured):
    uid = make_user(client)["id"]
    old = login(client).json()["token"]
    assert client.patch(f"/api/users/{uid}", json={"password": "a-brand-new-password"},
                        headers=auth("admin")).status_code == 200
    assert client.get("/api/me", headers=bearer(old)).status_code == 401
    assert login(client).status_code == 401
    assert login(client, password="a-brand-new-password").status_code == 200


def test_user_changes_own_password(client, secured):
    make_user(client)
    a = login(client).json()["token"]
    b = login(client).json()["token"]
    bad = client.post("/api/auth/password", json={"current_password": "wrong-wrong", "new_password": "another-long-pass"},
                      headers=bearer(a))
    assert bad.status_code == 403
    ok = client.post("/api/auth/password", json={"current_password": PW, "new_password": "another-long-pass"},
                     headers=bearer(a))
    assert ok.status_code == 200
    assert client.get("/api/me", headers=bearer(a)).status_code == 200  # current session kept
    assert client.get("/api/me", headers=bearer(b)).status_code == 401  # other sessions revoked
    assert login(client, password="another-long-pass").status_code == 200


def test_api_key_principal_cannot_change_password(client, secured):
    r = client.post("/api/auth/password", json={"current_password": "x" * 12, "new_password": "y" * 12},
                    headers=auth("admin"))
    assert r.status_code == 400


def test_validation(client, secured):
    h = auth("admin")
    assert client.post("/api/users", json={"username": "bob", "password": "short"}, headers=h).status_code == 400
    assert client.post("/api/users", json={"username": "B!", "password": PW}, headers=h).status_code == 400
    assert client.post("/api/users", json={"username": "bob", "password": PW, "role": "root"}, headers=h).status_code == 400
    make_user(client, "bob")
    assert client.post("/api/users", json={"username": "BOB", "password": PW}, headers=h).status_code == 409


def test_admin_cannot_demote_or_deactivate_self(client, secured):
    uid = make_user(client, "boss", "admin")["id"]
    token = login(client, "boss").json()["token"]
    assert client.patch(f"/api/users/{uid}", json={"role": "viewer"}, headers=bearer(token)).status_code == 400
    assert client.patch(f"/api/users/{uid}", json={"is_active": False}, headers=bearer(token)).status_code == 400
    assert client.patch(f"/api/users/{uid}", json={"password": "my-new-long-password"},
                        headers=bearer(token)).status_code == 200
    assert client.get("/api/me", headers=bearer(token)).status_code == 200  # own session survives


def test_login_is_throttled_per_username(client, secured, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures", 3)
    make_user(client)
    for _ in range(3):
        assert login(client, password="wrong-wrong-wrong").status_code == 401
    assert login(client).status_code == 429  # even the right password is refused while locked
    assert login(client, username="other-user").status_code == 401  # other usernames are unaffected


def test_successful_login_resets_the_failure_counter(client, secured, monkeypatch):
    monkeypatch.setattr(settings, "login_max_failures", 3)
    make_user(client)
    for _ in range(2):
        login(client, password="wrong-wrong-wrong")
    assert login(client).status_code == 200
    for _ in range(2):
        assert login(client, password="wrong-wrong-wrong").status_code == 401


def test_users_endpoint_hides_hashes(client, secured):
    make_user(client)
    body = client.get("/api/users", headers=auth("admin")).text
    assert "scrypt" not in body and "password" not in body
