"""Demo data for showing the app to other people: sample users with different roles, and a realistic spread of
disruptions (some auto-executed, some approved and completed with a post-mortem, one rejected, a few still waiting
for you to approve live).

    python -m app.demo_data users                  # sample accounts (prints the password once)
    python -m app.demo_data scenarios [--force]    # run every demo scenario through the real agents, then decide some
    python -m app.demo_data all [--force]          # both

Everything goes through the same pipeline as the live app: real LLM calls if DEEPSEEK_API_KEY is set (a few cents),
otherwise the deterministic fallbacks. Run it against a DEVELOPMENT / demo database, not a real one.
"""
import argparse
import logging
import os
import secrets
import sys
from collections import Counter

from sqlalchemy import select

from app.agents.graph import is_waiting, resume
from app.config import settings
from app.db import init_db, session_scope
from app.events import make_event
from app.models import Disruption, PurchaseOrder, User, utcnow
from app.seed import SCENARIOS, seed
from app.services.ingestion import apply_event
from app.services.postmortem import receive_purchase_order, run_postmortem
from app.users import create_user
from app.worker import process_event

log = logging.getLogger("demo_data")

DEMO_USERS = [
    ("anita.admin", "admin"),
    ("arjun.approver", "approver"),
    ("priya.approver", "approver"),
    ("vikram.viewer", "viewer"),
    ("sara.viewer", "viewer"),
]
REVIEWERS = [("arjun.approver", "approver"), ("priya.approver", "approver"), ("anita.admin", "admin")]
APPROVE_COMMENTS = [
    "Supplier is reliable and the cost is justified by the VIP exposure. Go ahead.",
    "Cheapest option that still protects the customer orders. Approved.",
    "Expected loss far exceeds the spend, so proceeding.",
    "Approved. Ops confirmed the supplier can ship this week.",
]
REJECT_COMMENT = "Reliability of this supplier is too low for a critical order; asking sourcing for another option."
KEEP_WAITING = 2  # leave this many approvals open so the demo can approve one live


def _iso(dt) -> str:
    return dt.isoformat() + "Z"


def create_demo_users(password: str) -> list[str]:
    """Create the sample accounts that do not exist yet. Returns the usernames created."""
    created = []
    with session_scope() as s:
        for username, role in DEMO_USERS:
            if s.scalar(select(User).where(User.username == username)):
                continue
            create_user(s, username, password, role, created_by="demo_data")
            created.append(username)
    return created


def run_scenarios() -> int:
    """Apply and process every demo scenario like the live system would. Returns the number of events processed."""
    for name, (type_, payload) in SCENARIOS.items():
        with session_scope() as s:
            event = apply_event(s, make_event(type_, dict(payload), source=f"demo:{name}"))
        result = process_event(event)
        log.info("scenario %-18s -> %s", name, {k: v for k, v in result.items() if k != "runs"} or result)
    return len(SCENARIOS)


def decide_some() -> dict:
    """Approve most waiting plans as different named reviewers, reject one, and leave a few open."""
    with session_scope() as s:
        waiting = [(d.id, d.thread_id) for d in s.scalars(
            select(Disruption).where(Disruption.status == "awaiting_approval").order_by(Disruption.id))]
    waiting = [w for w in waiting if is_waiting(w[1])]
    to_decide = waiting[: max(0, len(waiting) - KEEP_WAITING)]
    approved, rejected = [], []
    for i, (did, thread_id) in enumerate(to_decide):
        reviewer, role = REVIEWERS[i % len(REVIEWERS)]
        reject = i == 2  # one rejection for variety (needs at least three decided plans)
        decision = {"approved": not reject, "option_id": None, "reviewer": reviewer, "reviewer_role": role,
                    "comment": REJECT_COMMENT if reject else APPROVE_COMMENTS[i % len(APPROVE_COMMENTS)],
                    "decided_at": _iso(utcnow())}
        resume(thread_id, decision)
        (rejected if reject else approved).append(did)
        log.info("%s %s disruption %s", reviewer, "rejected" if reject else "approved", did)
    return {"approved": approved, "rejected": rejected, "left_waiting": len(waiting) - len(to_decide)}


def complete_some(disruption_ids: list[int], how_many: int = 2) -> list[int]:
    """Receive the purchase orders of the first few approved disruptions and write their post-mortems."""
    done = []
    for did in disruption_ids[:how_many]:
        with session_scope() as s:
            pos = s.scalars(select(PurchaseOrder).where(PurchaseOrder.disruption_id == did,
                                                        PurchaseOrder.status == "submitted")).all()
            if not pos:
                continue
            for po in pos:
                receive_purchase_order(s, po.id, actual_days=round((po.expected_days or 10) * 0.9, 1))
            run_postmortem(s, did)
            done.append(did)
        log.info("disruption %s completed (received + post-mortem)", did)
    return done


def summary() -> dict:
    with session_scope() as s:
        return dict(Counter(d.status for d in s.scalars(select(Disruption))))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["users", "scenarios", "all"])
    ap.add_argument("--force", action="store_true", help="run scenarios even if disruptions already exist")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    init_db()
    with session_scope() as s:
        seed(s)  # base suppliers/products/inventory if the database is empty

    if args.what in ("users", "all"):
        password = os.environ.get("DEMO_USER_PASSWORD") or secrets.token_urlsafe(12)
        created = create_demo_users(password)
        if created:
            print(f"Created users ({', '.join(created)}) with password: {password}")
        else:
            print("Demo users already exist; nothing created.")

    if args.what in ("scenarios", "all"):
        with session_scope() as s:
            existing = len(s.scalars(select(Disruption.id)).all())
        if existing and not args.force:
            print(f"{existing} disruptions already exist; skipping scenarios (use --force to run anyway).")
            return 0
        print(f"Running scenarios (LLM: {settings.deepseek_model if settings.llm_enabled else 'fallback'}) ...")
        run_scenarios()
        outcome = decide_some()
        completed = complete_some(outcome["approved"])
        print("Decisions:", outcome, "| completed:", completed)
        print("Disruptions by status:", summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
