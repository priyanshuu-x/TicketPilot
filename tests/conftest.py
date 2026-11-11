import pytest

from ticketpilot.config import settings
from ticketpilot.mcp_server.db import seed


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "support_db_path", str(tmp_path / "test.db"))
    seed()
