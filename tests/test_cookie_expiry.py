import logging
from unittest.mock import MagicMock, patch

import pytest

import crawl
import weibo_im.crawler as crawler_module


def _response(data, url="https://api.weibo.com/webim/test.json"):
    resp = MagicMock()
    resp.url = url
    resp.json.return_value = data
    resp.raise_for_status = MagicMock()
    return resp


def test_contacts_raises_for_explicit_cookie_error_21301():
    session = MagicMock()
    resp = _response({
        "error_code": 21301,
        "error": "Auth failed, Cookie expires or invalid.",
    })
    with patch.object(crawler_module, "_request_with_retry", return_value=resp):
        with pytest.raises(crawler_module.CookieExpiredError):
            crawler_module.fetch_contacts(session)


@pytest.mark.parametrize("data", [
    {"contacts": []},
    {"error_code": 10012, "error": "服务异常"},
])
def test_contacts_empty_or_service_error_is_not_cookie_expiry(data):
    session = MagicMock()
    with patch.object(
        crawler_module,
        "_request_with_retry",
        return_value=_response(data),
    ):
        assert crawler_module.fetch_contacts(session) == []


def test_messages_raises_for_explicit_cookie_error_21301():
    session = MagicMock()
    resp = _response({
        "error_code": 21301,
        "error": "Auth failed, Cookie expires or invalid.",
    })
    with patch.object(crawler_module, "_request_with_retry", return_value=resp):
        with pytest.raises(crawler_module.CookieExpiredError):
            crawler_module.fetch_messages(session, gid=123)


def test_messages_result_false_is_not_cookie_expiry(caplog):
    session = MagicMock()
    with patch.object(
        crawler_module,
        "_request_with_retry",
        return_value=_response({"result": False}),
    ), caplog.at_level(logging.WARNING):
        assert crawler_module.fetch_messages(session, gid=123) == []
    assert "cookie" not in caplog.text.lower()


def test_crawl_all_does_not_swallow_cookie_expiry():
    crawler = object.__new__(crawler_module.Crawler)
    crawler.sync_groups = MagicMock(return_value=[{"gid": 123, "name": "群"}])
    crawler.crawl_group = MagicMock(
        side_effect=crawler_module.CookieExpiredError("expired")
    )
    with patch.object(crawler_module, "_jitter_sleep"):
        with pytest.raises(crawler_module.CookieExpiredError):
            crawler.crawl_all()


def test_cli_reports_renew_only_for_cookie_expiry(caplog):
    with patch.object(
        crawl,
        "main",
        side_effect=crawler_module.CookieExpiredError("expired"),
    ), caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exc:
            crawl.cli()

    assert exc.value.code == 2
    assert "uv run crawl.py --renew-cookie" in caplog.text


def test_cli_does_not_report_renew_for_unrelated_error(caplog):
    with patch.object(
        crawl,
        "main",
        side_effect=RuntimeError("network error"),
    ), caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            crawl.cli()

    assert "--renew-cookie" not in caplog.text
