"""Tests for pydoll.serve.models."""

import pytest
from pydantic import ValidationError

from pydoll.serve.models import (
    Action,
    ActionsRequest,
    CookieParam,
    CreateSessionRequest,
    ErrorResponse,
    ExtractionField,
    ExtractionModel,
    ExtractRequest,
    NavigateRequest,
    SessionOptions,
    SetCookiesRequest,
)


class TestSessionOptions:
    def test_defaults(self):
        opts = SessionOptions()
        assert opts.headless is True
        assert opts.proxy is None
        assert opts.geolocation is None
        assert opts.extra_args == []

    def test_custom_values(self):
        opts = SessionOptions(headless=False, proxy='socks5://proxy:1080', geolocation='US')
        assert opts.headless is False
        assert opts.proxy == 'socks5://proxy:1080'
        assert opts.geolocation == 'US'


class TestCreateSessionRequest:
    def test_empty_body(self):
        req = CreateSessionRequest()
        assert isinstance(req.options, SessionOptions)

    def test_with_options(self):
        req = CreateSessionRequest(options={'headless': False})
        assert req.options.headless is False


class TestNavigateRequest:
    def test_required_url(self):
        with pytest.raises(ValidationError):
            NavigateRequest()

    def test_defaults(self):
        req = NavigateRequest(url='https://example.com')
        assert req.timeout == 30000
        assert req.wait_until == 'load'

    def test_custom(self):
        req = NavigateRequest(url='https://example.com', timeout=5000, wait_until='networkidle')
        assert req.timeout == 5000
        assert req.wait_until == 'networkidle'


class TestAction:
    def test_click_action(self):
        action = Action(type='click', selector='#btn')
        assert action.type == 'click'
        assert action.selector == '#btn'

    def test_type_action(self):
        action = Action(type='type', selector='input', text='hello')
        assert action.text == 'hello'

    def test_evaluate_action(self):
        action = Action(type='evaluate', script='return 1+1')
        assert action.script == 'return 1+1'

    def test_keypress_action(self):
        action = Action(type='keypress', key='Enter')
        assert action.key == 'Enter'

    def test_sleep_action(self):
        action = Action(type='sleep', ms=500)
        assert action.ms == 500

    def test_wait_action_for_alias(self):
        action = Action(**{'type': 'wait', 'for': '#element'})
        assert action.wait_for == '#element'

    def test_invalid_type(self):
        with pytest.raises(ValidationError):
            Action(type='unknown_action')


class TestActionsRequest:
    def test_default_continue_on_error(self):
        req = ActionsRequest(actions=[Action(type='sleep', ms=10)])
        assert req.continue_on_error is False

    def test_continue_on_error_true(self):
        req = ActionsRequest(actions=[], continue_on_error=True)
        assert req.continue_on_error is True


class TestErrorResponse:
    def test_fields(self):
        err = ErrorResponse(error='Not found', type='SessionNotFound', session_id='abc-123')
        assert err.error == 'Not found'
        assert err.type == 'SessionNotFound'
        assert err.session_id == 'abc-123'


class TestSetCookiesRequest:
    def test_cookie_params(self):
        req = SetCookiesRequest(cookies=[{'name': 'session', 'value': 'xyz'}])
        assert len(req.cookies) == 1
        assert req.cookies[0].name == 'session'
