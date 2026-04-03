"""Tests for pydoll.serve.actions."""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from pydoll.serve.actions import _resolve_key, execute_actions
from pydoll.serve.models import Action
from pydoll.constants import Key


# ---------------------------------------------------------------------------
# _resolve_key
# ---------------------------------------------------------------------------


class TestResolveKey:
    def test_enter_by_name(self):
        assert _resolve_key('Enter') == Key.ENTER

    def test_enter_uppercase(self):
        assert _resolve_key('ENTER') == Key.ENTER

    def test_arrowleft(self):
        assert _resolve_key('ArrowLeft') == Key.ARROWLEFT

    def test_unknown_key_raises(self):
        with pytest.raises(ValueError, match='Unknown key'):
            _resolve_key('SuperFakeKey')


# ---------------------------------------------------------------------------
# execute_actions
# ---------------------------------------------------------------------------


def _make_tab():
    tab = MagicMock()
    tab.query = AsyncMock()
    tab.execute_script = AsyncMock(return_value={'result': {'value': 42}})
    tab.keyboard = MagicMock()
    tab.keyboard.press = AsyncMock()

    element = MagicMock()
    element.click = AsyncMock()
    element.insert_text = AsyncMock()
    tab.query.return_value = element
    return tab, element


@pytest.mark.asyncio
class TestExecuteActions:
    async def test_sleep_action(self):
        tab, _ = _make_tab()
        actions = [Action(type='sleep', ms=10)]
        results = await execute_actions(tab, actions)
        assert len(results) == 1
        assert results[0].success is True

    async def test_click_action(self):
        tab, element = _make_tab()
        actions = [Action(type='click', selector='#btn')]
        results = await execute_actions(tab, actions)
        tab.query.assert_called_once_with('#btn', timeout=30)
        element.click.assert_called_once()
        assert results[0].success is True

    async def test_type_action(self):
        tab, element = _make_tab()
        actions = [Action(type='type', selector='input', text='hello')]
        results = await execute_actions(tab, actions)
        element.insert_text.assert_called_once_with('hello')
        assert results[0].success is True

    async def test_evaluate_action(self):
        tab, _ = _make_tab()
        actions = [Action(type='evaluate', script='return 1+1')]
        results = await execute_actions(tab, actions)
        tab.execute_script.assert_called_once_with('return 1+1')
        assert results[0].result == 42
        assert results[0].success is True

    async def test_keypress_action(self):
        tab, _ = _make_tab()
        actions = [Action(type='keypress', key='Enter')]
        results = await execute_actions(tab, actions)
        tab.keyboard.press.assert_called_once_with(Key.ENTER)
        assert results[0].success is True

    async def test_scroll_action(self):
        tab, _ = _make_tab()
        actions = [Action(type='scroll', x=0, y=500)]
        results = await execute_actions(tab, actions)
        tab.execute_script.assert_called_once_with('window.scrollBy(0, 500)')
        assert results[0].success is True

    async def test_wait_action(self):
        tab, _ = _make_tab()
        actions = [Action(**{'type': 'wait', 'for': '#element'})]
        results = await execute_actions(tab, actions)
        tab.query.assert_called_once_with('#element', timeout=30)
        assert results[0].success is True

    async def test_fail_fast_by_default(self):
        tab, element = _make_tab()
        element.click.side_effect = Exception('Element not found')
        actions = [
            Action(type='click', selector='#btn'),
            Action(type='sleep', ms=10),
        ]
        results = await execute_actions(tab, actions)
        assert len(results) == 1
        assert results[0].success is False

    async def test_continue_on_error(self):
        tab, element = _make_tab()
        element.click.side_effect = Exception('Element not found')
        actions = [
            Action(type='click', selector='#btn'),
            Action(type='sleep', ms=10),
        ]
        results = await execute_actions(tab, actions, continue_on_error=True)
        assert len(results) == 2
        assert results[0].success is False
        assert results[1].success is True

    async def test_click_missing_selector_raises(self):
        tab, _ = _make_tab()
        actions = [Action(type='click')]
        results = await execute_actions(tab, actions)
        assert results[0].success is False
        assert 'selector' in results[0].error

    async def test_unknown_action_type_raises(self):
        """Action type validation happens at model level — unknown types are rejected."""
        with pytest.raises(Exception):
            Action(type='fly_to_moon')
