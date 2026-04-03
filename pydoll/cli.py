from __future__ import annotations

import argparse
import asyncio

from pydoll.service.http_service import run_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='pydoll')
    subparsers = parser.add_subparsers(dest='command', required=True)

    serve_parser = subparsers.add_parser('serve', help='Start pydoll HTTP service')
    serve_parser.add_argument('--host', default='0.0.0.0')
    serve_parser.add_argument('--port', type=int, default=8000)
    serve_parser.add_argument('--max-sessions', type=int, default=5)
    serve_parser.add_argument('--session-timeout', type=int, default=300)
    serve_parser.add_argument('--browser-options', type=str, default=None)
    serve_parser.add_argument('--headless', action='store_true', default=False)
    serve_parser.add_argument('--api-key', type=str, default=None)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == 'serve':
        asyncio.run(run_service(args))


if __name__ == '__main__':
    main()
