import asyncio
from unittest.mock import AsyncMock

import pytest
from scrapy import Request, Spider
from scrapy.http import HtmlResponse

from scrapy_pydoll.middleware import PydollMiddleware


class _DummySpider(Spider):
    name = 'dummy'


class _DummyTab:
    def __init__(self):
        self.query = AsyncMock()
        self.execute_script = AsyncMock()
        self.enable_auto_solve_cloudflare_captcha = AsyncMock()
        self.disable_auto_solve_cloudflare_captcha = AsyncMock()
        self.set_cookies = AsyncMock()
        self.get_cookies = AsyncMock(return_value=[])
        self.go_to = AsyncMock()
        self.close = AsyncMock()
        self.page_source = '<html><body>ok</body></html>'


class _DummyBrowser:
    def __init__(self, tab):
        self._tab = tab
        self.start = AsyncMock(return_value=tab)
        self.new_tab = AsyncMock(return_value=tab)
        self.stop = AsyncMock()


@pytest.mark.asyncio
async def test_process_request_passthrough_when_not_enabled():
    middleware = PydollMiddleware(False, 1, {}, 30000, True)
    response = await middleware.process_request(Request('https://example.com'), _DummySpider())
    assert response is None


@pytest.mark.asyncio
async def test_process_request_passthrough_when_no_pydoll_meta():
    middleware = PydollMiddleware(True, 1, {}, 30000, True)
    response = await middleware.process_request(Request('https://example.com'), _DummySpider())
    assert response is None


@pytest.mark.asyncio
async def test_process_request_passthrough_when_pydoll_meta_empty_dict():
    middleware = PydollMiddleware(True, 1, {}, 30000, True)
    response = await middleware.process_request(
        Request('https://example.com', meta={'pydoll': {}}), _DummySpider()
    )
    assert response is None


@pytest.mark.asyncio
async def test_process_request_renders_html_and_set_cookie_headers():
    tab = _DummyTab()
    tab.get_cookies = AsyncMock(
        return_value=[
            {
                'name': 'sid',
                'value': 'abc',
                'domain': '.example.com',
                'path': '/',
                'expires': 0,
                'secure': True,
                'httpOnly': True,
                'sameSite': 'Lax',
            }
        ]
    )
    middleware = PydollMiddleware(True, 1, {}, 30000, True)
    middleware.browser = _DummyBrowser(tab)

    request = Request('https://example.com', meta={'pydoll': {'actions': []}})
    response = await middleware.process_request(request, _DummySpider())

    assert isinstance(response, HtmlResponse)
    assert response.text == '<html><body>ok</body></html>'
    assert response.headers.getlist('Set-Cookie')


@pytest.mark.asyncio
async def test_process_request_retries_on_timeout(monkeypatch):
    tab = _DummyTab()
    tab.go_to = AsyncMock(side_effect=asyncio.TimeoutError())

    middleware = PydollMiddleware(True, 1, {}, 30000, True)
    middleware.browser = _DummyBrowser(tab)

    retry_request = Request('https://retry.example.com', meta={'pydoll': {'actions': []}})

    def _fake_retry_request(request, spider, reason):
        return retry_request

    monkeypatch.setattr('scrapy_pydoll.middleware.get_retry_request', _fake_retry_request)

    request = Request('https://example.com', meta={'pydoll': {'actions': []}})
    result = await middleware.process_request(request, _DummySpider())

    assert result is retry_request
    assert 'pydoll_error' in request.meta


@pytest.mark.asyncio
async def test_actions_click_type_scroll_wait_selector_and_sleep():
    tab = _DummyTab()

    element = AsyncMock()
    tab.query = AsyncMock(return_value=element)

    middleware = PydollMiddleware(True, 1, {}, 30000, True)
    middleware.browser = _DummyBrowser(tab)

    request = Request(
        'https://example.com',
        meta={
            'pydoll': {
                'actions': [
                    {'type': 'click', 'selector': '#btn'},
                    {'type': 'type', 'selector': '#input', 'text': 'hello'},
                    {'type': 'scroll'},
                    {'type': 'wait', 'for': 'selector', 'value': '#ready'},
                    {'type': 'wait', 'for': 'sleep', 'ms': 1},
                ]
            }
        },
    )

    response = await middleware.process_request(request, _DummySpider())

    assert isinstance(response, HtmlResponse)
    assert tab.query.await_count == 3
    element.click.assert_awaited()
    element.insert_text.assert_awaited_with('hello')
    tab.execute_script.assert_awaited_with('window.scrollTo(0, document.body.scrollHeight)')
