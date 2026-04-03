"""Tests for pydoll.serve.session."""

import asyncio
import time

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from pydoll.serve.session import Session, SessionManager


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_browser(num_tabs: int = 0):
    """Return a mock browser that creates a new mock Tab on each new_tab() call."""
    browser = MagicMock()
    browser.new_tab = AsyncMock(side_effect=lambda: _make_tab())
    return browser


def _make_tab():
    tab = MagicMock()
    tab.close = AsyncMock()
    return tab


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------


class TestSession:
    def test_initial_last_activity(self):
        tab = _make_tab()
        before = time.monotonic()
        session = Session('sess-1', tab)
        assert session.last_activity >= before

    def test_touch_updates_last_activity(self):
        tab = _make_tab()
        session = Session('sess-1', tab)
        old_ts = session.last_activity
        time.sleep(0.01)
        session.touch()
        assert session.last_activity > old_ts

    def test_is_expired_false(self):
        tab = _make_tab()
        session = Session('sess-1', tab)
        assert session.is_expired(timeout_seconds=3600) is False

    def test_is_expired_true(self):
        tab = _make_tab()
        session = Session('sess-1', tab)
        # Simulate long-past last_activity
        session.last_activity = time.monotonic() - 9999
        assert session.is_expired(timeout_seconds=1) is True


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSessionManager:
    async def test_create_session(self):
        manager = SessionManager(_make_browser(), max_sessions=5, session_timeout=300)
        session = await manager.create_session()
        assert session.session_id
        assert manager.get_session(session.session_id) is session

    async def test_get_session_unknown_returns_none(self):
        manager = SessionManager(_make_browser(), max_sessions=5, session_timeout=300)
        assert manager.get_session('nonexistent') is None

    async def test_get_session_touches_activity(self):
        manager = SessionManager(_make_browser(), max_sessions=5, session_timeout=300)
        session = await manager.create_session()
        old_ts = session.last_activity
        time.sleep(0.01)
        manager.get_session(session.session_id)
        assert session.last_activity >= old_ts

    async def test_close_session(self):
        manager = SessionManager(_make_browser(), max_sessions=5, session_timeout=300)
        session = await manager.create_session()
        sid = session.session_id

        closed = await manager.close_session(sid)
        assert closed is True
        assert manager.get_session(sid) is None

    async def test_close_session_calls_tab_close(self):
        manager = SessionManager(_make_browser(), max_sessions=5, session_timeout=300)
        session = await manager.create_session()
        tab = session.tab
        await manager.close_session(session.session_id)
        tab.close.assert_called_once()

    async def test_close_nonexistent_session_returns_false(self):
        manager = SessionManager(_make_browser(), max_sessions=5, session_timeout=300)
        closed = await manager.close_session('nonexistent')
        assert closed is False

    async def test_max_sessions_enforced(self):
        manager = SessionManager(_make_browser(), max_sessions=2, session_timeout=300)
        # Exhaust the semaphore manually so create_session raises
        manager._semaphore = asyncio.Semaphore(0)
        # Also fill sessions dict
        for i in range(2):
            manager._sessions[str(i)] = MagicMock()

        with pytest.raises(RuntimeError, match='Maximum'):
            await manager.create_session()

    async def test_cleanup_removes_expired_sessions(self):
        manager = SessionManager(_make_browser(), max_sessions=5, session_timeout=1)
        session = await manager.create_session()
        sid = session.session_id
        # Simulate session expired
        session.last_activity = time.monotonic() - 9999

        await manager._remove_expired_sessions()
        assert manager.get_session(sid) is None
