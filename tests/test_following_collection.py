import asyncio
from dataclasses import asdict

import pytest
from account_content_provider import content_provider
from bilibili_client.account_content import AccountContent, ContentItem
from bilibili_client.cache import AccountCache
from bilibili_client.creator_content import CreatorEntry
from bilibili_client.following_collection import FollowingCollection
from bilibili_client.sources import DownloadError
from creator_provider import creator_provider


async def drain(api):
    for _ in range(100):
        progress = await api.collect_followings()
        if progress.done:
            return progress
    pytest.fail("Collection did not complete within its finite work plan")


async def test_large_directory_uses_50_per_page_and_resumes_beyond_200_after_restart(
    tmp_path,
):
    provider = content_provider(251)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        view = await api.read_account_content("followings")
        while len(view.items) < 250:
            view = await api.read_account_content("followings", more=True)
        assert len(view.items) == 250 and view.has_more
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert len(api.account_content.views["followings"].items) == 250
        final = await api.read_account_content("followings", more=True)
        assert len(final.items) == 251 and not final.has_more
        requests = [r for r in provider.requests if r.url.path.endswith("/followings")]
        assert [r.url.params["pn"] for r in requests] == [str(i) for i in range(1, 7)]
        assert {r.url.params["ps"] for r in requests} == {"50"}


async def test_retained_20_person_frontier_keeps_its_page_boundaries(tmp_path):
    provider = content_provider(51)
    cache = AccountCache(tmp_path, 42)
    try:
        # Sanitized recorded pre-batch shape: no page_size field.
        cache.put_many(
            {
                ("content", "followings"): {
                    "items": [
                        {
                            "key": str(i),
                            "title": "合成关注者",
                            "summary": "",
                            "body": "",
                            "image_role": "avatar",
                        }
                        for i in range(1, 21)
                    ],
                    "has_more": True,
                    "notice": "缓存",
                    "frontier": {"next_page": 2},
                }
            }
        )
        async with provider.api() as api:
            await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
            content = AccountContent(api.sdk, cache=cache)
            continued = await content.read("followings", 42, more=True)
            assert [int(row.key) for row in continued.items] == list(range(1, 41))
            assert provider.requests[-1].url.params["ps"] == "20"
            fresh = await content.read("followings", 42)
            assert len(fresh.items) == 50
            assert provider.requests[-1].url.params["ps"] == "50"
    finally:
        cache.close()


async def test_all_people_get_both_latest_pages_without_interaction_requests(
    tmp_path,
):
    provider = creator_provider()
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        first = await api.collect_followings()
        # The first six-step round reads the directory and five content pages.
        assert first.content_done == 5 and not first.done
        assert not any(r.url.path.endswith("/archive/relation") for r in provider.requests)
        progress = await drain(api)
        assert progress.done and progress.people == 21 and progress.content_total == 42
        expected = {str(i) for i in range(1, 22)}
        dynamic_reads = [r for r in provider.requests if "host_mid" in r.url.params]
        video_reads = [r for r in provider.requests if r.url.path.endswith("/arc/search")]
        assert {r.url.params["host_mid"] for r in dynamic_reads} == expected
        assert {r.url.params["mid"] for r in video_reads} == expected
        assert all("offset" not in r.url.params for r in dynamic_reads)
        assert all(r.url.params["pn"] == "1" for r in video_reads)
        assert len(dynamic_reads) == len(video_reads) == 21
        assert not any(r.url.path.endswith("/archive/relation") for r in provider.requests)
        assert "个人视频互动" not in progress.notice
        checkpoint = api.cache.get("following_collection", "current", max_age=None)
        assert set(checkpoint) == {
            "started_at",
            "stage",
            "directory_started",
            "mids",
            "content_position",
        }
        before = len(provider.requests)
        await api.collect_followings()
        assert len(provider.requests) == before
        assert all(r.method == "GET" for r in provider.requests)


async def test_cancel_process_restart_does_not_replay_completed_work(tmp_path):
    provider = creator_provider(3)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")

        def stop(owner, progress):
            if progress.content_done == 3:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            for _ in range(100):
                await api.collect_followings(on_update=stop)
        previous = api.following_collection.progress()
        assert previous.content_done == 3
    before = len(provider.requests)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await drain(api)
        resumed = provider.requests[before:]
        # Previously committed directory and the first three content pages survive.
        assert not any(r.url.path.endswith("/followings") for r in resumed)
        assert not any(r.url.params.get("host_mid") in {"1", "2"} for r in resumed)
        assert not any(r.url.params.get("mid") == "1" for r in resumed)
        assert api.following_collection.progress().done
        assert not any(r.url.path.endswith("/archive/relation") for r in provider.requests)


@pytest.fixture
def retained_interaction_checkpoint():
    # Sanitized saved value from the former content-then-interactions plan.
    return {
        "started_at": 1700000000.0,
        "stage": "interactions",
        "directory_started": True,
        "mids": list(range(1, 22)),
        "content_position": 42,
        "aids": [3, 2, 1],
        "interaction_position": 1,
    }


@pytest.mark.parametrize("stage", ["interactions", "done"])
async def test_retained_checkpoint_converts_once_without_replaying_content(
    tmp_path, retained_interaction_checkpoint, stage
):
    provider = creator_provider(3)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await drain(api)
        saved = {**retained_interaction_checkpoint, "stage": stage}
        if stage == "done":
            saved["interaction_position"] = len(saved["aids"])
        api.cache.put_many({("following_collection", "current"): saved})
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        before = len(provider.requests)
        progress = await api.collect_followings()
        assert progress.done and progress.content_done == 42
        converted = api.cache.get("following_collection", "current", max_age=None)
        assert converted == {
            "started_at": saved["started_at"],
            "stage": "done",
            "directory_started": True,
            "mids": saved["mids"],
            "content_position": 42,
        }
        # Reloading the canonical checkpoint does not rewrite it again.
        writes = []
        original_put = api.cache.put_many

        def record_put(*args, **kwargs):
            writes.append(args)
            return original_put(*args, **kwargs)

        api.cache.put_many = record_put
        reloaded = FollowingCollection(api.account_content, api.creator_content, cache=api.cache)
        assert reloaded.progress() == progress
        assert not writes
        assert len(provider.requests) == before


async def test_thousands_of_cached_distinct_videos_do_not_add_interaction_work(tmp_path):
    provider = creator_provider()
    provider.content_state["followings"] = content_provider(251).content_state["followings"]
    provider.cache_root = tmp_path
    cache = AccountCache(tmp_path, 42)
    records = {}
    for mid in range(1, 252):
        for kind in ("dynamic", "videos"):
            entries = []
            for number in range(20):
                aid = mid * 100 + number
                entries.append(
                    asdict(
                        CreatorEntry(ContentItem(str(aid), "合成内容", "", "", "cover"), aid=aid)
                    )
                )
            records[("creator_index", f"{mid}:{kind}")] = {"count": 1}
            records[("creator_page", f"{mid}:{kind}:1")] = {
                "entries": entries,
                "cursor": "next" if kind == "dynamic" else 2,
                "has_more": True,
                "fetched_at": 1e12,
            }
    cache.put_many(records)
    cache.close()
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        progress = await drain(api)
        assert progress.done and progress.people == 251
        assert progress.content_done == progress.content_total == 502
        aids = {
            row.aid
            for mid in range(1, 252)
            for row in api.creator_content.cached_view(mid, "videos").entries
        }
        assert len(aids) == 5020
        assert not any(r.url.path.endswith("/archive/relation") for r in provider.requests)
        assert not any("host_mid" in r.url.params for r in provider.requests)
        assert not any(r.url.path.endswith("/arc/search") for r in provider.requests)


async def test_explicit_restart_refreshes_directory_and_both_latest_pages(tmp_path):
    provider = creator_provider(2)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await drain(api)
        before = len(provider.requests)
        progress = await api.collect_followings(restart=True)
        assert not progress.done
        await drain(api)
        refreshed = provider.requests[before:]
        assert len([r for r in refreshed if r.url.path.endswith("/followings")]) == 1
        assert len([r for r in refreshed if "host_mid" in r.url.params]) == 21
        assert len([r for r in refreshed if r.url.path.endswith("/arc/search")]) == 21
        assert not any(r.url.path.endswith("/archive/relation") for r in refreshed)


async def test_collection_rejects_a_different_cache_account_without_changes(tmp_path):
    provider = creator_provider(2)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.collect_followings()
        previous = api.following_collection.progress()
        checkpoint = api.cache.get("following_collection", "current", max_age=None)
        before = len(provider.requests)
        with pytest.raises(DownloadError, match="账号不匹配"):
            await api.following_collection.run_round(43, restart=True)
        assert api.following_collection.progress() == previous
        assert api.cache.get("following_collection", "current", max_age=None) == checkpoint
        assert len(provider.requests) == before


async def test_account_change_discards_late_collection_result_and_does_not_continue():
    provider = creator_provider(2)
    original = provider.handler
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(request):
        if "host_mid" in request.url.params:
            entered.set()
            await release.wait()
        return original(request)

    provider.handler = delayed
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        task = asyncio.create_task(api.collect_followings())
        await entered.wait()
        before = len(provider.requests)
        api.clear_session()
        release.set()
        with pytest.raises(DownloadError, match="账号已变化"):
            await task
        assert len(provider.requests) == before
        assert api.following_collection.progress() is None


@pytest.mark.parametrize("corrupt", [False, True])
async def test_missing_or_corrupt_page_repairs_checkpoint_and_forget_scrubs_it(
    tmp_path,
    corrupt,
    retained_interaction_checkpoint,
):
    provider = creator_provider(2)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await drain(api)
        api.cache.put_many({("following_collection", "current"): retained_interaction_checkpoint})
        if corrupt:
            api.cache.put_many({("creator_page", "7:videos:1"): {"entries": "corrupt"}})
        else:
            api.cache.put_many({}, remove=[("creator_page", "7:videos:1")])
    before = len(provider.requests)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await drain(api)
        assert any(r.url.params.get("mid") == "7" for r in provider.requests[before:])
        folder = api.cache.folder
        await api.forget_account_cache(42)
        assert not folder.exists()
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert api.following_collection.progress() is None


async def test_refusal_stops_collection_without_automatic_retry_or_cursor_advance():
    provider = creator_provider(2)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.collect_followings()
        previous = api.following_collection.progress()
        provider.creator_state["risk"] = 412
        with pytest.raises(DownloadError, match="风控"):
            await api.collect_followings()
        assert api.following_collection.progress() == previous
        before = len(provider.requests)
        with pytest.raises(DownloadError):
            await api.collect_followings()
        assert len(provider.requests) == before
