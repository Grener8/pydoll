from scrapy_pydoll import PydollRequest


def test_pydoll_request_wraps_meta():
    request = PydollRequest(
        url='https://example.com',
        actions=[{'type': 'scroll'}],
        timeout=15000,
        solve_captcha=True,
    )

    assert request.meta['pydoll']['actions'] == [{'type': 'scroll'}]
    assert request.meta['pydoll']['timeout'] == 15000
    assert request.meta['pydoll']['solve_captcha'] is True
