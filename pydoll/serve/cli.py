"""CLI entry point for ``pydoll serve``."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional

from pydoll.serve.server import run_server


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='pydoll',
        description='pydoll CLI',
    )
    sub = parser.add_subparsers(dest='command')

    serve = sub.add_parser('serve', help='Start the pydoll HTTP server')
    serve.add_argument('--host', default='0.0.0.0', help='Bind address (default: 0.0.0.0)')
    serve.add_argument('--port', type=int, default=8000, help='TCP port (default: 8000)')
    serve.add_argument(
        '--max-sessions',
        type=int,
        default=10,
        dest='max_sessions',
        help='Maximum number of concurrent sessions (default: 10)',
    )
    serve.add_argument(
        '--session-timeout',
        type=float,
        default=300,
        dest='session_timeout',
        help='Session inactivity timeout in seconds (default: 300)',
    )
    serve.add_argument(
        '--browser-options',
        default=None,
        dest='browser_options',
        help='JSON string with additional browser options (e.g. {"arguments": ["--no-sandbox"]})',
    )
    serve.add_argument(
        '--headless',
        action='store_true',
        default=True,
        help='Run browser in headless mode (default: true)',
    )
    serve.add_argument(
        '--no-headless',
        action='store_false',
        dest='headless',
        help='Run browser in non-headless mode',
    )
    serve.add_argument(
        '--api-key',
        default=None,
        dest='api_key',
        help='Optional Bearer token required for all API requests',
    )
    serve.add_argument(
        '--log-level',
        default='INFO',
        dest='log_level',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging level (default: INFO)',
    )
    return parser


def _run_serve(args: argparse.Namespace) -> None:
    """Parse arguments and start the server."""
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format='%(asctime)s %(levelname)-8s %(name)s: %(message)s',
    )

    browser_options: Optional[dict] = None
    if args.browser_options:
        try:
            browser_options = json.loads(args.browser_options)
        except json.JSONDecodeError as exc:
            print(f'Error: --browser-options is not valid JSON: {exc}', file=sys.stderr)
            sys.exit(1)

    asyncio.run(
        run_server(
            host=args.host,
            port=args.port,
            headless=args.headless,
            max_sessions=args.max_sessions,
            session_timeout=args.session_timeout,
            browser_options=browser_options,
            api_key=args.api_key,
        )
    )


def main() -> None:
    """Main entry point for the ``pydoll`` CLI."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == 'serve':
        _run_serve(args)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == '__main__':
    main()
