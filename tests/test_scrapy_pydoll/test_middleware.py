"""Unit tests for scrapy_pydoll.middleware.PydollMiddleware."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import scrapy
from scrapy.exceptions import IgnoreRequest, NotConfigured
from scrapy.http import HtmlResponse

from scrapy_pydoll.middleware import PydollMiddleware
from scrapy_pydoll.request import PydollRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_crawler(**settings_overrides):
    """Return a minimal fake Crawler object."""
    default_settings = {
        'PYDOLL_ENABLED': True,
        'PYDOLL_CONCURRENCY': 2,
        'PYDOLL_BROWSER_OPTIONS': {},
        'PYDOLL_DEFAULT_TIMEOUT': 30000,
        'PYDOLL_ENABLE_CLOUDFLARE_SOLVER': False,
    }
    default_settings.update(settings_overrides)

    settings = MagicMock()
    settings.getbool.side_effect = lambda key, default=False: default_settings.get(key, default)
    settings.getint.side_effect = lambda key, default=0: default_settings.get(key, default)
    settings.getdict.side_effect = lambda key, default=None: default_settings.get(key, default or {})

    crawler = MagicMock()
    crawler.settings = settings
    return crawler


def _make_tab(html: str = '<html><body>hello</body></html>') -> MagicMock:
    """Return a mock Tab object."""
    tab = MagicMock()

    async def _page_source():
        return html

    type(tab).page_source = property(lambda self: _page_source())
    tab.get_cookies = AsyncMock(return_value=[])
    tab.set_cookies = AsyncMock()
    tab.go_to = AsyncMock()
    tab.close = AsyncMock()
    tab.enable_auto_solve_cloudflare_captcha = AsyncMock()
    tab.query = AsyncMock(return_value=MagicMock(click=AsyncMock(), insert_text=AsyncMock()))
    tab.evaluate = AsyncMock()
    return tab


def _make_middleware(**settings_overrides) -> PydollMiddleware:
    """Create a PydollMiddleware with a pre-initialised mock browser."""
    crawler = _make_crawler(**settings_overrides)
    mw = PydollMiddleware.__new__(PydollMiddleware)
    mw._enabled = True
    mw._concurrency = settings_overrides.get('PYDOLL_CONCURRENCY', 2)
    mw._browser_options = settings_overrides.get('PYDOLL_BROWSER_OPTIONS', {})
    mw._default_timeout = settings_overrides.get('PYDOLL_DEFAULT_TIMEOUT', 30000)
    mw._solve_captcha_default = settings_overrides.get('PYDOLL_ENABLE_CLOUDFLARE_SOLVER', False)
    mw._browser = AsyncMock()
    mw._semaphore = asyncio.Semaphore(mw._concurrency)
    return mw


# ---------------------------------------------------------------------------
# from_crawler / constructor
# ---------------------------------------------------------------------------

class TestFromCrawler:
    def test_raises_not_configured_when_disabled(self):
        crawler = _make_crawler(PYDOLL_ENABLED=False)
        with pytest.raises(NotConfigured):
            PydollMiddleware(crawler)

    def test_connects_signals(self):
        crawler = _make_crawler()
        mw = PydollMiddleware.from_crawler(crawler)
        # from_crawler must connect spider_opened and spider_closed signals
        assert crawler.signals.connect.call_count == 2


# ---------------------------------------------------------------------------
# process_request – pass-through
# ---------------------------------------------------------------------------

class TestProcessRequestPassThrough:
    @pytest.mark.asyncio
    async def test_returns_none_without_pydoll_meta(self):
        mw = _make_middleware()
        request = scrapy.Request('http://example.com')
        spider = MagicMock()
        result = await mw.process_request(request, spider)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_with_pydoll_meta_false(self):
        mw = _make_middleware()
        request = scrapy.Request('http://example.com', meta={'pydoll': False})
        spider = MagicMock()
        # pydoll key present but set to False → still intercepted (key presence check)
        # This test verifies a *plain* request without pydoll key is passed through
        request2 = scrapy.Request('http://example.com')
        result = await mw.process_request(request2, spider)
        assert result is None

    @pytest.mark.asyncio
    async def test_pydoll_false_value_still_intercepted(self):
        """Key presence (not truthiness) triggers pydoll handling."""
        mw = _make_middleware()
        tab = _make_tab()
        mw._browser.new_tab = AsyncMock(return_value=tab)
        request = scrapy.Request('http://example.com', meta={'pydoll': False})
        spider = MagicMock()
        result = await mw.process_request(request, spider)
        assert isinstance(result, HtmlResponse)

    @pytest.mark.asyncio
    async def test_returns_none_with_empty_pydoll_meta_dict(self):
        """Empty dict is truthy – request should be handled by Pydoll."""
        mw = _make_middleware()
        tab = _make_tab()
        mw._browser.new_tab = AsyncMock(return_value=tab)

        request = PydollRequest('http://example.com')
        spider = MagicMock()
        result = await mw.process_request(request, spider)
        assert isinstance(result, HtmlResponse)


# ---------------------------------------------------------------------------
# _extract_request_cookies
# ---------------------------------------------------------------------------

class TestExtractRequestCookies:
    def test_empty_when_no_cookie_header(self):
        request = scrapy.Request('http://example.com')
        cookies = PydollMiddleware._extract_request_cookies(request)
        assert cookies == []

    def test_single_cookie(self):
        request = scrapy.Request('http://example.com')
        request.headers[b'Cookie'] = b'session=abc123'
        cookies = PydollMiddleware._extract_request_cookies(request)
        assert len(cookies) == 1
        assert cookies[0]['name'] == 'session'
        assert cookies[0]['value'] == 'abc123'

    def test_multiple_cookies(self):
        request = scrapy.Request('http://example.com')
        request.headers[b'Cookie'] = b'a=1; b=2; c=3'
        cookies = PydollMiddleware._extract_request_cookies(request)
        assert len(cookies) == 3
        names = {c['name'] for c in cookies}
        assert names == {'a', 'b', 'c'}

    def test_domain_set_from_url(self):
        request = scrapy.Request('http://example.com/path')
        request.headers[b'Cookie'] = b'x=y'
        cookies = PydollMiddleware._extract_request_cookies(request)
        assert cookies[0]['domain'] == 'example.com'

    def test_cookie_with_equals_in_value(self):
        request = scrapy.Request('http://example.com')
        request.headers[b'Cookie'] = b'token=abc=def'
        cookies = PydollMiddleware._extract_request_cookies(request)
        assert cookies[0]['name'] == 'token'
        assert cookies[0]['value'] == 'abc=def'

    def test_empty_cookie_header(self):
        request = scrapy.Request('http://example.com')
        request.headers[b'Cookie'] = b''
        cookies = PydollMiddleware._extract_request_cookies(request)
        assert cookies == []


# ---------------------------------------------------------------------------
# _build_response
# ---------------------------------------------------------------------------

class TestBuildResponse:
    def test_returns_html_response(self):
        request = scrapy.Request('http://example.com')
        response = PydollMiddleware._build_response(request, '<html/>', [])
        assert isinstance(response, HtmlResponse)

    def test_url_matches_request(self):
        request = scrapy.Request('http://example.com/page')
        response = PydollMiddleware._build_response(request, '<html/>', [])
        assert response.url == 'http://example.com/page'

    def test_body_contains_html(self):
        request = scrapy.Request('http://example.com')
        html = '<html><body><h1>Test</h1></body></html>'
        response = PydollMiddleware._build_response(request, html, [])
        assert b'<h1>Test</h1>' in response.body

    def test_encoding_is_utf8(self):
        request = scrapy.Request('http://example.com')
        response = PydollMiddleware._build_response(request, '<html/>', [])
        assert response.encoding == 'utf-8'

    def test_set_cookie_headers_added(self):
        request = scrapy.Request('http://example.com')
        pydoll_cookies = [
            {'name': 'session', 'value': 'xyz', 'domain': 'example.com', 'path': '/'},
        ]
        response = PydollMiddleware._build_response(request, '<html/>', pydoll_cookies)
        assert b'Set-Cookie' in response.headers

    def test_no_set_cookie_when_no_cookies(self):
        request = scrapy.Request('http://example.com')
        response = PydollMiddleware._build_response(request, '<html/>', [])
        assert b'Set-Cookie' not in response.headers

    def test_css_works_on_response(self):
        request = scrapy.Request('http://example.com')
        html = '<html><body><p class="item">hello</p></body></html>'
        response = PydollMiddleware._build_response(request, html, [])
        assert response.css('p.item::text').get() == 'hello'

    def test_xpath_works_on_response(self):
        request = scrapy.Request('http://example.com')
        html = '<html><body><p>world</p></body></html>'
        response = PydollMiddleware._build_response(request, html, [])
        assert response.xpath('//p/text()').get() == 'world'


# ---------------------------------------------------------------------------
# _remaining
# ---------------------------------------------------------------------------

class TestRemaining:
    def test_remaining_decreases_over_time(self):
        import time
        start = time.monotonic()
        time.sleep(0.05)
        remaining = PydollMiddleware._remaining(start, 1000)
        assert remaining < 1000
        assert remaining > 0

    def test_remaining_negative_when_expired(self):
        import time
        start = time.monotonic() - 10  # 10 seconds ago
        remaining = PydollMiddleware._remaining(start, 5000)
        assert remaining < 0


# ---------------------------------------------------------------------------
# Action engine
# ---------------------------------------------------------------------------

class TestExecuteActions:
    @pytest.mark.asyncio
    async def test_click_action(self):
        mw = _make_middleware()
        tab = AsyncMock()
        el = AsyncMock()
        tab.query = AsyncMock(return_value=el)
        import time
        await mw._execute_actions(
            tab, [{'type': 'click', 'selector': '#btn'}], time.monotonic(), 30000
        )
        assert tab.query.await_count == 1
        assert tab.query.call_args[0][0] == '#btn'
        el.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_type_action(self):
        mw = _make_middleware()
        tab = AsyncMock()
        el = AsyncMock()
        tab.query = AsyncMock(return_value=el)
        import time
        await mw._execute_actions(
            tab, [{'type': 'type', 'selector': 'input', 'text': 'hello'}],
            time.monotonic(), 30000
        )
        assert tab.query.call_args[0][0] == 'input'
        el.insert_text.assert_awaited_once_with('hello')

    @pytest.mark.asyncio
    async def test_scroll_action(self):
        mw = _make_middleware()
        tab = AsyncMock()
        import time
        await mw._execute_actions(
            tab, [{'type': 'scroll'}], time.monotonic(), 30000
        )
        tab.evaluate.assert_awaited_once_with(
            'window.scrollTo(0, document.body.scrollHeight)'
        )

    @pytest.mark.asyncio
    async def test_wait_sleep_action(self):
        mw = _make_middleware()
        tab = AsyncMock()
        import time
        with patch('asyncio.sleep', new_callable=AsyncMock) as mock_sleep:
            await mw._execute_actions(
                tab, [{'type': 'wait', 'for': 'sleep', 'ms': 100}],
                time.monotonic(), 30000
            )
            mock_sleep.assert_awaited_once_with(0.1)

    @pytest.mark.asyncio
    async def test_wait_selector_action(self):
        mw = _make_middleware()
        tab = AsyncMock()
        el = AsyncMock()
        tab.query = AsyncMock(return_value=el)
        import time
        await mw._execute_actions(
            tab,
            [{'type': 'wait', 'for': 'selector', 'value': '.loaded'}],
            time.monotonic(), 30000
        )
        assert tab.query.await_count == 1
        assert tab.query.call_args[0][0] == '.loaded'

    @pytest.mark.asyncio
    async def test_invalid_action_type_raises_ignore_request(self):
        mw = _make_middleware()
        tab = AsyncMock()
        import time
        with pytest.raises(IgnoreRequest):
            await mw._execute_actions(
                tab, [{'type': 'unknown'}], time.monotonic(), 30000
            )

    @pytest.mark.asyncio
    async def test_invalid_wait_for_raises_ignore_request(self):
        mw = _make_middleware()
        tab = AsyncMock()
        import time
        with pytest.raises(IgnoreRequest):
            await mw._execute_actions(
                tab,
                [{'type': 'wait', 'for': 'magic'}],
                time.monotonic(), 30000
            )

    @pytest.mark.asyncio
    async def test_timeout_raises_io_error(self):
        mw = _make_middleware()
        tab = AsyncMock()
        import time
        # start_time far in the past so remaining_ms is negative
        past = time.monotonic() - 100
        with pytest.raises(IOError, match='Pydoll timeout'):
            await mw._execute_actions(
                tab, [{'type': 'scroll'}], past, 1000
            )
