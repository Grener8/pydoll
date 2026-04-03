"""
scrapy_pydoll/request.py

PydollRequest – a scrapy.Request subclass that pre-fills ``meta['pydoll']``
so callers never have to touch meta keys directly.
"""

from __future__ import annotations

from typing import Any

import scrapy


class PydollRequest(scrapy.Request):
    """A :class:`scrapy.Request` subclass that opts the request into the
    Pydoll rendering backend.

    All keyword arguments that are not listed below are forwarded unchanged
    to :class:`scrapy.Request`.

    Args:
        url: Target URL.
        actions: List of action dicts to execute after navigation.
            Each action follows the schema::

                {
                    "type": "click" | "type" | "scroll" | "wait",
                    "selector": str,   # CSS / XPath (for click, type)
                    "text": str,       # text to type  (for type)
                    "for": "selector" | "sleep",  # wait variant
                    "value": str,      # selector to wait for (for=selector)
                    "ms": int          # sleep duration in ms (for=sleep)
                }

        timeout: Per-request timeout in **milliseconds**.  Overrides the
            ``PYDOLL_DEFAULT_TIMEOUT`` setting for this request only.
        solve_captcha: Whether to attempt automatic Cloudflare Turnstile
            solving for this request.  Overrides
            ``PYDOLL_ENABLE_CLOUDFLARE_SOLVER``.
        **kwargs: Forwarded to :class:`scrapy.Request`.
    """

    def __init__(
        self,
        url: str,
        *,
        actions: list[dict[str, Any]] | None = None,
        timeout: int | None = None,
        solve_captcha: bool | None = None,
        **kwargs: Any,
    ) -> None:
        meta: dict[str, Any] = kwargs.pop('meta', {}) or {}
        pydoll_meta: dict[str, Any] = {}
        if actions is not None:
            pydoll_meta['actions'] = actions
        if timeout is not None:
            pydoll_meta['timeout'] = timeout
        if solve_captcha is not None:
            pydoll_meta['solve_captcha'] = solve_captcha
        meta['pydoll'] = pydoll_meta
        super().__init__(url, meta=meta, **kwargs)
