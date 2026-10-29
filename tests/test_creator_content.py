import asyncio
import json
from copy import deepcopy
from pathlib import Path

import pytest
from creator_provider import creator_provider

from bili_downloader.account_content import AccountContent
from bili_downloader.cache import AccountCache
from bili_downloader.creator_content import CreatorContent
from bili_downloader.sources import DownloadError


def relation_reads(provider):
    return [r for r in provider.requests if r.url.path.endswith("/archive/relation")]


async def test_selected_cached_content_page_still_fills_missing_personal_states(tmp_path):
    provider = creator_provider(3)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.read_account_content("followings")
        # Bulk collection owns content only; opening a page fills its missing states.
        page = await api.creator_content.read_page(7, "videos")
        assert all(row.liked is None and row.coined is None for row in page.entries)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        updates = []
        completed = await api.read_creator(
            7, "videos", on_update=lambda _, view: updates.append(view)
        )
        assert completed.cached
        assert all(row.liked is not None and row.coined is not None for row in completed.entries)
        assert updates[0].entries[0].liked is None
        assert len(relation_reads(provider)) == 3
        assert len([r for r in provider.requests if r.url.path.endswith("/arc/search")]) == 1
        before = len(provider.requests)
        await api.read_creator(7, "videos")
        assert len(provider.requests) == before


@pytest.mark.parametrize("cooperative", [True, False])
async def test_recorded_cooperative_submission_preserves_actual_author_and_validates_ordinary_rows(
    cooperative,
):
    # Live /arc/search returned is_union_video=1 with a different uploader;
    # this fixture retains that shape with synthetic IDs/text.
    row = json.loads((Path(__file__).parent / "fixtures/cooperative_submission.json").read_text())
    row["is_union_video"] = int(cooperative)
    provider = creator_provider(1)
    provider.creator_state["videos"] = [row]
    provider.creator_state["states"][9001] = {"like": True, "coin": 1}
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.read_account_content("followings")
        if cooperative:
            page = await api.read_creator(7, "videos")
            assert "合作投稿" in page.entries[0].content.summary
            assert "合成合作作者" in page.entries[0].content.body
            assert page.entries[0].liked and page.entries[0].coined
        else:
            with pytest.raises(DownloadError, match="作者"):
                await api.read_creator(7, "videos")
            assert not relation_reads(provider)


async def test_selected_author_pages_share_video_observations_and_keep_dynamic_likes_distinct(
    tmp_path,
):
    provider = creator_provider()
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.read_account_content("followings")
        assert not relation_reads(provider)
        updates = []
        first = await api.read_creator(7, "dynamic", on_update=lambda _, view: updates.append(view))
        assert len(first.entries) == 12 and first.has_more
        assert updates[0].entries[0].coined is None
        assert len(relation_reads(provider)) == 12
        assert all(row.reposted is None for row in first.entries)
        assert "转发：未知" in first.statistics()
        assert first.entries[0].liked is False  # Dynamic 1023 is not liked.
        assert first.entries[0].coined is True  # Video 23 has two coins.
        assert provider.creator_state["states"][23]["like"] is False
        second = await api.read_creator(7, "dynamic", 2)
        assert len(second.entries) == 11 and not second.has_more
        assert len(relation_reads(provider)) == 23
        before = len(provider.requests)
        cached = await api.read_creator(7, "dynamic")
        assert cached.cached and len(provider.requests) == before
        submissions = await api.read_creator(7, "videos")
        assert len(submissions.entries) == 20 and len(relation_reads(provider)) == 23
        # Dynamic 1020 is liked, but its linked video is not: do not conflate targets.
        assert next(row for row in first.entries if row.aid == 20).liked is True
        assert next(row for row in submissions.entries if row.aid == 20).liked is False
        assert all(r.method == "GET" for r in provider.requests)
    before = len(provider.requests)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        restored = await api.read_creator(7, "dynamic", 2)
        assert restored.cached and len(restored.entries) == 11
        assert len(provider.requests) == before + 1  # Nav only.
        assert restored.statistics() == second.statistics()
        other = AccountCache(tmp_path, 43)
        try:
            assert CreatorContent(api.sdk, cache=other).cached_view(7, "dynamic") is None
        finally:
            other.close()


async def test_new_head_only_fetches_missing_interactions_and_invalidates_dependent_page_cursors(
    tmp_path,
):
    provider = creator_provider()
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.read_account_content("followings")
        await api.read_creator(7, "dynamic")
        await api.read_creator(7, "dynamic", 2)
        newest = deepcopy(provider.creator_state["dynamics"][0])
        newest["id_str"] = "1024"
        newest["modules"]["module_dynamic"]["major"]["archive"]["aid"] = "24"
        provider.creator_state["dynamics"].insert(0, newest)
        provider.creator_state["states"][24] = {"like": True, "coin": 0}
        before = len(relation_reads(provider))
        latest = await api.read_creator(7, "dynamic", refresh=True)
        assert latest.entries[0].content.key == "1024"
        assert len(relation_reads(provider)) == before + 1
        assert "后续分页已重置" in latest.notice
        assert api.creator_content.cached_view(7, "dynamic", 2) is None
        next_page = await api.read_creator(7, "dynamic", 2)
        assert {row.content.key for row in latest.entries}.isdisjoint(
            row.content.key for row in next_page.entries
        )
        assert len(relation_reads(provider)) == before + 1
        requests = len(provider.requests)
        await api.read_creator(7, "dynamic", 1)
        await api.read_creator(7, "dynamic", 2)
        assert len(provider.requests) == requests


async def test_cancel_keeps_page_and_confirmed_observations_without_repeating_them_on_recovery(
    tmp_path,
):
    provider = creator_provider(3)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.read_account_content("followings")

        def cancel(_, view):
            if len(relation_reads(provider)) == 1:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await api.read_creator(7, "videos", on_update=cancel)
        saved = api.creator_content.cached_view(7, "videos")
        assert len(saved.entries) == 3
        assert sum(row.coined is not None for row in saved.entries) == 1
        first_aid = int(relation_reads(provider)[0].url.params["aid"])
        # Refresh reuses the saved observation; only the two missing ones are read.
        await api.read_creator(7, "videos", refresh=True)
        assert [int(r.url.params["aid"]) for r in relation_reads(provider)].count(first_aid) == 1
        assert len(relation_reads(provider)) == 3


@pytest.mark.parametrize("risk", [403, 412, 429, "payload"])
async def test_creator_refusal_stops_the_shared_gate_and_does_not_retry(risk):
    provider = creator_provider(2)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.read_account_content("followings")
        if risk == "payload":
            provider.creator_state["risk_flag"] = True
        else:
            provider.creator_state["risk"] = risk
        with pytest.raises(DownloadError, match="风控"):
            await api.read_creator(7, "videos")
        assert api.gate.stopped
        before = len(provider.requests)
        with pytest.raises(DownloadError):
            await api.read_creator(7, "videos")
        assert len(provider.requests) == before


async def test_missing_personal_flags_are_unknown_and_incomplete_pages_never_resume(tmp_path):
    provider = creator_provider(3)
    provider.cache_root = tmp_path
    row = provider.creator_state["dynamics"][0]
    row["modules"]["module_dynamic"]["major"]["archive"] = None
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.read_account_content("followings")
        page = await api.read_creator(7, "dynamic")
        assert "已核验 2/3" in page.statistics()
        assert "待核验 1" in page.statistics()
        assert all(row.reposted is None for row in page.entries)
        api.cache.put_many({}, remove=(("creator_page", "7:dynamic:1"),))
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert api.creator_content.cached_view(7, "dynamic") is None
        before = len(provider.requests)
        with pytest.raises(DownloadError, match="上一页"):
            await api.read_creator(7, "dynamic", 2)
        assert len(provider.requests) == before


@pytest.mark.parametrize("kind", ["dynamic", "videos"])
async def test_repeated_provider_page_including_terminal_page_keeps_prior_pages(kind):
    provider = creator_provider()
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.read_account_content("followings")
        first = await api.read_creator(7, kind)
        provider.creator_state["repeat"] = True
        with pytest.raises(DownloadError, match="未推进"):
            await api.read_creator(7, kind, 2)
        assert api.creator_content.cached_view(7, kind, 2) is None
        assert api.creator_content.cached_view(7, kind).entries == first.entries


async def test_special_following_uses_provider_tags_and_missing_fields_stay_unknown():
    provider = creator_provider()
    users = provider.content_state["followings"]
    users[7].update(special=0, tag=[-10])
    users[9].pop("special")
    users[9].pop("tag")
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        view = await api.read_account_content("followings")
        rows = {row.key: row for row in view.items}
        assert rows["7"].special is True and rows["8"].special is True
        assert rows["9"].special is False and rows["10"].special is None


async def test_earlier_following_cache_restores_unknown_then_provider_marks_special(
    tmp_path,
):
    provider = creator_provider()
    cache = AccountCache(tmp_path, 42)
    try:
        # Sanitized shape of the saved projection before special was retained.
        old = {
            "key": "7",
            "title": "合成 UP 主",
            "summary": "UID 7",
            "body": "合成签名",
            "image_role": "avatar",
            "url": "https://space.bilibili.com/7",
            "image": "",
        }
        cache.put_many(
            {
                ("content", "followings"): {
                    "items": [old],
                    "has_more": True,
                    "notice": "本地缓存",
                    "frontier": {"next_page": 2},
                }
            }
        )
        async with provider.api() as api:
            content = AccountContent(api.sdk, cache=cache)
            assert content.views["followings"].items[0].special is None
            await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
            await content.read("followings", 42)
            assert (
                next(row for row in content.views["followings"].items if row.key == "7").special
                is True
            )
            assert cache.get("content_block", "followings:0", max_age=None)[6]["special"] is True
            assert "items" not in cache.get("content", "followings", max_age=None)
    finally:
        cache.close()
