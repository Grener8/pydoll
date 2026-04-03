from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from email.utils import format_datetime
from http.cookies import SimpleCookie
from time import monotonic
from typing import Any, Optional

from scrapy import Request, signals
from scrapy.downloadermiddlewares.retry import get_retry_request
from scrapy.exceptions import IgnoreRequest
from scrapy.http import HtmlResponse
from scrapy.utils.defer import deferred_from_coro

from pydoll import exceptions as pydoll_exceptions
from pydoll.browser import Chrome
from pydoll.browser.options import ChromiumOptions

logger = logging.getLogger(__name__)
ASYNCIO_REACTOR = 'twisted.internet.asyncioreactor.AsyncioSelectorReactor'


class InvalidPydollAction(ValueError):
    pass


class PydollTimeout(TimeoutError):
    pass


class PydollMiddleware:
    RETRYABLE_EXCEPTIONS = (
        PydollTimeout,
        asyncio.TimeoutError,
        pydoll_exceptions.BrowserException,
        pydoll_exceptions.NavigationError,
        pydoll_exceptions.PageLoadTimeout,
    )
    SELECTOR_EXCEPTIONS = (
        pydoll_exceptions.ElementNotFound,
        pydoll_exceptions.WaitElementTimeout,
    )

    def __init__(
        self,
        enabled: bool,
        concurrency: int,
        browser_options: dict[str, Any],
        default_timeout_ms: int,
        enable_cloudflare_solver: bool,
    ):
        self.enabled = enabled
        self.default_timeout_ms = default_timeout_ms
        self.enable_cloudflare_solver = enable_cloudflare_solver

        options = ChromiumOptions()
        self._apply_browser_options(options, browser_options)

        self.browser = Chrome(options=options)
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._browser_started = False
        self._start_lock = asyncio.Lock()

    @classmethod
    def from_crawler(cls, crawler):
        settings = crawler.settings
        reactor_setting = settings.get('TWISTED_REACTOR')
        if reactor_setting != ASYNCIO_REACTOR:
            raise RuntimeError(f'TWISTED_REACTOR must be set to {ASYNCIO_REACTOR}')

        middleware = cls(
            enabled=settings.getbool('PYDOLL_ENABLED', True),
            concurrency=settings.getint('PYDOLL_CONCURRENCY', 4),
            browser_options=settings.getdict('PYDOLL_BROWSER_OPTIONS', {}),
            default_timeout_ms=settings.getint('PYDOLL_DEFAULT_TIMEOUT', 30000),
            enable_cloudflare_solver=settings.getbool('PYDOLL_ENABLE_CLOUDFLARE_SOLVER', True),
        )
        crawler.signals.connect(middleware.spider_closed, signal=signals.spider_closed)
        return middleware

    def spider_closed(self, spider):
        return deferred_from_coro(self._spider_closed())

    async def _spider_closed(self):
        if not self._browser_started:
            return

        with suppress(Exception):
            await self.browser.stop()

    async def process_request(self, request: Request, spider):
        if not self.enabled:
            return None

        pydoll_meta = request.meta.get('pydoll')
        if not pydoll_meta:
            return None
        if pydoll_meta is True:
            pydoll_meta = {}
        if not isinstance(pydoll_meta, dict):
            request.meta['pydoll_error'] = 'Invalid pydoll metadata format'
            raise IgnoreRequest('Invalid pydoll metadata format')

        actions = pydoll_meta.get('actions') or []
        timeout_ms = int(pydoll_meta.get('timeout', self.default_timeout_ms))
        solve_captcha = bool(
            pydoll_meta.get('solve_captcha', self.enable_cloudflare_solver)
        )

        await self._ensure_browser_started()
        logger.info('Pydoll request start: %s', request.url)

        start_time = monotonic()
        tab = None

        try:
            async with self._semaphore:
                self._check_remaining_time(start_time, timeout_ms)
                tab = await self.browser.new_tab()

                await self._inject_request_cookies(request, tab)
                await self._navigate(request.url, tab, start_time, timeout_ms)

                if solve_captcha:
                    await tab.enable_auto_solve_cloudflare_captcha()

                await self._run_actions(tab, actions, start_time, timeout_ms)

                html = await tab.page_source
                cookies = await tab.get_cookies()

            response = HtmlResponse(
                url=request.url,
                body=(html or '').encode('utf-8'),
                encoding='utf-8',
                request=request,
            )
            self._attach_set_cookie_headers(response, cookies)

            logger.info('Pydoll request end: %s', request.url)
            return response

        except self.RETRYABLE_EXCEPTIONS as exc:
            return self._retry_or_fail(request, spider, exc, actions)
        except self.SELECTOR_EXCEPTIONS as exc:
            self._record_failure(request, exc, actions)
            raise IgnoreRequest(f'Pydoll selector not found: {exc}') from exc
        except InvalidPydollAction as exc:
            self._record_failure(request, exc, actions)
            raise IgnoreRequest(f'Pydoll invalid action: {exc}') from exc
        except Exception as exc:
            self._record_failure(request, exc, actions)
            raise
        finally:
            if tab is not None:
                with suppress(Exception):
                    if solve_captcha:
                        await tab.disable_auto_solve_cloudflare_captcha()
                with suppress(Exception):
                    await tab.close()

    async def _ensure_browser_started(self):
        if self._browser_started:
            return

        async with self._start_lock:
            if self._browser_started:
                return

            startup_tab = await self.browser.start()
            self._browser_started = True
            with suppress(Exception):
                await startup_tab.close()

    @staticmethod
    def _apply_browser_options(options: ChromiumOptions, browser_options: dict[str, Any]):
        for key, value in browser_options.items():
            if key == 'arguments' and isinstance(value, list):
                options.arguments = value
                continue
            setattr(options, key, value)

    async def _navigate(self, url: str, tab, start_time: float, timeout_ms: int):
        remaining_seconds = self._remaining_seconds(start_time, timeout_ms)
        await tab.go_to(url, timeout=remaining_seconds)

    @staticmethod
    async def _inject_request_cookies(request: Request, tab):
        cookie_header = request.headers.get('Cookie') or request.headers.get(b'Cookie')
        if not cookie_header:
            return

        if isinstance(cookie_header, bytes):
            cookie_header = cookie_header.decode('latin1', errors='ignore')

        cookie_parser = SimpleCookie()
        cookie_parser.load(cookie_header)

        cookies = [
            {
                'name': morsel.key,
                'value': morsel.value,
                'url': request.url,
            }
            for morsel in cookie_parser.values()
        ]
        if cookies:
            await tab.set_cookies(cookies)

    @staticmethod
    def _attach_set_cookie_headers(response: HtmlResponse, cookies: list[dict[str, Any]]):
        for cookie in cookies:
            raw_header = PydollMiddleware._cookie_to_set_cookie(cookie)
            if raw_header:
                response.headers.appendlist('Set-Cookie', raw_header.encode('utf-8'))

    @staticmethod
    def _cookie_to_set_cookie(cookie: dict[str, Any]) -> str:
        name = cookie.get('name')
        value = cookie.get('value')
        if not name or value is None:
            return ''

        parts = [f'{name}={value}']

        domain = cookie.get('domain')
        if domain:
            parts.append(f'Domain={domain}')

        path = cookie.get('path')
        if path:
            parts.append(f'Path={path}')

        expires = cookie.get('expires')
        if isinstance(expires, (int, float)) and expires > 0:
            expires_dt = datetime.fromtimestamp(expires, tz=timezone.utc)
            parts.append(f'Expires={format_datetime(expires_dt, usegmt=True)}')

        if cookie.get('secure'):
            parts.append('Secure')

        if cookie.get('httpOnly'):
            parts.append('HttpOnly')

        same_site = cookie.get('sameSite')
        if same_site:
            parts.append(f'SameSite={same_site}')

        return '; '.join(parts)

    async def _run_actions(
        self,
        tab,
        actions: list[dict[str, Any]],
        start_time: float,
        timeout_ms: int,
    ):
        for action in actions:
            self._check_remaining_time(start_time, timeout_ms)
            await self._execute_action(tab, action, start_time, timeout_ms)

    async def _execute_action(
        self,
        tab,
        action: dict[str, Any],
        start_time: float,
        timeout_ms: int,
    ):
        if not isinstance(action, dict):
            raise InvalidPydollAction('Action must be a dict')

        action_type = action.get('type')
        logger.info('Executing Pydoll action: %s', action_type)

        if action_type == 'click':
            selector = self._required_str(action, 'selector')
            timeout = self._remaining_seconds(start_time, timeout_ms)
            element = await tab.query(selector, timeout=timeout)
            await element.click()
            return

        if action_type == 'type':
            selector = self._required_str(action, 'selector')
            text = self._required_str(action, 'text')
            timeout = self._remaining_seconds(start_time, timeout_ms)
            element = await tab.query(selector, timeout=timeout)
            await element.insert_text(text)
            return

        if action_type == 'scroll':
            await tab.execute_script('window.scrollTo(0, document.body.scrollHeight)')
            return

        if action_type == 'wait':
            wait_for = action.get('for')
            if wait_for == 'selector':
                value = self._required_str(action, 'value')
                await tab.query(value, timeout=self._remaining_seconds(start_time, timeout_ms))
                return

            if wait_for == 'sleep':
                milliseconds = action.get('ms')
                if not isinstance(milliseconds, int) or milliseconds < 0:
                    raise InvalidPydollAction(
                        'wait sleep action requires non-negative integer "ms"'
                    )
                if milliseconds > self._remaining_ms(start_time, timeout_ms):
                    raise PydollTimeout('Pydoll timeout')
                await asyncio.sleep(milliseconds / 1000)
                return

            raise InvalidPydollAction('wait action requires "for" of "selector" or "sleep"')

        raise InvalidPydollAction(f'Unknown action type: {action_type}')

    @staticmethod
    def _required_str(action: dict[str, Any], key: str) -> str:
        value = action.get(key)
        if not isinstance(value, str) or not value:
            raise InvalidPydollAction(f'Action requires non-empty string "{key}"')
        return value

    @staticmethod
    def _remaining_ms(start_time: float, timeout_ms: int) -> int:
        elapsed_ms = int((monotonic() - start_time) * 1000)
        return timeout_ms - elapsed_ms

    def _remaining_seconds(self, start_time: float, timeout_ms: int) -> int:
        remaining_ms = self._remaining_ms(start_time, timeout_ms)
        if remaining_ms <= 0:
            raise PydollTimeout('Pydoll timeout')
        return (remaining_ms + 999) // 1000

    def _check_remaining_time(self, start_time: float, timeout_ms: int):
        if self._remaining_ms(start_time, timeout_ms) <= 0:
            raise PydollTimeout('Pydoll timeout')

    @staticmethod
    def _record_failure(request: Request, exc: Exception, actions: list[dict[str, Any]]):
        request.meta['pydoll_error'] = str(exc)
        logger.exception(
            'Pydoll request failed url=%s actions=%s error_type=%s',
            request.url,
            actions,
            type(exc).__name__,
        )

    def _retry_or_fail(
        self, request: Request, spider, exc: Exception, actions: list[dict[str, Any]]
    ):
        self._record_failure(request, exc, actions)
        retry_request: Optional[Request] = get_retry_request(
            request,
            spider=spider,
            reason=f'pydoll:{type(exc).__name__}',
        )
        if retry_request is not None:
            return retry_request

        if isinstance(exc, PydollTimeout):
            raise IgnoreRequest('Pydoll timeout') from exc

        raise IgnoreRequest(f'Pydoll retry exhausted: {exc}') from exc
