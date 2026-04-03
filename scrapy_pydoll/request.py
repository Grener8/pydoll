from __future__ import annotations

from typing import Any, Optional

from scrapy import Request


class PydollRequest(Request):
    def __init__(
        self,
        *args,
        actions: Optional[list[dict[str, Any]]] = None,
        timeout: Optional[int] = None,
        solve_captcha: Optional[bool] = None,
        pydoll: Optional[dict[str, Any]] = None,
        **kwargs,
    ):
        meta = dict(kwargs.pop('meta', {}) or {})
        existing = meta.get('pydoll')
        pydoll_meta = dict(existing) if isinstance(existing, dict) else {}
        has_config = False

        if pydoll:
            pydoll_meta.update(pydoll)
            has_config = True
        if actions is not None:
            pydoll_meta['actions'] = actions
            has_config = True
        if timeout is not None:
            pydoll_meta['timeout'] = timeout
            has_config = True
        if solve_captcha is not None:
            pydoll_meta['solve_captcha'] = solve_captcha
            has_config = True

        if has_config:
            meta['pydoll'] = pydoll_meta
        kwargs['meta'] = meta
        super().__init__(*args, **kwargs)
