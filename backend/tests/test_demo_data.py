"""The demo-data helper: sample users and a realistic spread of disruption outcomes."""
from sqlalchemy import select

from app import demo_data
from app.db import session_scope
from app.models import Disruption, User
from app.users import verify_password


def test_demo_users_cover_all_roles_and_are_idempotent():
    created = demo_data.create_demo_users("demo-password-123")
    assert set(created) == {u for u, _ in demo_data.DEMO_USERS}
    assert demo_data.create_demo_users("demo-password-123") == []  # second run creates nothing
    with session_scope() as s:
        users = {u.username: u for u in s.scalars(select(User))}
    assert {u.role for u in users.values()} == {"viewer", "approver", "admin"}
    assert verify_password("demo-password-123", users["arjun.approver"].password_hash)


def test_scenarios_produce_a_realistic_mix():
    assert demo_data.run_scenarios() == 6
    outcome = demo_data.decide_some()
    completed = demo_data.complete_some(outcome["approved"])
    statuses = demo_data.summary()
    assert sum(statuses.values()) >= 4
    assert outcome["left_waiting"] >= 1 or "awaiting_approval" in statuses  # something is left to approve live
    assert outcome["approved"], statuses
    if len(outcome["approved"]) + len(outcome["rejected"]) >= 3:
        assert outcome["rejected"]  # one rejection when there are enough plans
    with session_scope() as s:
        by_id = {d.id: d for d in s.scalars(select(Disruption))}
    for did in completed:
        assert by_id[did].status == "resolved" and by_id[did].postmortem
    for did in outcome["approved"]:
        decision = by_id[did].decision or {}
        assert decision["reviewer"] in {"arjun.approver", "priya.approver", "anita.admin"}
        assert decision["reviewer_role"] in {"approver", "admin"}
