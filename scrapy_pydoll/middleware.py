"""
scrapy_pydoll/middleware.py

Scrapy downloader middleware that integrates Pydoll as an optional rendering
backend.  Requests opt-in via ``request.meta['pydoll']``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import scrapy
from scrapy import signals
from scrapy.exceptions import IgnoreRequest, NotConfigured
from scrapy.http import HtmlResponse

from pydoll.browser import Chrome
from pydoll.browser.options import ChromiumOptions
from pydoll.exceptions import (
    ElementNotFound,
    NavigationError,
    PageLoadTimeout,
    WaitElementTimeout,
)
from pydoll.protocol.network.types import CookieParam

if TYPE_CHECKING:
    from scrapy import Spider
    from scrapy.crawler import Crawler

    from pydoll.browser.tab import Tab

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions that should trigger Scrapy's RetryMiddleware
# ---------------------------------------------------------------------------
_RETRY_EXCEPTIONS = (NavigationError, PageLoadTimeout, ConnectionError, IOError)

# ---------------------------------------------------------------------------
# Exceptions that should cause the request to be dropped silently
# ---------------------------------------------------------------------------
_IGNORE_EXCEPTIONS = (ElementNotFound, WaitElementTimeout)


class PydollMiddleware:
    """Scrapy downloader middleware that renders pages via Pydoll.

    **Settings**

    ``PYDOLL_ENABLED`` (bool, default ``True``)
        Master switch.  Set to ``False`` to disable the middleware entirely
        without removing it from ``DOWNLOADER_MIDDLEWARES``.

    ``PYDOLL_CONCURRENCY`` (int, default ``4``)
        Maximum number of browser tabs open simultaneously.

    ``PYDOLL_BROWSER_OPTIONS`` (dict, default ``{}``)
        Extra options forwarded to :class:`~pydoll.browser.options.ChromiumOptions`.
        Supported keys:

        * ``headless`` (bool) – run browser in headless mode.
        * ``arguments`` (list[str]) – additional CLI flags.
        * ``binary_location`` (str) – path to the browser binary.

    ``PYDOLL_DEFAULT_TIMEOUT`` (int, default ``30000``)
        Default per-request timeout in **milliseconds**.

    ``PYDOLL_ENABLE_CLOUDFLARE_SOLVER`` (bool, default ``True``)
        Attempt automatic Cloudflare Turnstile bypass on every Pydoll request
        unless overridden per-request.

    **Recommended middleware ordering**::

        DOWNLOADER_MIDDLEWARES = {
            "scrapy.downloadermiddlewares.cookies.CookiesMiddleware": 700,
            "scrapy_pydoll.middleware.PydollMiddleware": 750,
            "scrapy.downloadermiddlewares.retry.RetryMiddleware": 800,
        }

    **Usage**

    Mark any request for browser rendering::

        scrapy.Request(url, meta={"pydoll": {}})

    Or use the helper class::

        from scrapy_pydoll import PydollRequest
        PydollRequest(url, actions=[{"type": "scroll"}])
    """

    def __init__(self, crawler: 'Crawler') -> None:
        settings = crawler.settings
        self._enabled: bool = settings.getbool('PYDOLL_ENABLED', True)
        if not self._enabled:
            raise NotConfigured('PYDOLL_ENABLED is False')

        self._concurrency: int = settings.getint('PYDOLL_CONCURRENCY', 4)
        self._browser_options: dict[str, Any] = settings.getdict(
            'PYDOLL_BROWSER_OPTIONS', {}
        )
        self._default_timeout: int = settings.getint('PYDOLL_DEFAULT_TIMEOUT', 30000)
        self._solve_captcha_default: bool = settings.getbool(
            'PYDOLL_ENABLE_CLOUDFLARE_SOLVER', True
        )

        self._browser: Chrome | None = None
        self._semaphore: asyncio.Semaphore | None = None

    # ------------------------------------------------------------------
    # Scrapy lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def from_crawler(cls, crawler: 'Crawler') -> 'PydollMiddleware':
        obj = cls(crawler)
        crawler.signals.connect(obj.spider_opened, signal=signals.spider_opened)
        crawler.signals.connect(obj.spider_closed, signal=signals.spider_closed)
        return obj

    async def spider_opened(self, spider: 'Spider') -> None:
        options = self._build_options()
        self._browser = Chrome(options=options)
        await self._browser.start()
        self._semaphore = asyncio.Semaphore(self._concurrency)
        logger.info(
            'PydollMiddleware: browser started (concurrency=%d)', self._concurrency
        )

    async def spider_closed(self, spider: 'Spider') -> None:
        if self._browser is not None:
            try:
                await self._browser.stop()
            except Exception:
                logger.debug('PydollMiddleware: error stopping browser', exc_info=True)
            finally:
                self._browser = None
        logger.info('PydollMiddleware: browser stopped')

    # ------------------------------------------------------------------
    # Downloader middleware interface
    # ------------------------------------------------------------------

    async def process_request(
        self, request: scrapy.Request, spider: 'Spider'
    ) -> HtmlResponse | None:
        if 'pydoll' not in request.meta:
            return None  # not a Pydoll request – pass through

        if self._browser is None or self._semaphore is None:
            raise IgnoreRequest('PydollMiddleware: browser not initialised')

        async with self._semaphore:
            return await self._render(request, spider)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_options(self) -> ChromiumOptions:
        options = ChromiumOptions()
        opts = self._browser_options
        if 'headless' in opts:
            options.headless = bool(opts['headless'])
        if opts.get('binary_location'):
            options.binary_location = opts['binary_location']
        for arg in opts.get('arguments', []):
            options.add_argument(arg)
        return options

    async def _render(
        self, request: scrapy.Request, spider: 'Spider'
    ) -> HtmlResponse:
        pydoll_meta: dict[str, Any] = request.meta.get('pydoll') or {}
        timeout_ms: int = int(pydoll_meta.get('timeout', self._default_timeout))
        actions: list[dict[str, Any]] = pydoll_meta.get('actions', [])
        solve_captcha: bool = pydoll_meta.get('solve_captcha', self._solve_captcha_default)

        tab: 'Tab | None' = None
        start_time = time.monotonic()

        logger.info(
            'PydollMiddleware: start url=%s actions=%d timeout=%dms',
            request.url,
            len(actions),
            timeout_ms,
        )

        try:
            assert self._browser is not None  # mypy / type checker
            tab = await self._browser.new_tab()

            # --- inject cookies -------------------------------------------
            scrapy_cookies = self._extract_request_cookies(request)
            if scrapy_cookies:
                await tab.set_cookies(scrapy_cookies)

            # --- optional captcha solver ------------------------------------
            if solve_captcha:
                await tab.enable_auto_solve_cloudflare_captcha()

            # --- navigate ---------------------------------------------------
            remaining_ms = self._remaining(start_time, timeout_ms)
            timeout_sec = max(1, int(remaining_ms / 1000))
            try:
                await tab.go_to(request.url, timeout=timeout_sec)
            except _RETRY_EXCEPTIONS as exc:
                logger.warning(
                    'PydollMiddleware: navigation error url=%s error=%s',
                    request.url,
                    exc,
                )
                request.meta['pydoll_error'] = exc
                raise IOError(str(exc)) from exc

            # --- execute actions --------------------------------------------
            if actions:
                await self._execute_actions(tab, actions, start_time, timeout_ms)

            # --- extract HTML -----------------------------------------------
            html: str = await tab.page_source

            # --- extract cookies and build response -------------------------
            pydoll_cookies = await tab.get_cookies()
            response = self._build_response(request, html, pydoll_cookies)

            elapsed = (time.monotonic() - start_time) * 1000
            logger.info(
                'PydollMiddleware: done url=%s elapsed=%.0fms', request.url, elapsed
            )
            return response

        except IgnoreRequest:
            raise
        except IOError:
            raise
        except _IGNORE_EXCEPTIONS as exc:
            logger.warning(
                'PydollMiddleware: non-retryable failure url=%s error=%s',
                request.url,
                exc,
            )
            request.meta['pydoll_error'] = exc
            raise IgnoreRequest(f'Pydoll: {exc}') from exc
        except Exception as exc:
            logger.error(
                'PydollMiddleware: unexpected error url=%s error=%s',
                request.url,
                exc,
                exc_info=True,
            )
            request.meta['pydoll_error'] = exc
            raise IOError(f'Pydoll unexpected error: {exc}') from exc
        finally:
            if tab is not None:
                try:
                    await tab.close()
                except Exception:
                    logger.debug(
                        'PydollMiddleware: error closing tab', exc_info=True
                    )

    # ------------------------------------------------------------------
    # Action engine
    # ------------------------------------------------------------------

    async def _execute_actions(
        self,
        tab: 'Tab',
        actions: list[dict[str, Any]],
        start_time: float,
        timeout_ms: int,
    ) -> None:
        for idx, action in enumerate(actions):
            remaining_ms = self._remaining(start_time, timeout_ms)
            if remaining_ms <= 0:
                raise IOError('Pydoll timeout')

            action_type = action.get('type')
            logger.debug(
                'PydollMiddleware: action[%d] type=%s', idx, action_type
            )

            if action_type == 'click':
                selector: str = action['selector']
                timeout_sec = max(1, int(remaining_ms / 1000))
                el = await tab.query(selector, timeout=timeout_sec)
                if el is None:
                    raise ElementNotFound(f'Selector not found: {selector}')
                await el.click()

            elif action_type == 'type':
                selector = action['selector']
                text: str = action.get('text', '')
                timeout_sec = max(1, int(remaining_ms / 1000))
                el = await tab.query(selector, timeout=timeout_sec)
                if el is None:
                    raise ElementNotFound(f'Selector not found: {selector}')
                await el.insert_text(text)

            elif action_type == 'scroll':
                await tab.evaluate('window.scrollTo(0, document.body.scrollHeight)')

            elif action_type == 'wait':
                await PydollMiddleware._execute_wait_action(tab, action, remaining_ms)

            else:
                raise IgnoreRequest(f'Pydoll: unknown action type: {action_type!r}')

    @staticmethod
    async def _execute_wait_action(
        tab: 'Tab', action: dict[str, Any], remaining_ms: float
    ) -> None:
        wait_for: str = action.get('for', '')
        if wait_for == 'selector':
            value: str = action['value']
            timeout_sec = max(1, int(remaining_ms / 1000))
            el = await tab.query(value, timeout=timeout_sec)
            if el is None:
                raise ElementNotFound(f'Selector not found: {value}')
        elif wait_for == 'sleep':
            ms: int = action.get('ms', 0)
            await asyncio.sleep(ms / 1000)
        else:
            raise IgnoreRequest(
                f"Pydoll: unknown 'for' value in wait action: {wait_for!r}"
            )

    # ------------------------------------------------------------------
    # Cookie helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_request_cookies(request: scrapy.Request) -> list[CookieParam]:
        """Convert Scrapy request cookies (from the Cookie header) to Pydoll format."""
        raw: bytes = request.headers.get(b'Cookie', b'')
        if not raw:
            return []

        domain = urlparse(request.url).hostname or ''
        cookies: list[CookieParam] = []
        for raw_part in raw.decode('latin-1').split(';'):
            part = raw_part.strip()
            if not part:
                continue
            if '=' in part:
                name, _, value = part.partition('=')
                name = name.strip()
                value = value.strip()
            else:
                name = part
                value = ''
            if name:
                cookies.append(CookieParam(name=name, value=value, domain=domain))
        return cookies

    @staticmethod
    def _build_response(
        request: scrapy.Request,
        html: str,
        pydoll_cookies: list[dict[str, Any]],
    ) -> HtmlResponse:
        """Build a Scrapy HtmlResponse, encoding pydoll cookies as Set-Cookie headers."""
        headers: dict[bytes, list[bytes]] = {}
        set_cookie_headers: list[bytes] = []
        for c in pydoll_cookies:
            name = c.get('name', '')
            value = c.get('value', '')
            domain = c.get('domain', '')
            path = c.get('path', '/')
            parts = [f'{name}={value}']
            if domain:
                parts.append(f'Domain={domain}')
            if path:
                parts.append(f'Path={path}')
            set_cookie_headers.append('; '.join(parts).encode('latin-1'))
        if set_cookie_headers:
            headers[b'Set-Cookie'] = set_cookie_headers

        return HtmlResponse(
            url=request.url,
            body=html,
            encoding='utf-8',
            request=request,
            headers=headers,
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @staticmethod
    def _remaining(start_time: float, timeout_ms: int) -> float:
        """Return remaining milliseconds given a start time and total timeout."""
        elapsed_ms = (time.monotonic() - start_time) * 1000
        return timeout_ms - elapsed_ms
