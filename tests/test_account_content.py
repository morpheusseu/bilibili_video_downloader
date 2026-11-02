import asyncio

import pytest
from account_content_provider import content_provider, dynamic
from bilibili_client.api import safe_error
from bilibili_client.sources import DownloadError


async def test_three_read_only_surfaces_paginate_through_real_sdk_and_shared_gate():
    provider = content_provider()
    async with provider.api() as api:
        with pytest.raises(DownloadError, match="登录"):
            await api.read_account_content("dynamic")
        assert not provider.requests
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        for kind in ("dynamic", "history"):
            first = await api.read_account_content(kind)
            assert len(first.items) == 20 and first.has_more
            full = await api.read_account_content(kind, more=True)
            assert len(full.items) == 21 and not full.has_more
            assert len({row.key for row in full.items}) == 21
            before = len(provider.requests)
            with pytest.raises(DownloadError, match="没有可继续"):
                await api.read_account_content(kind, more=True)
            assert len(provider.requests) == before
        assert "图片 1 张" in api.account_content.views["dynamic"].items[0].body
        assert "<plain>" in api.account_content.views["dynamic"].items[0].body
        assert "播放进度 12 秒" in api.account_content.views["history"].items[0].body
        cursor_request = provider.requests[-1]
        assert cursor_request.url.params["max"] == "2"
        assert cursor_request.url.params["view_at"] == "1700000002"
        assert cursor_request.url.params["business"] == "archive"
        assert all(request.method == "GET" for request in provider.requests)
        assert api.gate.count == len(provider.requests)


async def test_forward_keeps_only_first_original_text_and_picture_count_without_extra_reads(
    tmp_path,
):
    provider = content_provider(1)
    provider.cache_root = tmp_path
    outer = dynamic(3)
    outer["type"] = "DYNAMIC_TYPE_FORWARD"
    outer["modules"]["module_dynamic"] = {"desc": {"text": "我的转发"}}
    outer["modules"]["module_author"]["face"] = "https://i0.hdslb.com/face.png"
    original = dynamic(2)
    original["type"] = "DYNAMIC_TYPE_FORWARD"
    original["modules"]["module_dynamic"]["desc"]["text"] = "第一层原文"
    original["modules"]["module_dynamic"]["major"]["draw"]["items"] *= 3
    original["orig"] = dynamic(1)
    original["orig"]["modules"]["module_dynamic"]["desc"]["text"] = "更深层不应展示"
    outer["orig"] = original
    provider.content_state["dynamic"] = [outer]
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        before = len(provider.requests)
        view = await api.read_account_content("dynamic")
        assert len(provider.requests) == before + 1 and not provider.images
        item = view.items[0]
        assert "我的转发" in item.body and "第一层原文" in item.body
        assert "图片 3 张" in item.body and "更深层不应展示" not in item.body
        assert item.image_role == "avatar" and item.image.endswith("face.png")
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        cached = api.account_content.views["dynamic"]
        assert cached.cached and cached.items[0] == item


@pytest.mark.parametrize("visible", [True, False])
async def test_forward_video_or_unavailable_original_stays_readable(visible):
    provider = content_provider(1)
    outer = dynamic(2)
    outer["type"] = "DYNAMIC_TYPE_FORWARD"
    outer["orig"] = dynamic(1)
    outer["orig"]["visible"] = visible
    outer["orig"]["modules"]["module_dynamic"]["major"] = {
        "archive": {
            "title": "原视频标题",
            "bvid": "BV178411i7AD",
            "cover": "https://i0.hdslb.com/not-requested.jpg",
        }
    }
    provider.content_state["dynamic"] = [outer]
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        item = (await api.read_account_content("dynamic")).items[0]
        assert (
            ("原视频标题" in item.body and "视频 1 个" in item.body)
            if visible
            else "不可见" in item.body
        )
        assert not provider.images


@pytest.mark.parametrize("forwarded", [False, True])
@pytest.mark.parametrize("rich_text", [False, True])
async def test_null_major_variants_keep_opus_text_author_and_picture_count(forwarded, rich_text):
    provider = content_provider(1)
    opus = dynamic(1)
    opus["modules"]["module_author"].update(
        name="原作者", face="https://i0.hdslb.com/original-avatar.png"
    )
    opus["modules"]["module_dynamic"] = {
        "desc": None,
        "major": {
            "archive": None,
            "draw": None,
            "article": None,
            "common": None,
            "opus": {
                "summary": (
                    {"rich_text_nodes": [{"text": "真实结构"}, {"text": "的正文"}]}
                    if rich_text
                    else {"text": "真实结构的正文"}
                ),
                "pics": [{"url": "https://i0.hdslb.com/not-requested.png"}] * 3,
            },
        },
    }
    if forwarded:
        outer = dynamic(2)
        outer["type"] = "DYNAMIC_TYPE_FORWARD"
        outer["modules"]["module_author"]["face"] = "https://i0.hdslb.com/forwarder.png"
        outer["orig"] = opus
    else:
        outer = opus
    provider.content_state["dynamic"] = [outer]
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        before = len(provider.requests)
        item = (await api.read_account_content("dynamic")).items[0]
        assert "原作者" in item.body and "真实结构的正文" in item.body and "图片 3 张" in item.body
        assert ("转发原文（仅第一层）" in item.body) is forwarded
        assert item.image_role == "avatar"
        assert item.image.endswith("forwarder.png" if forwarded else "original-avatar.png")
        assert len(provider.requests) == before + 1 and not provider.images


async def test_video_uses_only_archive_cover_when_other_major_variants_are_null():
    provider = content_provider(1)
    video = dynamic(1)
    video["type"] = "DYNAMIC_TYPE_AV"
    major = video["modules"]["module_dynamic"]["major"]
    major.update(
        draw=None,
        archive={"title": "视频标题", "cover": "https://i0.hdslb.com/video-cover.png"},
    )
    video["modules"]["module_author"]["face"] = "https://i0.hdslb.com/author-avatar.png"
    provider.content_state["dynamic"] = [video]
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        item = (await api.read_account_content("dynamic")).items[0]
        assert item.title == "视频标题" and item.image_role == "cover"
        assert item.image.endswith("video-cover.png") and not provider.images


async def test_dynamic_update_check_skips_body_and_explicit_refresh_rechecks_edits():
    provider = content_provider()
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        first = await api.read_account_content("dynamic")
        before = len(provider.requests)
        same = await api.read_account_content("dynamic", check=True)
        assert same.items is first.items and same.has_more
        assert len(provider.requests) == before + 1
        assert provider.requests[-1].url.params["update_baseline"] == "synthetic-baseline"
        provider.content_state["dynamic"].insert(0, dynamic(99))
        provider.content_state["new"] = 1
        new = await api.read_account_content("dynamic", check=True)
        assert new.items[0].key == "99" and len(provider.requests) == before + 3
        provider.content_state["dynamic"][0]["modules"]["module_dynamic"]["desc"]["text"] = "已编辑"
        fresh = await api.read_account_content("dynamic")
        assert "已编辑" in fresh.items[0].body


@pytest.mark.parametrize("kind", ["dynamic", "followings", "history"])
async def test_nonadvancing_pagination_and_http_failures_preserve_view_and_frontier(
    kind,
):
    provider = content_provider(51 if kind == "followings" else 21)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        first = await api.read_account_content(kind)
        provider.content_state["repeat"] = True
        with pytest.raises(DownloadError, match="分页|游标"):
            await api.read_account_content(kind, more=True)
        assert api.account_content.views[kind] is first
        provider.content_state.update(repeat=False, fail=True)
        with pytest.raises(Exception) as error:
            await api.read_account_content(kind, more=True)
        assert "500" in safe_error(error.value)
        assert api.account_content.views[kind] is first
        provider.content_state["fail"] = False
        final = await api.read_account_content(kind, more=True)
        assert len(final.items) == (51 if kind == "followings" else 21) and not final.has_more


@pytest.mark.parametrize("risk", [403, 412, 429, -352, -412])
async def test_account_content_risk_stops_all_subsequent_requests_without_retry(risk):
    provider = content_provider()
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        first = await api.read_account_content("dynamic")
        provider.content_state["risk"] = risk
        with pytest.raises(DownloadError, match="停止"):
            await api.read_account_content("dynamic", check=True)
        count = len(provider.requests)
        for kind in ("dynamic", "followings", "history"):
            with pytest.raises(DownloadError, match="停止"):
                await api.read_account_content(kind)
        with pytest.raises(DownloadError, match="停止"):
            await api.sdk.video.view(aid=2)
        assert len(provider.requests) == count and api.account_content.views["dynamic"] is first


async def test_empty_results_are_real_and_bad_following_page_is_not_treated_as_unfollow():
    provider = content_provider(0)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        for kind in ("dynamic", "followings", "history"):
            empty = await api.read_account_content(kind)
            assert empty.items == () and not empty.has_more
        provider.content_state["followings"] = [{"mid": 7, "uname": "已关注"}]
        first = await api.read_account_content("followings")
        provider.content_state["empty"] = True
        with pytest.raises(DownloadError, match="不完整"):
            await api.read_account_content("followings")
        assert api.account_content.views["followings"] is first


async def test_session_change_rejects_late_result_without_repopulating_new_cache():
    provider = content_provider()
    entered, release = asyncio.Event(), asyncio.Event()
    handler = provider.handler

    async def delayed(request):
        if request.url.path.endswith("/feed/all"):
            entered.set()
            await release.wait()
        return handler(request)

    provider.handler = delayed
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        old = api.account_content
        task = asyncio.create_task(api.read_account_content("dynamic"))
        await entered.wait()
        api.clear_session()
        release.set()
        with pytest.raises(DownloadError, match="账号已变化"):
            await task
        assert api.account_content is not old and not api.account_content.views


async def test_cancelled_fetch_does_not_advance_pagination():
    provider = content_provider()
    entered = asyncio.Event()
    handler = provider.handler

    async def delayed(request):
        if request.url.params.get("offset"):
            entered.set()
            await asyncio.Event().wait()
        return handler(request)

    provider.handler = delayed
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        first = await api.read_account_content("dynamic")
        task = asyncio.create_task(api.read_account_content("dynamic", more=True))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert api.account_content.views["dynamic"] is first
        provider.handler = handler
        final = await api.read_account_content("dynamic", more=True)
        assert len(final.items) == 21
