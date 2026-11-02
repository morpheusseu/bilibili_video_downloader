import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
from bilibili_client import browser_login
from bilibili_client.api import Bilibili, RequestGate
from bilibili_client.sources import DownloadError, Source, VideoPart, parse_source
from conftest import BV, Provider, favorite_item, folder_info, response
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout


@pytest.mark.parametrize(
    ("text", "kind", "key", "page"),
    [
        (BV, "video", BV, None),
        ("av233802576", "video", "av233802576", None),
        (f"https://www.bilibili.com/video/{BV}?p=2", "video", BV, 2),
        ("https://space.bilibili.com/42/favlist?fid=10", "favorite", "10", None),
        ("https://www.bilibili.com/medialist/play/ml10", "favorite", "10", None),
        ("https://www.bilibili.com/bangumi/play/ep123", "episode", "123", None),
    ],
)
def test_source_inputs(text, kind, key, page):
    source = parse_source(text)
    assert (source.kind, source.key, source.page) == (kind, key, page)


@pytest.mark.parametrize(
    "value",
    [
        "file:///etc/passwd",
        "https://bilibili.com.evil/video/" + BV,
        "https://evil.com/" + BV,
        "ep0",
        "av0",
        f"https://www.bilibili.com/video/{BV}?p=0",
    ],
)
def test_invalid_sources(value):
    with pytest.raises(DownloadError):
        parse_source(value)


async def test_real_sdk_parts_and_cookie_identity():
    provider = Provider(pages=2)
    async with provider.api() as api:
        session = await api.use_cookie(
            "SESSDATA=synthetic; DedeUserID=42", user_agent="Synthetic Chrome"
        )
        assert session.mid == 42
        assert "synthetic" not in repr(session)
        assert provider.requests[-1].headers["user-agent"] == "Synthetic Chrome"
        selected = [
            p async for p in api.parts(parse_source(f"https://www.bilibili.com/video/{BV}?p=2"))
        ]
        assert [p.cid for p in selected] == [101]
        all_parts = [p async for p in api.parts(Source("video", BV), all_parts=True)]
        assert [p.number for p in all_parts] == [1, 2]
    assert api.session is None


async def test_favorites_read_actual_pages_not_reported_counts():
    def handler(request):
        if request.url.path.endswith("list-all"):
            return response(
                {
                    "count": 1,
                    "list": [
                        {
                            "id": 10,
                            "fid": 10,
                            "mid": 42,
                            "attr": 0,
                            "title": "Piano",
                            "fav_state": 0,
                            "media_count": 0,
                        }
                    ],
                }
            )
        page = int(request.url.params["pn"])
        items = (
            [favorite_item()] if page == 1 else [favorite_item(), favorite_item("BV1GJ411x7h7", 2)]
        )
        return response({"info": folder_info(0), "medias": items, "has_more": page == 1, "ttl": 0})

    provider = Provider(handler)
    async with provider.api() as api:
        parts = [p async for p in api.parts(parse_source("42", "user", "Pia.*"))]
        assert [p.bvid for p in parts] == [BV, "BV1GJ411x7h7"]
    assert [
        r.url.params["pn"] for r in provider.requests if r.url.path.endswith("resource/list")
    ] == ["1", "2"]


@pytest.mark.parametrize("empty", [True, False])
async def test_nonadvancing_favorite_page_is_not_success(empty):
    provider = Provider(
        lambda request: response(
            {
                "info": folder_info(),
                "medias": [] if empty else [favorite_item()],
                "has_more": True,
                "ttl": 0,
            }
        )
    )
    async with provider.api() as api:
        with pytest.raises(DownloadError, match="分页未推进"):
            _ = [v async for v in api.favorite_videos(10)]
    assert len(provider.requests) <= 2


async def test_spacing_and_risk_latch():
    gate = RequestGate(0.5, 0.51)
    async with gate.request():
        pass
    async with gate.request():
        assert gate.last_gap >= 0.5
    calls = []

    def deny(request):
        calls.append(request)
        return httpx.Response(200, content=b'{"code":-352}', headers={"content-type": "text/plain"})

    async with Bilibili(gate=RequestGate(0, 0), transport=httpx.MockTransport(deny)) as api:
        for _ in range(2):
            with pytest.raises(DownloadError):
                await api.sdk.video.view(bvid=BV)
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["read", "send", "unknown", "risk", "timeout", "cancel"])
async def test_http_diagnostics_never_log_credentials_payloads_or_arbitrary_paths(caplog, mode):
    caplog.set_level(logging.INFO, logger="bilibili_client")

    def handler(request):
        if mode == "timeout":
            raise httpx.ReadTimeout("EXCEPTION_SECRET SESSDATA=COOKIE_SECRET", request=request)
        if mode == "cancel":
            raise asyncio.CancelledError
        return httpx.Response(
            429 if mode == "risk" else 200,
            json={"code": "CODE_SECRET", "data": "PRIVATE_BODY_SECRET"},
        )

    async with Bilibili(gate=RequestGate(0, 0), transport=httpx.MockTransport(handler)) as api:
        path = (
            "/web_im/v1/web_im/send_msg"
            if mode == "send"
            else "/PATH_SECRET"
            if mode == "unknown"
            else "/x/v3/fav/resource/ids"
        )
        request = api.http.build_request(
            "POST" if mode == "send" else "GET",
            ("https://api.vc.bilibili.com" if mode == "send" else "https://api.bilibili.com")
            + path,
            params={"secret": "QUERY_SECRET"},
            headers={"Cookie": "SESSDATA=COOKIE_SECRET", "Authorization": "HEADER_SECRET"},
            content=b"PRIVATE_BODY_SECRET CSRF_SECRET",
        )
        if mode in {"risk", "timeout", "cancel"}:
            error = {
                "risk": DownloadError,
                "timeout": httpx.ReadTimeout,
                "cancel": asyncio.CancelledError,
            }[mode]
            with pytest.raises(error):
                await api.http.send(request)
        else:
            await api.http.send(request)
    records = [r.getMessage() for r in caplog.records if r.name == "bilibili_client.api"]
    assert len(records) == 1
    text = records[0]
    assert "HTTP request=1 endpoint=" in text and "gap_ms=" in text and "duration_ms=" in text
    assert "status=429" in text if mode == "risk" else "api_code=none" in text
    if mode == "unknown":
        assert "endpoint=other_api" in text
    assert not any(value in text for value in ("SECRET", "SESSDATA", "Cookie", "Authorization"))


class Browser:
    def __init__(self):
        self.closed = False
        self.cookies_started = asyncio.Event()
        self.wait_cookies = False
        self.pages = [self]
        self.navigated = False
        self.mid = "42"

    async def launch(self, **kwargs):
        assert kwargs == {"channel": "chrome", "headless": False}
        return self

    async def new_context(self, **kwargs):
        assert kwargs == {"no_viewport": True}
        return self

    async def new_page(self):
        return self

    async def goto(self, url, **kwargs):
        assert url == "https://passport.bilibili.com/login"
        self.navigated = True

    async def cookies(self, urls):
        self.cookies_started.set()
        if self.wait_cookies:
            await asyncio.Event().wait()
        return [
            {"name": "SESSDATA", "value": "synthetic"},
            {"name": "DedeUserID", "value": self.mid},
            {"name": "buvid4", "value": "synthetic-browser-device"},
            {"name": "ignored", "value": "not-exported"},
        ]

    async def evaluate(self, expression):
        assert expression == "navigator.userAgent"
        if self.navigated:
            raise BrowserError(
                "Execution context was destroyed, most likely because of a navigation"
            )
        return "Synthetic Chrome"

    def is_connected(self):
        return not self.closed

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True

    async def __aenter__(self):
        return SimpleNamespace(chromium=self)

    async def __aexit__(self, *args):
        pass


@pytest.mark.parametrize("cancel", [False, True])
async def test_chrome_handoff_and_cancel_close_only_owned_browser(monkeypatch, cancel):
    browser = Browser()
    browser.wait_cookies = cancel
    monkeypatch.setattr(browser_login, "async_playwright", lambda: browser)
    provider, messages = Provider(), []
    async with provider.api() as api:
        task = asyncio.create_task(browser_login.chrome_login(api, messages.append))
        await browser.cookies_started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert api.session is None
        else:
            session = await task
            assert session.mid == 42 and session.user_agent == "Synthetic Chrome"
            assert "ignored" not in session.cookie
            assert "buvid4=synthetic-browser-device" in session.cookie
            assert len(provider.requests) == 1
            assert [message[:3] for message in messages] == ["1/4", "2/4", "3/4", "4/4"]
        assert len(provider.requests) == (0 if cancel else 1)
    assert browser.closed


@pytest.mark.parametrize(
    ("method", "stage", "error"),
    [
        ("launch", "启动 Chrome", BrowserError("missing chrome; SESSDATA=do-not-log")),
        ("new_context", "准备独立会话", BrowserError("SESSDATA=do-not-log")),
        ("evaluate", "读取浏览器信息", BrowserError("Execution context was destroyed; do-not-log")),
        ("goto", "打开官方登录页", BrowserTimeout("https://private.invalid/?SESSDATA=do-not-log")),
        ("cookies", "读取登录状态", BrowserError("Target context has been closed; do-not-log")),
    ],
)
async def test_chrome_errors_identify_stage_without_disclosing_browser_details(
    monkeypatch, method, stage, error
):
    browser, provider = Browser(), Provider()

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(browser, method, fail)
    monkeypatch.setattr(browser_login, "async_playwright", lambda: browser)
    async with provider.api() as api:
        with pytest.raises(DownloadError) as raised:
            await browser_login.chrome_login(api, lambda message: None)
        assert f"［{stage}］" in str(raised.value)
        assert "do-not-log" not in str(raised.value)
        assert "SESSDATA" not in str(raised.value)
        assert "private.invalid" not in str(raised.value)
        assert api.session is None
    assert not provider.requests
    if method != "launch":
        assert browser.closed


async def test_chrome_handoff_does_not_depend_on_original_page_staying_open(monkeypatch):
    browser = Browser()
    browser.pages = [object()]  # Provider replaced the original login tab.
    monkeypatch.setattr(browser, "is_closed", lambda: True)
    monkeypatch.setattr(browser_login, "async_playwright", lambda: browser)
    provider = Provider()
    async with provider.api() as api:
        assert (await browser_login.chrome_login(api, lambda message: None)).mid == 42
        assert len(provider.requests) == 1
    assert browser.closed


@pytest.mark.parametrize("verification", ["identity_mismatch", "risk_rejected"])
async def test_chrome_failed_identity_verification_clears_session_without_retry(
    monkeypatch, verification
):
    browser = Browser()
    monkeypatch.setattr(browser_login, "async_playwright", lambda: browser)
    requests = []

    def nav(request):
        requests.append(request)
        if verification == "risk_rejected":
            return response(None, code=-352)
        return response({"isLogin": True, "mid": 43, "uname": "Another synthetic account"})

    async with Bilibili(gate=RequestGate(0, 0), transport=httpx.MockTransport(nav)) as api:
        with pytest.raises(DownloadError, match="验证账号身份"):
            await browser_login.chrome_login(api, lambda message: None)
        assert api.session is None and not list(api.http.cookies)
        assert api.gate.stopped == (verification == "risk_rejected")
    assert browser.closed and len(requests) == 1


@pytest.mark.parametrize("mismatch", [False, True])
async def test_chrome_cleanup_failure_does_not_mask_verified_session_or_primary_error(
    monkeypatch, mismatch
):
    browser, provider, messages = Browser(), Provider(), []
    browser.mid = "43" if mismatch else "42"

    async def cannot_close():
        raise BrowserError("SESSDATA=do-not-log")

    monkeypatch.setattr(browser, "close", cannot_close)
    monkeypatch.setattr(browser_login, "async_playwright", lambda: browser)
    async with provider.api() as api:
        if mismatch:
            with pytest.raises(DownloadError, match="验证账号身份.*账号不一致"):
                await browser_login.chrome_login(api, messages.append)
            assert api.session is None
        else:
            session = await browser_login.chrome_login(api, messages.append)
            assert session is api.session and session.mid == 42
        assert "清理提示［关闭登录窗口］" in messages[-1]
        assert "do-not-log" not in "\n".join(messages)
    assert len(provider.requests) == 1


@pytest.mark.parametrize(("drm", "preview"), [(True, 0), (False, 1)])
async def test_episode_stream_does_not_download_drm_or_preview(drm, preview):
    async with Provider().api() as api:

        async def stream(**kwargs):
            assert kwargs == {"ep_id": 123, "quality": 64, "format_flags": 16}
            return SimpleNamespace(is_drm=drm, is_preview=preview)

        api.sdk.bangumi.video_stream = stream
        with pytest.raises(DownloadError, match="DRM"):
            await api.streams(VideoPart(BV, 1, 100, "title", "part", 1, episode_id=123), 64)
