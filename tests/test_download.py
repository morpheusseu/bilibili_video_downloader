import asyncio

import httpx
import pytest
from conftest import BV, Provider, playback, response

from bili_downloader.api import RequestGate
from bili_downloader.download import download_source, media_url, transfer
from bili_downloader.sources import DownloadError, Source


@pytest.mark.parametrize("format", ["mp4", "mp3", "wav"])
async def test_sdk_to_streams_to_real_ffmpeg_and_existing_output(tmp_path, media, format):
    provider = Provider()
    media_requests = []

    def cdn(request):
        media_requests.append(request)
        assert "cookie" not in request.headers
        return httpx.Response(200, content=media[request.url.path.lstrip("/")])

    transport = httpx.MockTransport(cdn)
    progress = []
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        result = await download_source(
            api,
            Source("video", BV),
            tmp_path,
            format=format,
            media_transport=transport,
            report=progress.append,
        )
        assert len(result) == 1 and result[0].path.stat().st_size > 100
        assert result[0].path.parent == tmp_path
        assert result[0].path.suffix == "." + format
        assert len(media_requests) == (2 if format == "mp4" else 1)
        starts = [e.message for e in progress if e.phase == "download"]
        assert len(starts) == len(media_requests)
        if format == "mp4":
            assert "视频流（画面）" in starts[0] and "音频流（声音）" in starts[1]
            assert any("合并视频流与音频流，生成 1 个 MP4" in e.message for e in progress)
        else:
            assert "音频流（声音）" in starts[0]
        assert all(
            "视频流" in e.message or "音频流" in e.message for e in progress if e.phase == "bytes"
        )
        contents = result[0].path.read_bytes()
        again = await download_source(
            api, Source("video", BV), tmp_path, format=format, media_transport=transport
        )
        assert again[0].skipped and result[0].path.read_bytes() == contents
        assert len(media_requests) == (2 if format == "mp4" else 1)
    assert not list(tmp_path.glob(".bili-download-*"))


async def test_incomplete_media_never_publishes_final_file(tmp_path):
    provider = Provider()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"short", headers={"content-length": "1000"})
    )
    async with provider.api() as api:
        with pytest.raises(DownloadError, match="下载长度不完整"):
            await download_source(api, Source("video", BV), tmp_path, media_transport=transport)
    assert not list(tmp_path.iterdir())


async def test_download_cancel_cleans_temporary_files(tmp_path):
    started = asyncio.Event()

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"chunk"
            started.set()
            await asyncio.Event().wait()

    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Slow()))
    async with Provider().api() as api:
        task = asyncio.create_task(
            download_source(api, Source("video", BV), tmp_path, media_transport=transport)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not list(tmp_path.iterdir())


async def test_cdn_risk_response_stops_future_provider_requests(tmp_path):
    provider = Provider()
    async with provider.api() as api:
        transport = httpx.MockTransport(lambda request: httpx.Response(429))
        with pytest.raises(DownloadError):
            await download_source(api, Source("video", BV), tmp_path, media_transport=transport)
        count = len(provider.requests)
        with pytest.raises(DownloadError):
            await api.sdk.video.view(bvid=BV)
        assert len(provider.requests) == count
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "url",
    [
        "http://media.bilivideo.com/x",
        "https://localhost/x",
        "https://bilivideo.com.evil/x",
        "https://127.0.0.1/x",
    ],
)
def test_untrusted_media_addresses_are_not_fetched(url):
    with pytest.raises(DownloadError):
        media_url(url, [])


@pytest.mark.parametrize(
    "location",
    [
        "https://other.bilivideo.com/final",
        "http://other.bilivideo.com/final",
        "https://evil.example/final",
    ],
)
async def test_cdn_redirects_are_bounded_and_validated(tmp_path, location):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": location})
        return httpx.Response(200, content=b"synthetic-media")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        if location.startswith("https://other.bilivideo.com"):
            await transfer(
                http,
                RequestGate(0, 0),
                "https://media.bilivideo.com/start",
                tmp_path / "media",
                lambda e: None,
            )
            assert len(calls) == 2
            assert (tmp_path / "media").read_bytes() == b"synthetic-media"
        else:
            with pytest.raises(DownloadError):
                await transfer(
                    http,
                    RequestGate(0, 0),
                    "https://media.bilivideo.com/start",
                    tmp_path / "media",
                    lambda e: None,
                )
            assert len(calls) == 1 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("format", ["mp4", "mp3"])
async def test_progressive_segments_are_concatenated_in_server_order(tmp_path, media, format):
    class Progressive(Provider):
        def __call__(self, request):
            if request.url.path.endswith("playurl"):
                data = playback()
                data.pop("dash")
                data["durl"] = [
                    {
                        "order": number,
                        "length": 400,
                        "size": len(media["combined"]),
                        "ahead": "",
                        "vhead": "",
                        "url": f"https://media.bilivideo.com/segment{number}",
                    }
                    for number in (2, 1)
                ]
                return response(data)
            return super().__call__(request)

    calls, progress = [], []

    def cdn(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=media["combined"])

    async with Progressive().api() as api:
        result = await download_source(
            api,
            Source("video", BV),
            tmp_path,
            format=format,
            media_transport=httpx.MockTransport(cdn),
            report=progress.append,
        )
    assert calls == ["/segment1", "/segment2"]
    assert result[0].path.stat().st_size > 100
    starts = [e.message for e in progress if e.phase == "download"]
    assert len(starts) == 2
    assert "音视频片段 1/2（同一视频）" in starts[0]
    assert "音视频片段 2/2（同一视频）" in starts[1]
