import asyncio
from types import SimpleNamespace

import httpx
import pytest
from conftest import BV, Provider, favorite_item, folder_info, response

from bili_downloader import auth, browser_login
from bili_downloader.api import Bilibili, RequestGate, safe_error
from bili_downloader.auth import Session, load_session, save_session
from bili_downloader.sources import DownloadError, Source, VideoPart, parse_source


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


def test_opt_in_keychain_roundtrip_and_failure(monkeypatch):
    records = {}
    backend = SimpleNamespace(
        set_password=lambda service, key, value: records.update({(service, key): value}),
        get_password=lambda service, key: records.get((service, key)),
    )
    monkeypatch.setattr(auth, "_backend", lambda: backend)
    assert load_session() is None
    session = Session(42, "Synthetic", "SESSDATA=synthetic; DedeUserID=42", "Synthetic Chrome")
    save_session(session)
    assert load_session() == session

    def unavailable():
        raise RuntimeError("sensitive=never-display")

    monkeypatch.setattr(auth, "_backend", unavailable)
    with pytest.raises(DownloadError) as error:
        save_session(session)
    assert "sensitive" not in safe_error(error.value)


class Browser:
    def __init__(self):
        self.closed = False
        self.cookies_started = asyncio.Event()
        self.wait_cookies = False

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

    async def cookies(self, urls):
        self.cookies_started.set()
        if self.wait_cookies:
            await asyncio.Event().wait()
        return [
            {"name": "SESSDATA", "value": "synthetic"},
            {"name": "DedeUserID", "value": "42"},
            {"name": "buvid4", "value": "synthetic-browser-device"},
            {"name": "ignored", "value": "not-exported"},
        ]

    async def evaluate(self, expression):
        assert expression == "navigator.userAgent"
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
    async with Provider().api() as api:
        task = asyncio.create_task(browser_login.chrome_login(api, lambda message: None))
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
    assert browser.closed


@pytest.mark.parametrize(("drm", "preview"), [(True, 0), (False, 1)])
async def test_episode_stream_does_not_download_drm_or_preview(drm, preview):
    async with Provider().api() as api:

        async def stream(**kwargs):
            assert kwargs == {"ep_id": 123, "quality": 64, "format_flags": 16}
            return SimpleNamespace(is_drm=drm, is_preview=preview)

        api.sdk.bangumi.video_stream = stream
        with pytest.raises(DownloadError, match="DRM"):
            await api.streams(VideoPart(BV, 1, 100, "title", "part", 1, episode_id=123), 64)
