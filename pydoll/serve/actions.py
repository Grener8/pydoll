"""Action execution engine for pydoll-serve."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from pydoll.constants import Key
from pydoll.serve.models import Action, ActionResult

if TYPE_CHECKING:
    from pydoll.browser.tab import Tab

logger = logging.getLogger(__name__)

# Map common key name strings to Key enum members
_KEY_MAP: dict[str, Key] = {member.name.upper(): member for member in Key}
# Also allow the CDP key string itself (e.g. "Enter", "ArrowLeft")
_KEY_MAP.update({member.value[0].upper(): member for member in Key})


def _resolve_key(key_str: str) -> Key:
    """Resolve a human-readable key string to a :class:`Key` enum member."""
    normalised = key_str.upper().replace(' ', '')
    if normalised in _KEY_MAP:
        return _KEY_MAP[normalised]
    # Fallback: try the raw string as an enum name
    try:
        return Key[normalised]
    except KeyError as err:
        raise ValueError(f'Unknown key: {key_str!r}') from err


async def _action_click(tab: 'Tab', action: Action, timeout: int) -> None:
    if not action.selector:
        raise ValueError('"click" action requires a "selector"')
    element = await tab.query(action.selector, timeout=timeout // 1000)
    await element.click()


async def _action_type(tab: 'Tab', action: Action, timeout: int) -> None:
    if not action.selector:
        raise ValueError('"type" action requires a "selector"')
    if action.text is None:
        raise ValueError('"type" action requires "text"')
    element = await tab.query(action.selector, timeout=timeout // 1000)
    await element.insert_text(action.text)


async def _action_scroll(tab: 'Tab', action: Action, timeout: int) -> None:  # noqa: ARG001
    x = action.x or 0
    y = action.y or 0
    await tab.execute_script(f'window.scrollBy({x}, {y})')


async def _action_evaluate(tab: 'Tab', action: Action, timeout: int) -> Any:  # noqa: ARG001
    if not action.script:
        raise ValueError('"evaluate" action requires a "script"')
    response = await tab.execute_script(action.script)
    result_obj = response.get('result', {})
    return result_obj.get('value')


async def _action_keypress(tab: 'Tab', action: Action, timeout: int) -> None:  # noqa: ARG001
    if not action.key:
        raise ValueError('"keypress" action requires a "key"')
    key = _resolve_key(action.key)
    await tab.keyboard.press(key)


async def _action_wait(tab: 'Tab', action: Action, timeout: int) -> None:
    wait_for = action.wait_for or action.selector
    if not wait_for:
        raise ValueError('"wait" action requires "for" or "selector"')
    await tab.query(wait_for, timeout=timeout // 1000)


async def _action_sleep(tab: 'Tab', action: Action, timeout: int) -> None:  # noqa: ARG001
    ms = action.ms or 0
    await asyncio.sleep(ms / 1000)


_ACTION_HANDLERS = {
    'click': _action_click,
    'type': _action_type,
    'scroll': _action_scroll,
    'evaluate': _action_evaluate,
    'keypress': _action_keypress,
    'wait': _action_wait,
    'sleep': _action_sleep,
}


async def _execute_action(tab: 'Tab', action: Action, timeout: int) -> Any:
    """Execute a single action on *tab* and return the result value."""
    handler = _ACTION_HANDLERS.get(action.type)
    if handler is None:
        raise ValueError(f'Unknown action type: {action.type!r}')
    return await handler(tab, action, timeout)


async def execute_actions(
    tab: 'Tab',
    actions: list[Action],
    continue_on_error: bool = False,
    default_timeout: int = 30000,
) -> list[ActionResult]:
    """Execute a list of actions sequentially.

    Args:
        tab: The browser tab to execute actions on.
        actions: Ordered list of actions to perform.
        continue_on_error: When True, execution continues after a failed action.
        default_timeout: Default timeout in milliseconds for element-wait operations.

    Returns:
        List of :class:`ActionResult` instances, one per action.
    """
    results: list[ActionResult] = []

    for index, action in enumerate(actions):
        logger.info('Executing action %d: type=%s', index, action.type)
        try:
            value = await _execute_action(tab, action, default_timeout)
            results.append(
                ActionResult(index=index, type=action.type, success=True, result=value)
            )
            logger.debug('Action %d succeeded', index)
        except Exception as exc:
            logger.warning('Action %d failed: %s', index, exc)
            results.append(
                ActionResult(
                    index=index,
                    type=action.type,
                    success=False,
                    error=str(exc),
                )
            )
            if not continue_on_error:
                break

    return results
