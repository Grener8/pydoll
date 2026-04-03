"""aiohttp HTTP server for pydoll-serve."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from aiohttp import web

from pydoll.browser.chromium.chrome import Chrome
from pydoll.browser.options import ChromiumOptions
from pydoll.serve.actions import execute_actions
from pydoll.serve.models import (
    ActionsRequest,
    ActionsResponse,
    ContentResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    ErrorResponse,
    ExtractRequest,
    NavigateRequest,
    SetCookiesRequest,
)
from pydoll.serve.session import SessionManager

logger = logging.getLogger(__name__)

# Key used to store the SessionManager on the application.
_SESSION_MANAGER_KEY = 'session_manager'
# Key used to store the optional API key on the application.
_API_KEY_KEY = 'api_key'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_response(data: Any, status: int = 200) -> web.Response:
    return web.Response(
        text=json.dumps(data),
        status=status,
        content_type='application/json',
    )


def _error_response(
    error: str,
    error_type: str,
    status: int,
    session_id: Optional[str] = None,
) -> web.Response:
    body = ErrorResponse(error=error, type=error_type, session_id=session_id)
    return _json_response(body.model_dump(), status=status)


def _get_manager(request: web.Request) -> SessionManager:
    return request.app[_SESSION_MANAGER_KEY]


def _parse_body(data: dict, model_class):  # type: ignore[no-untyped-def]
    """Parse *data* dict into *model_class*, raising HTTP 422 on validation errors."""
    try:
        return model_class(**data)
    except Exception as exc:
        raise web.HTTPUnprocessableEntity(
            text=json.dumps({'error': str(exc), 'type': 'ValidationError'}),
            content_type='application/json',
        ) from exc


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


@web.middleware
async def auth_middleware(request: web.Request, handler):  # type: ignore[no-untyped-def]
    """Optionally enforce Bearer token authentication."""
    api_key: Optional[str] = request.app.get(_API_KEY_KEY)
    if api_key:
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer ') or auth_header[7:] != api_key:
            return _error_response('Unauthorized', 'Unauthorized', 401)
    return await handler(request)


@web.middleware
async def error_middleware(request: web.Request, handler):  # type: ignore[no-untyped-def]
    """Catch unhandled exceptions and convert them to JSON error responses."""
    try:
        return await handler(request)
    except web.HTTPException as exc:
        return web.Response(
            text=exc.text or exc.reason,
            status=exc.status,
            content_type='application/json',
        )
    except Exception as exc:
        logger.exception('Unhandled error for %s %s', request.method, request.path)
        return _error_response(str(exc), 'InternalServerError', 500)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _post_sessions(request: web.Request) -> web.Response:
    """POST /sessions — create a new session."""
    raw = await request.json() if request.content_length else {}
    _parse_body(raw, CreateSessionRequest)
    manager = _get_manager(request)

    try:
        session = await manager.create_session()
    except RuntimeError as exc:
        return _error_response(str(exc), 'TooManyRequests', 429)

    logger.info('Session created via API: %s', session.session_id)
    response = CreateSessionResponse(session_id=session.session_id)
    return _json_response(response.model_dump(), status=201)


async def _post_navigate(request: web.Request) -> web.Response:
    """POST /sessions/{id}/navigate — navigate the tab to a URL."""
    session_id = request.match_info['session_id']
    manager = _get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        return _error_response('Session not found', 'SessionNotFound', 404, session_id)

    raw = await request.json()
    body = _parse_body(raw, NavigateRequest)

    timeout_seconds = body.timeout // 1000
    try:
        await session.tab.go_to(body.url, timeout=timeout_seconds)
    except Exception as exc:
        logger.warning('Navigation failed for session %s: %s', session_id, exc)
        return _error_response(str(exc), 'NavigationError', 408, session_id)

    logger.info('Navigated session %s to %s', session_id, body.url)
    return _json_response({'url': body.url})


async def _post_actions(request: web.Request) -> web.Response:
    """POST /sessions/{id}/actions — execute a list of actions."""
    session_id = request.match_info['session_id']
    manager = _get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        return _error_response('Session not found', 'SessionNotFound', 404, session_id)

    raw = await request.json()
    body = _parse_body(raw, ActionsRequest)

    results = await execute_actions(
        session.tab,
        body.actions,
        continue_on_error=body.continue_on_error,
    )

    # Determine overall HTTP status: 422 if any action failed and continue_on_error is False
    failed = [r for r in results if not r.success]
    status = 422 if failed and not body.continue_on_error else 200
    response = ActionsResponse(results=results)
    return _json_response(response.model_dump(), status=status)


async def _get_content(request: web.Request) -> web.Response:
    """GET /sessions/{id}/content — retrieve page HTML."""
    session_id = request.match_info['session_id']
    manager = _get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        return _error_response('Session not found', 'SessionNotFound', 404, session_id)

    fmt = request.rel_url.query.get('format', 'html')

    if fmt == 'json':
        # Future-proofed: return raw JSON with HTML content
        html = await session.tab.page_source
        return _json_response({'content': html, 'format': 'json'})

    html = await session.tab.page_source
    response = ContentResponse(content=html)
    return _json_response(response.model_dump())


async def _post_extract(request: web.Request) -> web.Response:
    """POST /sessions/{id}/extract — extract structured data from the page."""
    session_id = request.match_info['session_id']
    manager = _get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        return _error_response('Session not found', 'SessionNotFound', 404, session_id)

    raw = await request.json()
    body = _parse_body(raw, ExtractRequest)

    extracted: dict[str, Any] = {}
    for field in body.model.fields:
        try:
            element = await session.tab.query(field.selector)
            if field.attribute:
                value = await element.get_attribute(field.attribute)
            else:
                value = await element.get_element_text()
            extracted[field.name] = value
        except Exception as exc:
            logger.warning(
                'Extraction failed for field %r in session %s: %s',
                field.name,
                session_id,
                exc,
            )
            extracted[field.name] = None

    return _json_response(extracted)


async def _get_cookies(request: web.Request) -> web.Response:
    """GET /sessions/{id}/cookies — retrieve cookies for the session."""
    session_id = request.match_info['session_id']
    manager = _get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        return _error_response('Session not found', 'SessionNotFound', 404, session_id)

    cookies = await session.tab.get_cookies()
    return _json_response({'cookies': cookies})


async def _post_cookies(request: web.Request) -> web.Response:
    """POST /sessions/{id}/cookies — set cookies for the session."""
    session_id = request.match_info['session_id']
    manager = _get_manager(request)
    session = manager.get_session(session_id)
    if session is None:
        return _error_response('Session not found', 'SessionNotFound', 404, session_id)

    raw = await request.json()
    body = _parse_body(raw, SetCookiesRequest)

    # Build CookieParam list from our model (exclude None values)
    cookie_params = [
        {k: v for k, v in cookie.model_dump().items() if v is not None}
        for cookie in body.cookies
    ]
    await session.tab.set_cookies(cookie_params)  # type: ignore[arg-type]
    return _json_response({'set': len(cookie_params)})


async def _delete_session(request: web.Request) -> web.Response:
    """DELETE /sessions/{id} — close and remove a session."""
    session_id = request.match_info['session_id']
    manager = _get_manager(request)
    closed = await manager.close_session(session_id)
    if not closed:
        return _error_response('Session not found', 'SessionNotFound', 404, session_id)
    logger.info('Session deleted via API: %s', session_id)
    return web.Response(status=204)


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    browser: Chrome,
    *,
    max_sessions: int = 10,
    session_timeout: float = 300,
    api_key: Optional[str] = None,
) -> web.Application:
    """Create and configure the aiohttp :class:`web.Application`.

    Args:
        browser: An already-started :class:`Chrome` instance shared across sessions.
        max_sessions: Maximum number of concurrent sessions.
        session_timeout: Seconds of inactivity before a session is expired.
        api_key: Optional Bearer token required for all requests.

    Returns:
        Configured :class:`web.Application` ready to be served.
    """
    manager = SessionManager(
        browser=browser,
        max_sessions=max_sessions,
        session_timeout=session_timeout,
    )

    app = web.Application(middlewares=[error_middleware, auth_middleware])
    app[_SESSION_MANAGER_KEY] = manager
    app[_API_KEY_KEY] = api_key

    # Lifecycle hooks
    async def on_startup(_app: web.Application) -> None:
        manager.start_cleanup_loop()
        logger.info('pydoll-serve started (max_sessions=%d)', max_sessions)

    async def on_cleanup(_app: web.Application) -> None:
        await manager.stop_cleanup_loop()
        logger.info('pydoll-serve shutting down')

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Routes
    app.router.add_post('/sessions', _post_sessions)
    app.router.add_post('/sessions/{session_id}/navigate', _post_navigate)
    app.router.add_post('/sessions/{session_id}/actions', _post_actions)
    app.router.add_get('/sessions/{session_id}/content', _get_content)
    app.router.add_post('/sessions/{session_id}/extract', _post_extract)
    app.router.add_get('/sessions/{session_id}/cookies', _get_cookies)
    app.router.add_post('/sessions/{session_id}/cookies', _post_cookies)
    app.router.add_delete('/sessions/{session_id}', _delete_session)

    return app


async def run_server(
    host: str = '0.0.0.0',
    port: int = 8000,
    *,
    headless: bool = True,
    max_sessions: int = 10,
    session_timeout: float = 300,
    browser_options: Optional[dict] = None,
    api_key: Optional[str] = None,
) -> None:
    """Start the browser and run the HTTP server until interrupted.

    Args:
        host: Bind address for the HTTP server.
        port: TCP port for the HTTP server.
        headless: Whether to start Chrome in headless mode.
        max_sessions: Maximum number of concurrent sessions.
        session_timeout: Inactivity timeout for sessions (seconds).
        browser_options: Additional Chrome options as a dict (arguments list, etc.).
        api_key: Optional Bearer token for request authentication.
    """
    options = ChromiumOptions()
    options.headless = headless

    if browser_options:
        for arg in browser_options.get('arguments', []):
            options.add_argument(arg)

    browser = Chrome(options=options)
    await browser.start(headless=headless)

    app = create_app(
        browser,
        max_sessions=max_sessions,
        session_timeout=session_timeout,
        api_key=api_key,
    )

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info('pydoll-serve listening on http://%s:%d', host, port)

    try:
        await asyncio.Event().wait()  # run forever
    finally:
        await runner.cleanup()
        await browser.stop()
