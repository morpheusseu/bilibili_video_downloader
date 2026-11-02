"""Personal discovery, bounded correction, durability and account ownership."""

import asyncio
from copy import deepcopy

import httpx
import pytest
from bilibili_client.api import Bilibili, RequestGate
from bilibili_client.cache import AccountCache
from bilibili_client.creator_content import CreatorContent
from bilibili_client.sources import DownloadError
from conftest import response
from creator_provider import creator_provider

RECENT_PATHS = {
    "/x/space/like/video",
    "/x/space/coin/video",
    "/x/polymer/web-dynamic/v1/feed/space",
}


def recent_reads(provider):
    return [request for request in provider.requests if request.url.path in RECENT_PATHS]


def relation_reads(provider):
    return [
        request for request in provider.requests if request.url.path.endswith("/archive/relation")
    ]


def positive_lists(provider):
    state = provider.creator_state
    state["recent_likes"] = [{"aid": 1}, {"aid": 2}]
    state["recent_coins"] = [{"aid": 1, "coins": 1}, {"aid": 2, "coins": 2}]
    repost = deepcopy(state["dynamics"][0])
    repost["id_str"] = "9000"
    repost["type"] = "DYNAMIC_TYPE_FORWARD"
    repost["modules"]["module_author"]["mid"] = 42
    repost["orig"] = {"id_str": "1001"}
    state["own_dynamics"] = [repost]


async def test_recent_positive_discovery_populates_pages_without_enumerating_relationships():
    provider = creator_provider(2)
    positive_lists(provider)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.read_account_content("followings")
        notice, refreshed = await api.check_recent_actions()
        assert refreshed and "不代表完整历史" in notice
        videos = await api.read_creator(7, "videos")
        dynamics = await api.read_creator(7, "dynamic")
        assert all(row.liked and row.coined for row in videos.entries)
        linked = next(row for row in dynamics.entries if row.aid == 1)
        assert linked.liked is False  # The dynamic's own status remains distinct.
        assert linked.coined is True and linked.reposted is True
        assert "点赞：100%（2/2，已核验 2/2" in videos.statistics()
        assert "转发：100%（1/1，已核验 1/2，待核验 1）" in dynamics.statistics()
    assert len(recent_reads(provider)) == 3
    assert not relation_reads(provider)
    assert all(request.method == "GET" for request in provider.requests)


async def test_disappearing_recent_fact_is_uncertain_until_current_page_exact_correction():
    provider = creator_provider(2)
    positive_lists(provider)
    clock = [1700000000.0]
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        api.creator_content.clock = lambda: clock[0]
        await api.read_account_content("followings")
        await api.read_creator(7, "videos")
        first = api.creator_content.interactions.video(2)["liked"]
        provider.creator_state["recent_likes"] = []
        provider.creator_state["recent_coins"] = []
        clock[0] += 10
        await api.check_recent_actions(force=True)
        uncertain = api.creator_content.cached_view(7, "videos")
        assert all(row.liked is True and row.coined is True for row in uncertain.entries)
        assert all(set(row.uncertain) == {"liked", "coined"} for row in uncertain.entries)
        assert "点赞：未知（已核验 0/2）" in uncertain.statistics()
        assert api.creator_content.interactions.video(2)["liked"].checked_at == first.checked_at
        provider.creator_state["states"] = {
            1: {"like": False, "coin": 0},
            2: {"like": False, "coin": 0},
        }
        corrected = await api.read_creator(7, "videos")
        assert all(row.liked is False and row.coined is False for row in corrected.entries)
        assert all(not row.uncertain for row in corrected.entries)
        assert "点赞：0%（0/2，已核验 2/2" in corrected.statistics()
    assert len(relation_reads(provider)) == 2


async def test_recent_source_ttl_force_and_partial_exact_reads_keep_independent_times():
    provider = creator_provider(2)
    positive_lists(provider)
    clock = [1700000000.0]
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        api.creator_content.clock = lambda: clock[0]
        _, refreshed = await api.check_recent_actions()
        assert refreshed and len(recent_reads(provider)) == 3
        clock[0] += 599
        _, refreshed = await api.check_recent_actions()
        assert not refreshed and len(recent_reads(provider)) == 3
        clock[0] += 1
        _, refreshed = await api.check_recent_actions()
        assert refreshed and len(recent_reads(provider)) == 6
        _, refreshed = await api.check_recent_actions(force=True)
        assert refreshed and len(recent_reads(provider)) == 9
        interactions = api.creator_content.interactions
        coin_time = interactions.video(2)["coined"].checked_at
        clock[0] += 20
        provider.creator_state["states"][2] = {"like": False}
        await interactions.verify_video(2, force=True)
        state = interactions.video(2)
        assert state["liked"].value is False and state["liked"].checked_at == clock[0]
        assert state["coined"].value is True and state["coined"].checked_at == coin_time


async def test_recent_observations_survive_restart_are_account_scoped_and_forgotten(tmp_path):
    provider = creator_provider(2)
    positive_lists(provider)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.check_recent_actions()
    recent_count = len(recent_reads(provider))
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        interactions = api.creator_content.interactions
        assert interactions.video(2)["liked"].value is True
        assert interactions.repost("1001").value is True
        _, refreshed = await api.check_recent_actions()
        assert not refreshed and len(recent_reads(provider)) == recent_count
        other_cache = AccountCache(tmp_path, 43)
        try:
            other = CreatorContent(api.sdk, cache=other_cache).interactions
            assert other.video(2) == {} and other.repost("1001") is None
            assert other.source("liked") is None
        finally:
            other_cache.close()
        await api.forget_account_cache(42)
        assert api.session is None
        assert api.creator_content.interactions.video(2) == {}
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        interactions = api.creator_content.interactions
        assert interactions.video(2) == {} and interactions.source("liked") is None


async def test_malformed_recent_source_stays_blocked_until_explicit_refresh():
    provider = creator_provider(2)
    positive_lists(provider)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.check_recent_actions()
        provider.creator_state["recent_likes"] = [{"aid": "malformed"}]
        notice, refreshed = await api.check_recent_actions(force=True)
        assert not refreshed and "停止自动重试" in notice
        assert api.creator_content.interactions.video(2)["liked"].value is True
        before = len(recent_reads(provider))
        await api.check_recent_actions()
        assert len(recent_reads(provider)) == before
        provider.creator_state["recent_likes"] = [{"aid": 2}]
        _, refreshed = await api.check_recent_actions(force=True)
        assert refreshed and len(recent_reads(provider)) == before + 3


async def test_recent_risk_refusal_stops_shared_gate_before_other_reads():
    provider = creator_provider(2)
    fallback = provider.handler

    def refuse(request):
        if request.url.path == "/x/space/like/video":
            return response(None, code=-352)
        return fallback(request)

    provider.handler = refuse
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        with pytest.raises(DownloadError, match="风控"):
            await api.check_recent_actions()
        assert api.gate.stopped and len(recent_reads(provider)) == 1
        before = len(provider.requests)
        with pytest.raises(DownloadError):
            await api.sdk.video.view(aid=1)
        assert len(provider.requests) == before


async def test_account_switch_while_recent_read_waits_cannot_commit_or_continue():
    provider = creator_provider(2)
    positive_lists(provider)
    started, release = asyncio.Event(), asyncio.Event()

    async def transport(request):
        result = provider(request)
        if request.url.path == "/x/space/like/video":
            started.set()
            await release.wait()
        return result

    async with Bilibili(gate=RequestGate(0, 0), transport=httpx.MockTransport(transport)) as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        old = api.creator_content.interactions
        reading = asyncio.create_task(api.check_recent_actions())
        await started.wait()
        api.clear_session()
        release.set()
        with pytest.raises(DownloadError, match="账号已变化"):
            await reading
        assert api.creator_content.interactions.video(2) == {}
        assert old.video(2) == {} and old.source("liked") is None
    assert len(recent_reads(provider)) == 1


async def test_retained_paired_snapshot_converts_once_and_newer_facts_win(tmp_path):
    provider = creator_provider(1)
    provider.cache_root = tmp_path
    cache = AccountCache(tmp_path, 42)
    cache.put_many(
        {("video_interaction", "1"): {"checked_at": 100.0, "state": {"like": False, "coin": 0}}}
    )
    cache.close()
    provider.creator_state["dynamics"][0]["modules"]["module_stat"]["coin"] = {"status": True}
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        content = api.creator_content
        content.clock = lambda: 1000.0
        assert content.interactions.video(1)["coined"].value is False
        record = api.cache.get("video_interaction", "1", max_age=None)
        assert set(record) == {"liked", "coined"}
        assert record["liked"]["checked_at"] == 100.0
        # New explicit coin state from the page beats the older video snapshot.
        view = await content.read_page(7, "dynamic")
        assert view.entries[0].coined is True and view.entries[0].coined_at == 1000.0
        assert view.entries[0].liked is False  # Dynamic's own status.
        # A later exact video check then corrects that shared coin state.
        content.clock = lambda: 1001.0
        provider.creator_state["states"][1] = {"like": True, "coin": 0}
        await content.interactions.verify_video(1, force=True)
        view = content.cached_view(7, "dynamic")
        assert view.entries[0].coined is False and view.entries[0].coined_at == 1001.0
        assert view.entries[0].liked is False
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert api.creator_content.interactions.video(1)["coined"].checked_at == 1001.0


async def test_recent_cache_commit_failure_never_publishes_positive_facts(tmp_path, monkeypatch):
    provider = creator_provider(1)
    provider.cache_root = tmp_path
    positive_lists(provider)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        original = api.cache.put_many

        def fail(values, **kwargs):
            if any(kind == "recent_actions" for kind, key in values):
                raise DownloadError("synthetic cache write failure")
            return original(values, **kwargs)

        monkeypatch.setattr(api.cache, "put_many", fail)
        notice, refreshed = await api.check_recent_actions()
        assert not refreshed and "停止自动重试" in notice
        assert api.creator_content.interactions.video(1) == {}
        assert api.cache.get("video_interaction", "1", max_age=None) is None
        assert api.cache.get("recent_actions", "liked", max_age=None) is None
        # Failed persistence cannot turn the foreground timer into a retry loop.
        before = len([r for r in recent_reads(provider) if r.url.path.endswith("like/video")])
        await api.check_recent_actions()
        assert (
            len([r for r in recent_reads(provider) if r.url.path.endswith("like/video")]) == before
        )
