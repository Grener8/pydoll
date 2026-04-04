"""Tests for pydoll.serve.cli."""

import sys
import pytest
from unittest.mock import patch, AsyncMock, MagicMock


from pydoll.serve.cli import _build_parser, main


class TestParser:
    def test_serve_defaults(self):
        parser = _build_parser()
        args = parser.parse_args(['serve'])
        assert args.host == '0.0.0.0'
        assert args.port == 8000
        assert args.max_sessions == 10
        assert args.session_timeout == 300
        assert args.headless is True
        assert args.api_key is None

    def test_serve_custom_args(self):
        parser = _build_parser()
        args = parser.parse_args([
            'serve',
            '--host', '127.0.0.1',
            '--port', '9000',
            '--max-sessions', '5',
            '--session-timeout', '120',
            '--no-headless',
            '--api-key', 'mytoken',
        ])
        assert args.host == '127.0.0.1'
        assert args.port == 9000
        assert args.max_sessions == 5
        assert args.session_timeout == 120
        assert args.headless is False
        assert args.api_key == 'mytoken'

    def test_no_command_exits_zero(self):
        with patch('sys.argv', ['pydoll']):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 0

    def test_invalid_browser_options_exits(self):
        with patch('sys.argv', ['pydoll', 'serve', '--browser-options', 'not-json']):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1

    def test_serve_calls_run_server(self):
        with patch('pydoll.serve.cli.asyncio') as mock_asyncio:
            mock_asyncio.run = MagicMock()
            with patch('sys.argv', ['pydoll', 'serve']):
                main()
            mock_asyncio.run.assert_called_once()
