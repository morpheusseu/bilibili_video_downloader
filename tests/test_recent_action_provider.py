"""Recent observations travel through the real SDK and application HTTP gate.

Synthetic fixtures retain the public response shapes documented at:
https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/user/space.md
https://github.com/BACNext/bilibili-API-collect-backup/blob/master/docs/dynamic/space.md
"""

import httpx
import pytest
from bpi.errors import ApiError, InvalidParameterError, MissingDataError, ResponseDecodeError

from bili_downloader.api import Bilibili, RequestGate
from bili_downloader.provider_views import (
    FeedPage,
    own_dynamic_page,
    recent_coined_videos,
    recent_liked_videos,
)
from bili_downloader.sources import DownloadError


def feed():
    # Shape of the public /feed/space response, with synthetic identities/text.
    return {
        "has_more": True,
        "offset": "100",
        "update_baseline": "",
        "update_num": 0,
        "items": [
            {
                "basic": {
                    "comment_id_str": "100",
                    "comment_type": 17,
                    "like_icon": {},
                    "rid_str": "100",
                },
                "id_str": "100",
                "modules": {"module_author": {"mid": 42}},
                "type": "DYNAMIC_TYPE_FORWARD",
                "visible": True,
                "orig": {
                    "id_str": "99",
                    "type": "DYNAMIC_TYPE_AV",
                    "modules": {"module_dynamic": {"major": {"archive": {"aid": "7"}}}},
                },
            }
        ],
    }


@pytest.mark.parametrize("wrapped", [True, False])
async def test_recent_reads_use_same_sdk_session_and_gate_without_pagination(wrapped):
    requests = []
    gate = RequestGate(0, 0)

    def handler(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "api.bilibili.com"
        assert "SESSDATA=synthetic" in request.headers["cookie"]
        if request.url.path == "/x/space/like/video":
            assert dict(request.url.params) == {"vmid": "42"}
            rows = [{"aid": 7, "title": "Ignored video metadata"}, {"aid": 8}]
            data = {"list": rows, "total": 999} if wrapped else rows
        elif request.url.path == "/x/space/coin/video":
            assert dict(request.url.params) == {"vmid": "42"}
            data = [{"aid": 9, "coins": 2, "time": 1601802763}]
        else:
            assert request.url.path == "/x/polymer/web-dynamic/v1/feed/space"
            assert request.url.params["host_mid"] == "42"
            assert "offset" not in request.url.params
            data = feed()
        return httpx.Response(200, json={"code": 0, "data": data})

    async with Bilibili(gate=gate, transport=httpx.MockTransport(handler)) as api:
        api.http.cookies.set("SESSDATA", "synthetic", domain=".bilibili.com", path="/")
        liked = await recent_liked_videos(api.sdk, 42)
        coined = await recent_coined_videos(api.sdk, 42)
        page = await own_dynamic_page(api.sdk, 42)
    assert [row.model_dump() for row in liked] == [{"aid": 7}, {"aid": 8}]
    assert [row.model_dump() for row in coined] == [{"aid": 9, "coins": 2}]
    assert isinstance(page, FeedPage)
    assert page.items[0].orig["id_str"] == "99"
    assert page.has_more  # The adapter stops at the first screen anyway.
    assert len(requests) == gate.count == 3


@pytest.mark.parametrize(
    ("read", "data"),
    [
        (recent_liked_videos, []),
        (recent_liked_videos, {"list": []}),
        (recent_coined_videos, []),
        (own_dynamic_page, dict(feed(), items=[], has_more=False)),
    ],
)
async def test_explicit_empty_recent_response_is_valid(read, data):
    async with Bilibili(
        gate=RequestGate(0, 0),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": 0, "data": data})
        ),
    ) as api:
        result = await read(api.sdk, 42)
    assert (result.items if isinstance(result, FeedPage) else result) == []


@pytest.mark.parametrize("read", [recent_liked_videos, recent_coined_videos, own_dynamic_page])
@pytest.mark.parametrize("payload", [{"code": 0}, {"code": 0, "data": None}])
async def test_missing_recent_payload_is_an_error(read, payload):
    async with Bilibili(
        gate=RequestGate(0, 0),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    ) as api:
        with pytest.raises(MissingDataError):
            await read(api.sdk, 42)


@pytest.mark.parametrize(
    ("read", "data"),
    [
        (recent_liked_videos, {}),
        (recent_liked_videos, {"list": None}),
        (recent_liked_videos, [{"aid": "7"}]),
        (recent_liked_videos, [{"aid": True}]),
        (recent_liked_videos, [{"aid": 0}]),
        (recent_liked_videos, [{"aid": 7}, {}]),
        (recent_coined_videos, {"list": []}),
        (recent_coined_videos, [{"aid": 7}]),
        (recent_coined_videos, [{"aid": 7, "coins": "1"}]),
        (recent_coined_videos, [{"aid": 7, "coins": True}]),
        (recent_coined_videos, [{"aid": 7, "coins": 0}]),
        (recent_coined_videos, [{"aid": 7, "coins": 3}]),
        (own_dynamic_page, {}),
        (own_dynamic_page, dict(feed(), items=None)),
        (own_dynamic_page, dict(feed(), items=[{}])),
    ],
)
async def test_malformed_recent_payload_is_never_an_empty_success(read, data):
    async with Bilibili(
        gate=RequestGate(0, 0),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": 0, "data": data})
        ),
    ) as api:
        with pytest.raises(ResponseDecodeError):
            await read(api.sdk, 42)


@pytest.mark.parametrize("read", [recent_liked_videos, recent_coined_videos, own_dynamic_page])
async def test_recent_privacy_denial_propagates_without_becoming_empty(read):
    async with Bilibili(
        gate=RequestGate(0, 0),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"code": 53013, "data": None})
        ),
    ) as api:
        with pytest.raises(ApiError) as error:
            await read(api.sdk, 42)
    assert error.value.code == 53013


async def test_recent_risk_denial_uses_existing_transport_stop_latch():
    requests = []

    def deny(request):
        requests.append(request)
        return httpx.Response(200, json={"code": -352, "data": None})

    gate = RequestGate(0, 0)
    async with Bilibili(gate=gate, transport=httpx.MockTransport(deny)) as api:
        for read in (recent_liked_videos, recent_coined_videos, own_dynamic_page):
            with pytest.raises(DownloadError):
                await read(api.sdk, 42)
    assert gate.stopped
    assert len(requests) == 1


@pytest.mark.parametrize("read", [recent_liked_videos, recent_coined_videos, own_dynamic_page])
@pytest.mark.parametrize("mid", [0, -1, True, "42"])
async def test_recent_account_identity_is_validated_before_transport(read, mid):
    def unexpected(request):
        raise AssertionError("Invalid account identity must not send a request")

    async with Bilibili(gate=RequestGate(0, 0), transport=httpx.MockTransport(unexpected)) as api:
        with pytest.raises(InvalidParameterError):
            await read(api.sdk, mid)
