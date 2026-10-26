import httpx
import pytest
from bpi.errors import HttpStatusError
from conftest import PNG, favorite_item, library_provider, response

from bili_downloader import local_store
from bili_downloader.api import Bilibili, RequestGate
from bili_downloader.auth import load_session, save_session
from bili_downloader.sources import DownloadError


def detail_calls(provider):
    return [r for r in provider.requests if r.url.path.endswith("resource/list")]


async def test_minute_checks_refresh_directory_only_every_ten_minutes_and_force_is_immediate():
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
