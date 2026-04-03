"""Unit tests for scrapy_pydoll.request.PydollRequest."""

import pytest
import scrapy

from scrapy_pydoll.request import PydollRequest


class TestPydollRequest:
    def test_sets_pydoll_meta_key(self):
        req = PydollRequest('http://example.com')
        assert 'pydoll' in req.meta

    def test_empty_meta_by_default(self):
        req = PydollRequest('http://example.com')
        assert req.meta['pydoll'] == {}

    def test_actions_stored_in_meta(self):
        actions = [{'type': 'scroll'}]
        req = PydollRequest('http://example.com', actions=actions)
        assert req.meta['pydoll']['actions'] == actions

    def test_timeout_stored_in_meta(self):
        req = PydollRequest('http://example.com', timeout=5000)
        assert req.meta['pydoll']['timeout'] == 5000

    def test_solve_captcha_stored_in_meta(self):
        req = PydollRequest('http://example.com', solve_captcha=False)
        assert req.meta['pydoll']['solve_captcha'] is False

    def test_all_pydoll_kwargs(self):
        actions = [{'type': 'click', 'selector': '#btn'}]
        req = PydollRequest(
            'http://example.com',
            actions=actions,
            timeout=10000,
            solve_captcha=True,
        )
        pydoll = req.meta['pydoll']
        assert pydoll['actions'] == actions
        assert pydoll['timeout'] == 10000
        assert pydoll['solve_captcha'] is True

    def test_existing_meta_is_preserved(self):
        req = PydollRequest('http://example.com', meta={'custom_key': 'value'})
        assert req.meta['custom_key'] == 'value'
        assert 'pydoll' in req.meta

    def test_is_scrapy_request_subclass(self):
        req = PydollRequest('http://example.com')
        assert isinstance(req, scrapy.Request)

    def test_callback_forwarded(self):
        def my_callback(response):
            pass

        req = PydollRequest('http://example.com', callback=my_callback)
        assert req.callback is my_callback

    def test_none_actions_not_in_meta(self):
        req = PydollRequest('http://example.com', actions=None)
        assert 'actions' not in req.meta['pydoll']

    def test_none_timeout_not_in_meta(self):
        req = PydollRequest('http://example.com', timeout=None)
        assert 'timeout' not in req.meta['pydoll']

    def test_none_solve_captcha_not_in_meta(self):
        req = PydollRequest('http://example.com', solve_captcha=None)
        assert 'solve_captcha' not in req.meta['pydoll']

    def test_pydoll_meta_not_overwritten_when_none_provided(self):
        """meta passed by caller should be augmented, not replaced."""
        req = PydollRequest('http://example.com', meta={'dont_redirect': True})
        assert req.meta['dont_redirect'] is True
        assert req.meta['pydoll'] == {}
