import os
import tempfile

# Configure an isolated SQLite database and force the deterministic (no-LLM) path before app imports.
_tmp = tempfile.mkdtemp(prefix="scda-test-")
os.environ.update({
    "DATABASE_URL": f"sqlite:///{_tmp}/test.db",
    "CHECKPOINT_SQLITE_PATH": f"{_tmp}/checkpoints.db",
    "DEEPSEEK_API_KEY": "",
    "REDIS_URL": "",
    "RUN_EMBEDDED_WORKER": "false",
    "MONTE_CARLO_RUNS": "3000",
})

import pytest  # noqa: E402

from app.seed import reset_database  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    reset_database()
    yield
