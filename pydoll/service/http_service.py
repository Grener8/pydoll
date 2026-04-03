from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import suppress
from typing import Any, Optional

from aiohttp import web
from pydantic import ValidationError, create_model

from pydoll.browser.options import ChromiumOptions
from pydoll.constants import Key
from pydoll.exceptions import (
    CommandExecutionTimeout,
    ElementException,
    NavigationError,
    PageLoadTimeout,
    PydollException,
    WaitElementTimeout,
)
from pydoll.extractor import ExtractionModel, Field
from pydoll.service.models import ActionsBody, CreateSessionRequest, ExtractBody, NavigateRequest
from pydoll.service.session_manager import (
    InvalidSessionError,
    SessionLimitError,
    SessionManager,
)

logger = logging.getLogger(__name__)


class ActionExecutionError(Exception):
    pass


def _json_error(
    error: str,
    error_type: str,
    *,
    session_id: Optional[str] = None,
    status: int = 500,
) -> web.Response:
    payload = {'error': error, 'type': error_type, 'session_id': session_id}
    return web.json_response(payload, status=status)


def _ms_to_seconds_timeout(value: Optional[int], default_seconds: int) -> int:
    if value is None:
        return default_seconds
    seconds = max(1, int(value / 1000))
    return seconds


def _get_api_key_from_header(request: web.Request) -> Optional[str]:
    header = request.headers.get('Authorization', '').strip()
    if not header.startswith('Bearer '):
        return None
    return header[7:].strip() or None


def _resolve_key(key_name: str) -> Key:
    normalized = key_name.strip()
    for key in Key:
        if key.value[0].lower() == normalized.lower():
            return key
        if key.name.lower() == normalized.replace('-', '').replace('_', '').lower():
            return key
    raise ValueError(f'Unsupported key: {key_name}')


def _create_extract_model(fields: list[dict[str, str]]) -> type[ExtractionModel]:
    model_fields: dict[str, tuple[type, Any]] = {}
    for item in fields:
        name = item['name']
        selector = item['selector']
        attribute = item.get('attribute')
        model_fields[name] = (
            Optional[str],
            Field(selector=selector, attribute=attribute, default=None),
        )
    return create_model('RemoteExtractionModel', __base__=ExtractionModel, **model_fields)


def _apply_browser_options(option_values: dict[str, Any], options: ChromiumOptions) -> None:
    arguments = option_values.get('arguments')
    if isinstance(arguments, list):
        for argument in arguments:
            if isinstance(argument, str) and argument not in options.arguments:
                options.add_argument(argument)

    binary_location = option_values.get('binary_location')
    if isinstance(binary_location, str):
        options.binary_location = binary_location

    start_timeout = option_values.get('start_timeout')
    if isinstance(start_timeout, int):
        options.start_timeout = start_timeout

    browser_preferences = option_values.get('browser_preferences')
    if isinstance(browser_preferences, dict):
        options.browser_preferences = browser_preferences

    if isinstance(option_values.get('headless'), bool):
        options.headless = option_values['headless']

    if isinstance(option_values.get('webrtc_leak_protection'), bool):
        options.webrtc_leak_protection = option_values['webrtc_leak_protection']


@web.middleware
async def auth_middleware(request: web.Request, handler):
    required_api_key = request.app.get('api_key')
    if not required_api_key:
        return await handler(request)

    provided = _get_api_key_from_header(request)
    if provided != required_api_key:
        return _json_error('Unauthorized', 'Unauthorized', status=401)
    return await handler(request)


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except InvalidSessionError as exc:
        session_id = request.match_info.get('id')
        return _json_error(str(exc), 'InvalidSession', session_id=session_id, status=404)
    except (asyncio.TimeoutError, PageLoadTimeout, WaitElementTimeout, CommandExecutionTimeout) as exc:
        session_id = request.match_info.get('id')
        return _json_error(str(exc), 'Timeout', session_id=session_id, status=408)
    except (ActionExecutionError, ElementException, NavigationError) as exc:
        session_id = request.match_info.get('id')
        return _json_error(str(exc), type(exc).__name__, session_id=session_id, status=422)
    except SessionLimitError as exc:
        return _json_error(str(exc), 'SessionLimitExceeded', status=429)
    except ValidationError as exc:
        session_id = request.match_info.get('id')
        return _json_error(str(exc), 'ValidationError', session_id=session_id, status=422)
    except PydollException as exc:
        session_id = request.match_info.get('id')
        return _json_error(str(exc), type(exc).__name__, session_id=session_id, status=500)
    except Exception as exc:  # noqa: BLE001
        logger.exception('Unexpected service error')
        session_id = request.match_info.get('id')
        return _json_error(str(exc), type(exc).__name__, session_id=session_id, status=500)


async def create_session_handler(request: web.Request) -> web.Response:
    manager: SessionManager = request.app['session_manager']
    payload = await request.json() if request.can_read_body else {}
    body = CreateSessionRequest.model_validate(payload or {})
    session_id = await manager.create_session(body.options)
    return web.json_response({'session_id': session_id})


async def navigate_handler(request: web.Request) -> web.Response:
    manager: SessionManager = request.app['session_manager']
    session_id = request.match_info['id']
    body = NavigateRequest.model_validate(await request.json())

    session = manager.get_session(session_id)
    timeout_seconds = _ms_to_seconds_timeout(body.timeout, 300)
    await manager.run_guarded(
        session_id,
        session.tab.go_to(body.url, timeout=timeout_seconds),
        timeout_ms=body.timeout,
    )
    return web.json_response({'ok': True})


async def _run_action(tab, action) -> dict[str, Any]:
    action_type = action.type.lower().strip()

    if action_type == 'click':
        if not action.selector:
            raise ActionExecutionError('click action requires selector')
        element = await tab.query(action.selector, timeout=_ms_to_seconds_timeout(action.timeout, 0))
        await element.click()
        return {'type': action.type, 'ok': True}

    if action_type == 'type':
        if not action.selector:
            raise ActionExecutionError('type action requires selector')
        element = await tab.query(action.selector, timeout=_ms_to_seconds_timeout(action.timeout, 0))
        await element.insert_text(action.text or '')
        return {'type': action.type, 'ok': True}

    if action_type == 'scroll':
        script = action.script
        if not script:
            distance = int(action.value) if action.value and action.value.isdigit() else 500
            script = f'window.scrollBy({{top: {distance}, behavior: "smooth"}});'
        await tab.execute_script(script)
        return {'type': action.type, 'ok': True}

    if action_type == 'wait':
        wait_for = (action.for_ or '').strip().lower()
        if wait_for == 'sleep':
            await asyncio.sleep((action.ms or 0) / 1000)
            return {'type': action.type, 'ok': True}

        selector = action.selector or action.value
        if not selector:
            raise ActionExecutionError('wait action requires selector or for=sleep with ms')
        await tab.query(selector, timeout=_ms_to_seconds_timeout(action.timeout or action.ms, 30))
        return {'type': action.type, 'ok': True}

    if action_type == 'evaluate':
        if not action.script:
            raise ActionExecutionError('evaluate action requires script')
        response = await tab.execute_script(action.script, return_by_value=True)
        return {'type': action.type, 'ok': True, 'result': response}

    if action_type == 'keypress':
        if not action.key:
            raise ActionExecutionError('keypress action requires key')
        key = _resolve_key(action.key)
        await tab.keyboard.press(key)
        return {'type': action.type, 'ok': True}

    raise ActionExecutionError(f'Unsupported action type: {action.type}')


async def actions_handler(request: web.Request) -> web.Response:
    manager: SessionManager = request.app['session_manager']
    session_id = request.match_info['id']
    body = ActionsBody.model_validate(await request.json())
    session = manager.get_session(session_id)

    results: list[dict[str, Any]] = []

    async def _execute_all() -> None:
        for index, action in enumerate(body.actions):
            try:
                result = await _run_action(session.tab, action)
                result['index'] = index
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                if not body.continue_on_error:
                    raise ActionExecutionError(str(exc)) from exc
                results.append({'index': index, 'type': action.type, 'ok': False, 'error': str(exc)})

    await manager.run_guarded(session_id, _execute_all(), timeout_ms=body.timeout)
    return web.json_response({'results': results})


async def content_handler(request: web.Request) -> web.Response:
    manager: SessionManager = request.app['session_manager']
    session_id = request.match_info['id']
    session = manager.get_session(session_id)

    response_format = request.query.get('format', 'html').lower()

    async def _read_content() -> web.Response:
        html = await session.tab.page_source
        if response_format == 'json':
            current_url = await session.tab.current_url
            title = await session.tab.title
            return web.json_response({'url': current_url, 'title': title, 'content': html})
        return web.json_response({'content': html})

    return await manager.run_guarded(session_id, _read_content())


async def extract_handler(request: web.Request) -> web.Response:
    manager: SessionManager = request.app['session_manager']
    session_id = request.match_info['id']
    session = manager.get_session(session_id)

    body = ExtractBody.model_validate(await request.json())
    fields = [field.model_dump() for field in body.model.fields]
    extraction_model = _create_extract_model(fields)

    async def _extract() -> web.Response:
        result = await session.tab.extract(extraction_model)
        return web.json_response(result.model_dump())

    return await manager.run_guarded(session_id, _extract())


async def get_cookies_handler(request: web.Request) -> web.Response:
    manager: SessionManager = request.app['session_manager']
    session_id = request.match_info['id']
    session = manager.get_session(session_id)

    async def _get() -> web.Response:
        cookies = await session.tab.get_cookies()
        return web.json_response({'cookies': cookies})

    return await manager.run_guarded(session_id, _get())


async def set_cookies_handler(request: web.Request) -> web.Response:
    manager: SessionManager = request.app['session_manager']
    session_id = request.match_info['id']
    session = manager.get_session(session_id)

    payload = await request.json()
    cookies = payload.get('cookies') if isinstance(payload, dict) else payload
    if not isinstance(cookies, list):
        raise ValidationError.from_exception_data('SetCookies', [])

    async def _set() -> web.Response:
        await session.tab.set_cookies(cookies)
        return web.json_response({'ok': True})

    return await manager.run_guarded(session_id, _set())


async def close_session_handler(request: web.Request) -> web.Response:
    manager: SessionManager = request.app['session_manager']
    session_id = request.match_info['id']
    await manager.close_session(session_id)
    return web.json_response({'ok': True})


async def cleanup_loop(app: web.Application) -> None:
    manager: SessionManager = app['session_manager']
    interval = app['cleanup_interval']
    while True:
        await asyncio.sleep(interval)
        with suppress(Exception):
            await manager.cleanup_expired_sessions()


def create_app(
    *,
    max_sessions: int,
    session_timeout: int,
    browser_options: Optional[dict[str, Any]] = None,
    headless: bool = False,
    api_key: Optional[str] = None,
) -> web.Application:
    options = ChromiumOptions()
    if browser_options:
        _apply_browser_options(browser_options, options)
    if headless:
        options.headless = True

    manager = SessionManager(
        max_sessions=max_sessions,
        session_timeout=session_timeout,
        browser_options=options,
        headless=headless,
    )

    app = web.Application(middlewares=[error_middleware, auth_middleware])
    app['session_manager'] = manager
    app['api_key'] = api_key
    app['cleanup_interval'] = max(1, min(30, session_timeout // 2 if session_timeout > 0 else 10))

    app.add_routes(
        [
            web.post('/sessions', create_session_handler),
            web.post('/sessions/{id}/navigate', navigate_handler),
            web.post('/sessions/{id}/actions', actions_handler),
            web.get('/sessions/{id}/content', content_handler),
            web.post('/sessions/{id}/extract', extract_handler),
            web.get('/sessions/{id}/cookies', get_cookies_handler),
            web.post('/sessions/{id}/cookies', set_cookies_handler),
            web.delete('/sessions/{id}', close_session_handler),
        ]
    )

    async def on_startup(app_: web.Application) -> None:
        app_['cleanup_task'] = asyncio.create_task(cleanup_loop(app_))

    async def on_shutdown(app_: web.Application) -> None:
        cleanup_task = app_.get('cleanup_task')
        if cleanup_task:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
        await app_['session_manager'].shutdown()

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


async def run_service(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s [%(name)s] %(message)s',
    )

    browser_options: Optional[dict[str, Any]] = None
    if args.browser_options:
        browser_options = json.loads(args.browser_options)

    app = create_app(
        max_sessions=args.max_sessions,
        session_timeout=args.session_timeout,
        browser_options=browser_options,
        headless=args.headless,
        api_key=args.api_key,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=args.host, port=args.port)
    await site.start()

    logger.info('pydoll-serve started on %s:%s', args.host, args.port)
    stop_event = asyncio.Event()

    try:
        await stop_event.wait()
    finally:
        await runner.cleanup()
