"""Session management for pydoll-serve."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from pydoll.browser.chromium.base import Browser
    from pydoll.browser.tab import Tab

logger = logging.getLogger(__name__)


class Session:
    """Represents a browser session backed by a single tab."""

    def __init__(self, session_id: str, tab: 'Tab') -> None:
        self.session_id = session_id
        self.tab = tab
        self.last_activity: float = time.monotonic()

    def touch(self) -> None:
        """Update the last-activity timestamp."""
        self.last_activity = time.monotonic()

    def is_expired(self, timeout_seconds: float) -> bool:
        """Return True if the session has been inactive for longer than *timeout_seconds*."""
        return (time.monotonic() - self.last_activity) > timeout_seconds


class SessionManager:
    """Creates, stores, and expires browser sessions."""

    def __init__(
        self,
        browser: 'Browser',
        max_sessions: int = 10,
        session_timeout: float = 300,
    ) -> None:
        self._browser = browser
        self._max_sessions = max_sessions
        self._session_timeout = session_timeout
        self._sessions: dict[str, Session] = {}
        self._semaphore = asyncio.Semaphore(max_sessions)
        self._cleanup_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_session(self) -> Session:
        """Create a new browser tab and return a Session.

        Raises:
            RuntimeError: If the maximum number of sessions has been reached.
        """
        if len(self._sessions) >= self._max_sessions:
            raise RuntimeError('Maximum number of sessions reached')

        async with self._semaphore:
            tab = await self._browser.new_tab()
            session_id = str(uuid.uuid4())
            session = Session(session_id, tab)
            self._sessions[session_id] = session
            logger.info('Session created: %s', session_id)
            return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """Return the session with *session_id*, or None if it does not exist."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.touch()
        return session

    async def close_session(self, session_id: str) -> bool:
        """Close and remove a session.

        Returns:
            True if the session was found and closed, False otherwise.
        """
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        try:
            await session.tab.close()
        except Exception:
            logger.exception('Error closing tab for session %s', session_id)
        logger.info('Session closed: %s', session_id)
        return True

    def start_cleanup_loop(self) -> None:
        """Start a background task that periodically removes expired sessions."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.debug('Session cleanup loop started')

    async def stop_cleanup_loop(self) -> None:
        """Cancel the background cleanup task."""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        logger.debug('Session cleanup loop stopped')

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        """Continuously remove expired sessions every *session_timeout / 2* seconds."""
        interval = max(self._session_timeout / 2, 10)
        while True:
            await asyncio.sleep(interval)
            await self._remove_expired_sessions()

    async def _remove_expired_sessions(self) -> None:
        expired = [
            sid
            for sid, session in list(self._sessions.items())
            if session.is_expired(self._session_timeout)
        ]
        for sid in expired:
            logger.info('Expiring idle session: %s', sid)
            await self.close_session(sid)
