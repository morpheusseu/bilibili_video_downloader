import httpx
import pytest
from bilibili_client import local_store
from bilibili_client.api import Bilibili, RequestGate
from bilibili_client.cache import AccountCache
from bilibili_client.library import FavoriteLibrary
from bilibili_client.sources import DownloadError
from bpi.errors import HttpStatusError
from conftest import PNG, favorite_item, library_provider, response

from bili_downloader.auth import load_session, save_session


def detail_calls(provider):
    return [r for r in provider.requests if r.url.path.endswith("resource/list")]


async def test_missing_favorite_metadata_is_batched_and_shared_across_folders_and_restart(tmp_path):
    provider, state = library_provider(21)
    provider.cache_root = tmp_path
    state["folders"].append(dict(state["folders"][0], id=11, fid=11))
    originals = {row["id"]: dict(row) for row in state["items"]}
    for row in state["items"]:
        row.update(cover="", fav_time=1700000000)
    state["items"][0]["title"] = ""
    original = provider.handler

    def handler(request):
        if request.url.path.endswith("resource/infos"):
            rows = []
            for resource in request.url.params["resources"].split(","):
                aid, kind = map(int, resource.split(":"))
                assert kind == 2
                rows.append(
                    {
                        **originals[aid],
                        "fav_time": 1,
                        "cnt_info": {"collect": 0, "play": 0, "danmaku": 0},
                    }
                )
            return response(rows)
        return original(request)

    provider.handler = handler
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        first = await api.favorites.refresh(42)
        assert first.items[0].title == "Synthetic video"
        assert all(row.cover and row.fav_time == 1700000000 for row in first.items)
        complete = await api.favorites.more()
        assert len(complete.items) == 21
        calls = [r for r in provider.requests if r.url.path.endswith("resource/infos")]
        assert [len(r.url.params["resources"].split(",")) for r in calls] == [20, 1]
        await api.favorites.open(11)
        assert len([r for r in provider.requests if r.url.path.endswith("resource/infos")]) == 2
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.favorites.refresh(42, force=True)
        assert len([r for r in provider.requests if r.url.path.endswith("resource/infos")]) == 2
        assert all(row.cover for row in api.favorites.view.items)


@pytest.mark.parametrize("bad", ["mismatch", "refusal", "missing"])
async def test_batch_metadata_failure_never_replaces_membership_or_retries_per_video(bad):
    provider, state = library_provider(2)
    original = provider.handler

    def handler(request):
        if request.url.path.endswith("resource/infos"):
            if bad == "refusal":
                return httpx.Response(412)
            if bad == "missing":
                return response([])
            return response(
                [{**favorite_item(id=999), "cnt_info": {"collect": 0, "play": 0, "danmaku": 0}}]
            )
        return original(request)

    provider.handler = handler
    async with provider.api() as api:
        first = await api.favorites.refresh(42)
        state["items"][0]["cover"] = ""
        if bad == "missing":
            updated = await api.favorites.refresh(42, force=True)
            assert not updated.items[0].cover and updated.items[0].id == first.items[0].id
        else:
            with pytest.raises(DownloadError):
                await api.favorites.refresh(42, force=True)
            assert api.favorites.view is first
        assert len([r for r in provider.requests if r.url.path.endswith("resource/infos")]) == 1
        assert not any(r.url.path.endswith("/view") for r in provider.requests)
        if bad == "refusal":
            assert api.gate.stopped


async def test_minute_checks_refresh_directory_only_every_ten_minutes_and_force_is_immediate(
    caplog,
):
    caplog.set_level("INFO", logger="bilibili_client.library")
    provider, _ = library_provider(1)
    now = [0.0]
    async with provider.api() as api:
        api.favorites.clock = lambda: now[0]
        for minute in range(61):
            now[0] = minute * 60.0
            await api.favorites.refresh(42)
            assert (
                sum(r.url.path.endswith("list-all") for r in provider.requests) == 1 + minute // 10
            )
        assert sum(r.url.path.endswith("resource/ids") for r in provider.requests) == 61
        assert len(detail_calls(provider)) == 7
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert text.count("Favorites directory=fetched") == 7
        assert text.count("Favorites directory=reused") == 54
        assert text.count("Favorites body=fetched") == 7
        assert text.count("Favorites body=reused") == 54
        before = len(provider.requests)
        now[0] += 1
        await api.favorites.refresh(42, force=True)
        assert len(provider.requests) - before == 3


async def test_empty_directory_is_cached_but_new_and_removed_folders_are_discovered():
    provider, state = library_provider(1)
    folders, state["folders"] = state["folders"], []
    now = [0.0]
    async with provider.api() as api:
        api.favorites.clock = lambda: now[0]
        assert not (await api.favorites.refresh(42)).folders
        state["folders"] = folders
        now[0] = 599
        assert not (await api.favorites.refresh(42)).folders
        assert len(provider.requests) == 1
        now[0] = 600
        assert (await api.favorites.refresh(42)).folder_id == 10
        state["folders"] = []
        now[0] = 1200
        empty = await api.favorites.refresh(42)
        assert not empty.folders and not empty.items and empty.folder_id is None
        before = len(provider.requests)
        now[0] += 60
        await api.favorites.refresh(42)
        assert len(provider.requests) == before


async def test_directory_refresh_keeps_selection_or_selects_a_remaining_folder():
    provider, state = library_provider(1)
    state["folders"].append(dict(state["folders"][0], id=11, fid=11, title="另一个收藏夹"))
    now = [0.0]
    async with provider.api() as api:
        api.favorites.clock = lambda: now[0]
        await api.favorites.refresh(42)
        await api.favorites.open(11)
        now[0] = 600
        assert (await api.favorites.refresh(42)).folder_id == 11
        state["folders"].pop()
        now[0] = 1200
        assert (await api.favorites.refresh(42)).folder_id == 10


async def test_failed_content_check_does_not_commit_directory_freshness():
    provider, _ = library_provider(1)
    original, fail = provider.handler, [False]

    def handler(request):
        if fail[0] and request.url.path.endswith("resource/ids"):
            return httpx.Response(500)
        return original(request)

    provider.handler = handler
    now = [0.0]
    async with provider.api() as api:
        api.favorites.clock = lambda: now[0]
        first = await api.favorites.refresh(42)
        now[0], fail[0] = 600, True
        with pytest.raises(HttpStatusError):
            await api.favorites.refresh(42)
        assert api.favorites.view is first
        now[0], fail[0] = 601, False
        await api.favorites.refresh(42)
        assert sum(r.url.path.endswith("list-all") for r in provider.requests) == 3


async def test_favorites_cache_reuses_body_and_loads_pages_only_on_demand():
    provider, _ = library_provider()
    async with provider.api() as api:
        first = await api.favorites.refresh(42)
        assert len(first.items) == 20 and first.has_more and first.folder_id == 10
        assert len(first.digest) == 64 and len(detail_calls(provider)) == 1
        cached = await api.favorites.refresh(42)
        assert cached.items is first.items and not cached.refreshed
        assert len(detail_calls(provider)) == 1
        complete = await api.favorites.more()
        assert len(complete.items) == 21 and not complete.has_more
        assert [r.url.params["pn"] for r in detail_calls(provider)] == ["1", "2"]
        count = len(provider.requests)
        await api.favorites.more()
        assert len(provider.requests) == count
        assert (await api.favorites.refresh(42)).items is complete.items


async def test_equal_count_replacement_changes_digest_and_resets_pagination():
    provider, state = library_provider()
    async with provider.api() as api:
        original = await api.favorites.refresh(42)
        await api.favorites.more()
        state["items"][-1] = favorite_item("BV0000000022", 22)
        updated = await api.favorites.refresh(42)
        assert updated.digest != original.digest
        assert len(updated.items) == 20 and updated.has_more
        complete = await api.favorites.more()
        assert complete.items[-1].id == 22
        assert 21 not in {item.id for item in complete.items}


async def test_title_updates_have_forced_and_bounded_metadata_refresh():
    provider, state = library_provider(1)
    now = [0.0]
    async with provider.api() as api:
        api.favorites.clock = lambda: now[0]
        first = await api.favorites.refresh(42)
        state["items"][0]["title"] = "标题已修改"
        assert (await api.favorites.refresh(42)).items is first.items
        forced = await api.favorites.refresh(42, force=True)
        assert forced.items[0].title == "标题已修改"
        state["items"][0]["title"] = "再次修改"
        now[0] = 601
        assert (await api.favorites.refresh(42)).items[0].title == "再次修改"
        assert len(detail_calls(provider)) == 3


async def test_failed_or_nonadvancing_refresh_keeps_cache_and_risk_stop():
    provider, state = library_provider()
    async with provider.api() as api:
        first = await api.favorites.refresh(42)
        state["repeat"] = True
        with pytest.raises(DownloadError, match="分页未推进"):
            await api.favorites.more()
        assert api.favorites.view is first
        state["deny"] = True
        with pytest.raises(DownloadError, match="风控"):
            await api.favorites.refresh(42)
        assert api.favorites.view is first and api.gate.stopped
        count = len(provider.requests)
        with pytest.raises(DownloadError):
            await api.favorites.refresh(42)
        assert len(provider.requests) == count


async def test_new_login_and_logout_scrub_account_favorites():
    provider, _ = library_provider(1)
    async with provider.api() as api:
        await api.favorites.refresh(42)
        assert api.favorites.view.items
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert not api.favorites.view.folders and not api.favorites.view.items
        await api.favorites.refresh(42)
        api.clear_session()
        assert api.session is None and not api.favorites.view.items


async def test_inconsistent_identifiers_never_certify_unchanged_content():
    provider, state = library_provider(1)
    state["ids_override"] = []
    async with provider.api() as api:
        view = await api.favorites.refresh(42)
        assert view.items and not view.digest
        await api.favorites.refresh(42)
        assert len(detail_calls(provider)) == 2
        del state["ids_override"]
        assert (await api.favorites.refresh(42)).digest


async def test_restart_restores_each_folder_and_resumes_only_the_next_page(tmp_path):
    provider, state = library_provider(51)
    provider.cache_root = tmp_path
    state["folders"].append(dict(state["folders"][0], id=11, fid=11, title="另一个收藏夹"))
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.favorites.refresh(42)
        await api.favorites.more()
        await api.favorites.open(11)
        assert len(api.favorites.view.items) == 20
    before = len(provider.requests)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert len(provider.requests) == before + 1  # Nav only; restore does not fetch folders.
        assert api.favorites.cached_view().folder_id == 11
        cached = api.favorites.cached_view(10)
        assert cached.cached and len(cached.items) == 40 and cached.has_more
        assert len(provider.requests) == before + 1
        await api.favorites.open(10)
        complete = await api.favorites.more()
        assert len(complete.items) == 51 and not complete.has_more
        assert [r.url.params["pn"] for r in detail_calls(provider)] == ["1", "2", "1", "3"]
        await api.favorites.open(11)
        assert len(api.favorites.view.items) == 20
        other = AccountCache(tmp_path, 43)
        try:
            assert not FavoriteLibrary(api.sdk.fav, cache=other).cached_view().folders
        finally:
            other.close()


async def test_favorite_pages_do_not_expire_with_messages_and_forget_is_account_scoped(tmp_path):
    provider, _ = library_provider(41)
    now = [0.0]
    cache = AccountCache(tmp_path, 42, clock=lambda: now[0])
    other = AccountCache(tmp_path, 43, clock=lambda: now[0])
    try:
        async with provider.api() as api:
            library = FavoriteLibrary(api.sdk.fav, cache=cache, clock=lambda: now[0])
            await library.refresh(42)
            await library.more()
            cache.put_many({("thread", "synthetic"): {"sample": True}})
            other.put_many({("favorites", "sentinel"): "other account"})
            now[0] = 31 * 86400
            assert cache.get("thread", "synthetic") is None
            restored = FavoriteLibrary(api.sdk.fav, cache=cache, clock=lambda: now[0])
            assert len(restored.cached_view().items) == 40
            cache.close()
            AccountCache.forget(tmp_path, 42)
            assert other.get("favorites", "sentinel", max_age=None) == "other account"
            empty = AccountCache(tmp_path, 42)
            try:
                assert not FavoriteLibrary(api.sdk.fav, cache=empty).view.folders
            finally:
                empty.close()
    finally:
        cache.close()
        other.close()


async def test_favorite_commit_failure_and_missing_page_never_advance_a_saved_cursor(tmp_path):
    provider, _ = library_provider(61)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.favorites.refresh(42)
        previous = await api.favorites.more()
        api.cache.connection.execute(
            "CREATE TRIGGER fail_page BEFORE INSERT ON records "
            "WHEN NEW.kind='favorite_page' BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
        with pytest.raises(DownloadError, match="写入失败"):
            await api.favorites.more()
        assert api.favorites.view is previous
        assert api.cache.get("favorite_folder", "10")["page_count"] == 2
        assert api.cache.get("favorite_page", "10:3") is None
        api.cache.connection.execute("DROP TRIGGER fail_page")
        api.cache.put_many({}, remove=(("favorite_page", "10:2"),))
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        # A manifest cannot resume past a missing/expired/corrupt page.
        assert not api.favorites.cached_view().items
        assert not api.favorites.view.has_more
        await api.favorites.refresh(42)
        await api.favorites.more()
        assert [r.url.params["pn"] for r in detail_calls(provider)][-2:] == ["1", "2"]
        assert len(api.favorites.view.items) == 40


async def test_metadata_check_retains_loaded_pages_but_membership_change_discards_old_pages(
    tmp_path,
):
    provider, state = library_provider(61)
    provider.cache_root = tmp_path
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        now = [0.0]
        api.favorites.clock = lambda: now[0]
        await api.favorites.refresh(42)
        await api.favorites.more()
        state["items"][0]["title"] = "最新标题"
        now[0] = 600
        view = await api.favorites.refresh(42)
        assert len(view.items) == 40 and view.items[0].title == "最新标题"
        await api.favorites.more()
        assert len(api.favorites.view.items) == 60
        state["items"][-1] = favorite_item("BV0000000070", 70)
        changed = await api.favorites.refresh(42)
        assert len(changed.items) == 20 and changed.has_more
        assert api.cache.get("favorite_folder", "10")["page_count"] == 1
        assert api.cache.get("favorite_page", "10:2") is None
        assert api.cache.get("favorite_page", "10:3") is None
        state["deny"] = True
        with pytest.raises(DownloadError, match="风控"):
            await api.favorites.more()
        assert api.favorites.view is changed


@pytest.mark.parametrize("image_status", [200, 302, 404])
async def test_login_profile_fetches_avatar_once_without_credentials(image_status):
    requests, images = [], []

    def nav(request):
        requests.append(request)
        return response(
            {
                "isLogin": True,
                "mid": 42,
                "uname": "头像测试",
                "face": "http://i0.hdslb.com/bfs/face/synthetic.png",
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/a.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/b.png",
                },
            }
        )

    def picture(request):
        images.append(request)
        assert "cookie" not in request.headers and request.url.scheme == "https"
        assert request.headers["user-agent"] == "Synthetic Chrome"
        return httpx.Response(image_status, content=PNG, headers={"content-type": "image/png"})

    async with Bilibili(
        gate=RequestGate(0, 0),
        transport=httpx.MockTransport(nav),
        image_transport=httpx.MockTransport(picture),
        fetch_avatar=True,
    ) as api:
        session = await api.use_cookie(
            "SESSDATA=synthetic; DedeUserID=42", user_agent="Synthetic Chrome"
        )
        assert session.name == "头像测试"
        assert session.avatar == (PNG if image_status == 200 else b"")
        assert len(requests) == len(images) == 1
        assert "avatar" not in repr(session) and "SESSDATA" not in repr(session)
        save_session(session)
        assert load_session().avatar == b""  # Image is not persisted alongside the credential.
        credential = (
            local_store.account_folder(local_store.data_root() / "accounts", 42) / "session.json"
        )
        assert "avatar" not in local_store.read_json(credential)


@pytest.mark.parametrize(
    "url",
    ["https://evil.invalid/a.png", "file:///tmp/a.png", "https://i0.hdslb.com@evil.invalid/a.png"],
)
async def test_avatar_never_requests_arbitrary_hosts(url):
    def no_request(request):
        raise AssertionError("untrusted avatar was requested")

    async with Bilibili(image_transport=httpx.MockTransport(no_request)) as api:
        assert await api.cdn_image(url) == b""
        assert api.gate.count == 0
