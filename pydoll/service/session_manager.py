from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from pydoll import Chrome
from pydoll.browser.options import ChromiumOptions

logger = logging.getLogger(__name__)


class SessionManagerError(Exception):
    pass


class InvalidSessionError(SessionManagerError):
    pass


class SessionLimitError(SessionManagerError):
    pass


@dataclass(slots=True)
class Session:
    session_id: str
    tab: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SessionManager:
    def __init__(
        self,
        *,
        max_sessions: int,
        session_timeout: int,
        browser_options: Optional[ChromiumOptions] = None,
        headless: bool = False,
    ) -> None:
        self._max_sessions = max_sessions
        self._session_timeout = session_timeout
        self._browser_options = browser_options or ChromiumOptions()
        self._headless = headless

        self._browser: Optional[Chrome] = None
        self._sessions: dict[str, Session] = {}
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(max_sessions)

    @property
    def sessions(self) -> dict[str, Session]:
        return self._sessions

    async def _ensure_browser(self) -> Chrome:
        if self._browser is None:
            logger.info('Starting shared browser instance')
            self._browser = Chrome(options=self._browser_options)
        return self._browser

    async def shutdown(self) -> None:
        session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            await self.close_session(session_id)

        if self._browser is not None:
            logger.info('Stopping shared browser instance')
            await self._browser.stop()
            self._browser = None

    async def create_session(self, options: Optional[dict[str, Any]] = None) -> str:
        async with self._lock:
            if len(self._sessions) >= self._max_sessions:
                raise SessionLimitError('Maximum number of sessions reached')

            browser = await self._ensure_browser()
            if not self._sessions:
                tab = await browser.start(headless=self._headless)
            else:
                tab = await browser.new_tab()

            session_id = str(uuid4())
            session = Session(session_id=session_id, tab=tab, metadata=options or {})
            self._sessions[session_id] = session
            logger.info('Session created: %s', session_id)
            return session_id

    def get_session(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise InvalidSessionError('Invalid session')
        return session

    def touch(self, session_id: str) -> Session:
        session = self.get_session(session_id)
        session.last_activity = datetime.now(timezone.utc)
        return session

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            raise InvalidSessionError('Invalid session')

        await session.tab.close()
        logger.info('Session closed: %s', session_id)

    def get_expired_session_ids(self) -> list[str]:
        if self._session_timeout <= 0:
            return []
        now = datetime.now(timezone.utc)
        return [
            sid
            for sid, session in self._sessions.items()
            if (now - session.last_activity).total_seconds() > self._session_timeout
        ]

    async def cleanup_expired_sessions(self) -> int:
        expired = self.get_expired_session_ids()
        for session_id in expired:
            try:
                await self.close_session(session_id)
            except InvalidSessionError:
                continue
        if expired:
            logger.info('Expired sessions cleaned: %d', len(expired))
        return len(expired)

    async def run_guarded(self, session_id: str, awaitable, timeout_ms: Optional[int] = None):
        self.touch(session_id)
        async with self._semaphore:
            if timeout_ms is None:
                result = await awaitable
            else:
                result = await asyncio.wait_for(awaitable, timeout=timeout_ms / 1000)
        self.touch(session_id)
        return result
