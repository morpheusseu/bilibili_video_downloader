import asyncio

import pytest
from account_content_provider import content_provider
from creator_provider import creator_provider

from bili_downloader.account_content import AccountContent
from bili_downloader.cache import AccountCache
from bili_downloader.sources import DownloadError


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


async def test_all_people_get_both_latest_pages_before_any_interaction_request(
    tmp_path,
):
    provider = creator_provider()
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        first = await api.collect_followings()
        assert first.content_done < first.content_total and not first.done
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
        first_interaction = next(
            i for i, r in enumerate(provider.requests) if r.url.path.endswith("/archive/relation")
        )
        assert all(
            provider.requests.index(r) < first_interaction for r in dynamic_reads + video_reads
        )
        # Latest dynamic page has 12 videos; latest submission page has 20,
        # with shared aids. Every distinct video is fetched only once.
        assert progress.interactions_done == progress.interactions_total == 20
        relations = [r for r in provider.requests if r.url.path.endswith("/archive/relation")]
        assert len(relations) == len({r.url.params["aid"] for r in relations}) == 20
        before = len(provider.requests)
        await api.collect_followings()
        assert len(provider.requests) == before
        assert all(r.method == "GET" for r in provider.requests)


@pytest.mark.parametrize("stage", ["content", "interactions"])
async def test_cancel_restart_does_not_replay_completed_work(tmp_path, stage):
    provider = creator_provider(3)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")

        def stop(owner, progress):
            if (stage == "content" and progress.content_done == 3) or (
                stage == "interactions" and progress.interactions_done == 1
            ):
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            for _ in range(100):
                await api.collect_followings(on_update=stop)
        previous = api.following_collection.progress()
        assert previous.content_done == (3 if stage == "content" else 42)
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
        relations = [r for r in provider.requests if r.url.path.endswith("/archive/relation")]
        assert len(relations) == len({r.url.params["aid"] for r in relations}) == 3


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


async def test_missing_page_repairs_the_checkpoint_and_account_forget_scrubs_it(
    tmp_path,
):
    provider = creator_provider(2)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await drain(api)
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
