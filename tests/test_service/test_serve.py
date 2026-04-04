from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from pydoll.cli import build_parser
from pydoll.constants import Key
from pydoll.service.http_service import _resolve_key, _run_action
from pydoll.service.models import ActionRequest
from pydoll.service.session_manager import InvalidSessionError, SessionManager, SessionLimitError


def test_cli_parse_serve_args():
    parser = build_parser()
    args = parser.parse_args(
        [
            'serve',
            '--host',
            '127.0.0.1',
            '--port',
            '9000',
            '--max-sessions',
            '7',
            '--session-timeout',
            '120',
            '--headless',
        ]
    )

    assert args.command == 'serve'
    assert args.host == '127.0.0.1'
    assert args.port == 9000
    assert args.max_sessions == 7
    assert args.session_timeout == 120
    assert args.headless is True


def test_resolve_key_accepts_enum_name_and_value():
    assert _resolve_key('Enter') == Key.ENTER
    assert _resolve_key('enter') == Key.ENTER
    assert _resolve_key('ARROWDOWN') == Key.ARROWDOWN


@pytest.mark.asyncio
async def test_run_action_click_uses_selector_query_and_click():
    element = AsyncMock()
    tab = AsyncMock()
    tab.query = AsyncMock(return_value=element)

    action = ActionRequest(type='click', selector='#submit')
    result = await _run_action(tab, action)

    assert result['ok'] is True
    tab.query.assert_awaited_once()
    element.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_action_type_uses_insert_text():
    element = AsyncMock()
    tab = AsyncMock()
    tab.query = AsyncMock(return_value=element)

    action = ActionRequest(type='type', selector='#input', text='hello')
    result = await _run_action(tab, action)

    assert result['ok'] is True
    element.insert_text.assert_awaited_once_with('hello')


@pytest.mark.asyncio
async def test_run_action_keypress_calls_keyboard_press():
    keyboard = AsyncMock()
    tab = AsyncMock()
    tab.keyboard = keyboard

    action = ActionRequest(type='keypress', key='Enter')
    result = await _run_action(tab, action)

    assert result['ok'] is True
    keyboard.press.assert_awaited_once_with(Key.ENTER)


@pytest.mark.asyncio
async def test_session_manager_create_and_close_with_shared_browser_tabs():
    fake_tab_1 = AsyncMock()
    fake_tab_2 = AsyncMock()
    fake_browser = AsyncMock()
    fake_browser.start = AsyncMock(return_value=fake_tab_1)
    fake_browser.new_tab = AsyncMock(return_value=fake_tab_2)

    with patch('pydoll.service.session_manager.Chrome', return_value=fake_browser):
        manager = SessionManager(max_sessions=2, session_timeout=60)

        first_id = await manager.create_session()
        second_id = await manager.create_session()

        assert first_id != second_id
        assert len(manager.sessions) == 2
        fake_browser.start.assert_awaited_once()
        fake_browser.new_tab.assert_awaited_once()

        await manager.close_session(first_id)
        assert len(manager.sessions) == 1
        fake_tab_1.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_session_manager_rejects_when_max_sessions_reached():
    fake_browser = AsyncMock()
    fake_browser.start = AsyncMock(return_value=AsyncMock())

    with patch('pydoll.service.session_manager.Chrome', return_value=fake_browser):
        manager = SessionManager(max_sessions=1, session_timeout=60)
        await manager.create_session()

        with pytest.raises(SessionLimitError):
            await manager.create_session()


@pytest.mark.asyncio
async def test_session_manager_invalid_session_lookup_raises():
    manager = SessionManager(max_sessions=1, session_timeout=60)

    with pytest.raises(InvalidSessionError):
        manager.get_session('missing')


@pytest.mark.asyncio
async def test_session_manager_cleanup_expired_sessions():
    tab = AsyncMock()
    browser = AsyncMock()
    browser.start = AsyncMock(return_value=tab)

    with patch('pydoll.service.session_manager.Chrome', return_value=browser):
        manager = SessionManager(max_sessions=1, session_timeout=1)
        session_id = await manager.create_session()

        # Force expiration
        manager.sessions[session_id].last_activity = manager.sessions[session_id].last_activity.replace(
            year=2000
        )

        cleaned = await manager.cleanup_expired_sessions()

        assert cleaned == 1
        tab.close.assert_awaited_once()
