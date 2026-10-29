import asyncio
import base64
import json
import os
import sqlite3
import stat

import httpx
import pytest
from account_content_provider import content_provider
from conftest import PNG, inbox_provider, private_message

from bili_downloader import cache as storage
from bili_downloader.cache import AccountCache, account_folder
from bili_downloader.inbox import message_text
from bili_downloader.message_content import MessageImage, message_content
from bili_downloader.sources import DownloadError


def persistent(provider, root):
    provider.cache_root = root
    return provider


async def test_received_messages_images_and_selection_restore_without_refetch(tmp_path, caplog):
    caplog.set_level("INFO", logger="bili_downloader")
    provider, state = inbox_provider()
    persistent(provider, tmp_path)
    url = "https://i0.hdslb.com/bfs/face/PATH_SECRET.png?token=QUERY_SECRET"
    provider.image_handler = lambda request: httpx.Response(
        200, content=PNG, headers={"content-type": "image/png"}
    )
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.inbox.refresh(42)
        await api.inbox.open(7, 42)
        await api.inbox.open(8, 42)
        assert await api.cdn_image(url) == PNG
    before, images = len(provider.requests), len(provider.images)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert len(provider.requests) == before + 1  # Identity validation only, no history scan.
        assert api.inbox.view.cached and api.inbox.thread.talker_id == 8
        assert len(api.inbox.threads[7].messages) == 20 and api.inbox.high_water[7] == 25
        before = len(provider.requests)
        assert (await api.inbox.open(7, 42)).cached
        assert (await api.inbox.open(8, 42)).cached
        assert await api.cdn_image(url) == PNG
        assert len(provider.requests) == before and len(provider.images) == images
        own_logs = "\n".join(
            r.getMessage() for r in caplog.records if r.name.startswith("bili_downloader.")
        )
        assert own_logs.count("endpoint=cdn.image") == 1
        assert "Image cache reused bytes=" in own_logs
        assert not any(secret in own_logs for secret in ("SECRET", "SESSDATA", "合成私信"))
        # A notification for a nonselected peer must retain bodies, not only a cursor.
        state["messages"].append(private_message(26))
        assert await api.inbox.sync_talker(7, 42) == 1
        assert provider.requests[-1].url.params["begin_seqno"] == "25"
        assert api.inbox.thread.talker_id == 8
        assert (await api.inbox.open(7, 42)).messages[-1].msg_seqno == 26
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert api.inbox.high_water[7] == 26
        assert message_text(api.inbox.threads[7].messages[-1]) == "合成私信 26"
    raw = (account_folder(tmp_path, 42) / "cache.sqlite3").read_bytes()
    # This is deliberately local plaintext storage, not a credential vault.
    assert PNG in raw
    assert b"SESSDATA" not in raw


async def test_emoji_metadata_and_image_restore_with_the_message_without_extra_api_reads(tmp_path):
    provider, state = inbox_provider()
    persistent(provider, tmp_path)
    url = "https://i0.hdslb.com/bfs/emote/synthetic.png"
    state["messages"][-1]["content"] = json.dumps({"content": "收到[热词系列测试]"})
    state["emojis"] = [{"text": "[热词系列测试]", "url": url, "size": 2}]
    provider.image_handler = lambda request: httpx.Response(
        200, content=PNG, headers={"content-type": "image/png"}
    )
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.inbox.refresh(42)
        before = len(provider.requests)
        view = await api.inbox.open(7, 42)
        assert len(provider.requests) == before + 1
        assert view.messages[-1].emojis[0].url == url
        assert await api.cdn_image(url) == PNG
    before, image_count = len(provider.requests), len(provider.images)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        view = await api.inbox.open(7, 42)
        assert view.cached and len(provider.requests) == before + 1
        content = message_content(view.messages[-1])
        assert [part.url for part in content.parts if isinstance(part, MessageImage)] == [url]
        assert await api.cdn_image(url) == PNG and len(provider.images) == image_count


@pytest.mark.parametrize(
    "metadata",
    [None, "unexpected", {"url": "unexpected"}, [{"text": "bad"}], [{"text": "bad", "url": 7}]],
)
async def test_optional_emoji_metadata_does_not_block_plain_message_acquisition(metadata):
    provider, state = inbox_provider()
    state["emojis"] = metadata
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.inbox.refresh(42)
        view = await api.inbox.open(7, 42)
        assert message_text(view.messages[-1]) == "合成私信 25"
        assert view.messages[-1].emojis == []


async def test_sqlite_failure_does_not_commit_cursor_or_publish_unstored_messages(tmp_path):
    provider, state = inbox_provider()
    persistent(provider, tmp_path)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.inbox.refresh(42)
        old = await api.inbox.open(7, 42)
        api.cache.connection.execute(
            "CREATE TEMP TRIGGER fail_cache BEFORE INSERT ON records WHEN NEW.kind='thread' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END"
        )
        state["messages"].append(private_message(26))
        with pytest.raises(DownloadError, match="写入失败"):
            await api.inbox.sync_talker(7, 42)
        assert api.inbox.thread is old and api.inbox.high_water[7] == 25
        assert api.cache.get("thread", "7")["high_water"] == 25
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert api.inbox.high_water[7] == 25
        assert await api.inbox.sync_talker(7, 42) == 1
        assert api.inbox.high_water[7] == 26


async def test_refresh_after_restart_drains_forward_gap_before_revalidating_latest_page(tmp_path):
    provider, state = inbox_provider()
    persistent(provider, tmp_path)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.inbox.refresh(42)
        await api.inbox.open(7, 42)
    state["messages"].extend(private_message(i) for i in range(26, 71))
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        start = len(provider.requests)
        view = await api.inbox.open(7, 42, refresh=True)
        assert [m.msg_seqno for m in view.messages] == list(range(6, 71))
        reads = provider.requests[start:]
        assert len(reads) == 4 and all(r.url.params.get("begin_seqno") == "25" for r in reads[:3])
        assert "begin_seqno" not in reads[-1].url.params
        assert api.cache.get("thread", "7")["high_water"] == 70


async def test_thread_cap_keeps_cursor_with_latest_bodies_and_older_history_is_on_demand(tmp_path):
    provider, state = inbox_provider()
    persistent(provider, tmp_path)
    state["messages"] = [private_message(i) for i in range(1, 541)]
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.inbox.refresh(42)
        await api.inbox.open(7, 42)
        for _ in range(26):
            await api.inbox.older(42)
        assert len(api.inbox.thread.messages) == 540
        record = api.cache.get("thread", "7")
        assert len(record["messages"]) == 500 and record["high_water"] == 540
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert len(api.inbox.thread.messages) == 500 and api.inbox.thread.has_more
        before = len(provider.requests)
        view = await api.inbox.older(42)
        assert len(view.messages) == 520 and len(provider.requests) == before + 1
        assert provider.requests[-1].url.params["end_seqno"] == "41"


async def test_content_resumes_saved_pages_and_never_scans_on_login(tmp_path):
    provider = persistent(content_provider(), tmp_path)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        for kind in ("dynamic", "followings", "history"):
            await api.read_account_content(kind)
    before = len(provider.requests)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert len(provider.requests) == before + 1
        for kind in ("dynamic", "followings", "history"):
            cached = api.account_content.views[kind]
            if kind == "followings":
                assert len(cached.items) == 21 and cached.cached and not cached.has_more
                continue
            assert len(cached.items) == 20 and cached.cached
            start = len(provider.requests)
            view = await api.read_account_content(kind, more=True)
            assert len(view.items) == 21 and not view.has_more
            assert len(provider.requests) == start + 1
        assert provider.requests[-1].url.params["max"] == "2"


@pytest.mark.parametrize("missing", [False, True])
async def test_stale_display_projection_does_not_disable_message_or_image_cache(tmp_path, missing):
    provider = persistent(content_provider(), tmp_path)
    url = "https://i0.hdslb.com/saved-avatar.png"
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.inbox.refresh(42)
        await api.inbox.open(7, 42)
        await api.read_account_content("dynamic")
        api.cache.put_image(url, PNG)
        stale = api.cache.get("content_block", "dynamic:0", max_age=None)
        if missing:
            api.cache.put_many({}, remove=[("content_block", "dynamic:0")])
        else:
            for row in stale:
                row.pop("image_role")
            api.cache.put_many({("content_block", "dynamic:0"): stale})
    before = len(provider.requests)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert api.cache is not None and len(provider.requests) == before + 1
        assert api.inbox.high_water[7] == 25 and len(api.inbox.threads[7].messages) == 20
        assert "dynamic" not in api.account_content.views
        assert "dynamic" not in api.account_content.frontiers
        assert await api.cdn_image(url) == PNG and not provider.images
        before = len(provider.requests)
        view = await api.read_account_content("dynamic")
        assert len(provider.requests) == before + 1 and len(view.items) == 20
        assert all(row.image_role in {"avatar", "cover"} for row in view.items)


@pytest.mark.parametrize("kind", ["dynamic", "history"])
async def test_all_manually_loaded_content_resumes_after_restart_without_refetch(tmp_path, kind):
    provider = persistent(content_provider(221), tmp_path)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.read_account_content(kind)
        for _ in range(10):
            await api.read_account_content(kind, more=True)
        assert len(api.account_content.views[kind].items) == 220
    before = len(provider.requests)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert len(provider.requests) == before + 1  # Navigation only, no history scan.
        assert len(api.account_content.views[kind].items) == 220
        view = await api.read_account_content(kind, more=True)
        assert len(view.items) == 221 and len(provider.requests) == before + 2
        if kind == "dynamic":
            assert provider.requests[-1].url.params["offset"] == "220"
        else:
            assert provider.requests[-1].url.params["max"] == "2"


def test_account_image_idle_expiry_isolation_and_precise_forget(tmp_path):
    clock = [100000.0]
    one = AccountCache(tmp_path, 42, clock=lambda: clock[0])
    two = AccountCache(tmp_path, 43, clock=lambda: clock[0])
    if os.name == "posix":
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert stat.S_IMODE(one.folder.stat().st_mode) == 0o700
        assert stat.S_IMODE((one.folder / "cache.sqlite3").stat().st_mode) == 0o600
    for i in range(3):
        one.put_image(f"image-{i}", PNG)
    one.put_many({("inbox", "head"): []})
    clock[0] += 20 * 86400
    assert one.image("image-0") == PNG
    assert one.get("inbox", "head") == []
    clock[0] += 15 * 86400
    assert one.image("image-0") == PNG
    assert one.image("image-2") is None
    assert one.get("inbox", "head") is None
    assert two.image("image-2") is None
    two.put_image("image-2", b"another account")
    clock[0] += storage.IMAGE_AGE
    assert one.image("image-0") is None
    one.close()
    AccountCache.forget(tmp_path, 42)
    assert not account_folder(tmp_path, 42).exists()
    assert account_folder(tmp_path, 43).exists()
    with pytest.raises(DownloadError, match="关闭"):
        one.put_many({("inbox", "head"): []})
    assert not account_folder(tmp_path, 42).exists()
    two.close()


def test_image_cache_has_no_entry_limit_and_survives_restart(tmp_path):
    urls = [f"https://i0.hdslb.com/cache/{index}.png" for index in range(200)]
    cache = AccountCache(tmp_path, 42)
    for url in urls:
        cache.put_image(url, PNG)
    cache.close()
    restored = AccountCache(tmp_path, 42)
    try:
        assert all(restored.image(url) == PNG for url in urls)
    finally:
        restored.close()


def test_image_blobs_have_no_total_disk_budget_and_keep_their_own_size_limit(tmp_path):
    body = b"x" * (20 * 1024 * 1024)
    cache = AccountCache(tmp_path, 42)
    try:
        cache.put_image("first", body)
        cache.put_image("second", body)
        rows = cache.connection.execute("SELECT typeof(body), length(body) FROM images").fetchall()
        assert rows == [("blob", len(body)), ("blob", len(body))]
        assert (
            cache.connection.execute("SELECT COUNT(*) FROM records WHERE kind='image'").fetchone()[
                0
            ]
            == 0
        )
        with pytest.raises(DownloadError, match="20 MiB"):
            cache.put_image("oversized", body + b"x")
    finally:
        cache.close()
    restored = AccountCache(tmp_path, 42)
    try:
        assert restored.image("first") == body and restored.image("second") == body
    finally:
        restored.close()


def test_former_base64_images_convert_without_changing_messages(tmp_path, caplog):
    cache = AccountCache(tmp_path, 42)
    cache.put_many({("inbox", "head"): ["preserved"]})
    for account_id, url in ((42, "valid"), (43, "wrong-account")):
        raw = json.dumps(
            {
                "account_id": account_id,
                "kind": "image",
                "key": url,
                "value": base64.b64encode(PNG).decode("ascii"),
            }
        ).encode()
        cache.connection.execute(
            "INSERT INTO records VALUES (?,?,?,?)",
            ("image", cache._key(url), raw, cache.clock()),
        )
    cache.connection.commit()
    cache.close()
    restored = AccountCache(tmp_path, 42)
    try:
        assert restored.image("valid") == PNG and restored.image("wrong-account") is None
        assert restored.get("inbox", "head") == ["preserved"]
        assert (
            restored.connection.execute(
                "SELECT COUNT(*) FROM records WHERE kind='image'"
            ).fetchone()[0]
            == 0
        )
        assert restored.connection.execute("SELECT typeof(body), body FROM images").fetchone() == (
            "blob",
            PNG,
        )
        assert "wrong-account" not in caplog.text
        with pytest.raises(DownloadError, match="二进制"):
            restored.put_many({("image", "new"): "not-an-image"})
    finally:
        restored.close()


def test_open_prunes_idle_images_but_hits_survive_a_month_after_original_write(tmp_path):
    now = [100000.0]
    cache = AccountCache(tmp_path, 42, clock=lambda: now[0])
    cache.put_image("used", PNG)
    cache.put_image("idle", PNG)
    now[0] += 20 * 86400
    assert cache.image("used") == PNG
    cache.close()
    now[0] += 15 * 86400
    restored = AccountCache(tmp_path, 42, clock=lambda: now[0])
    try:
        assert restored.image("used") == PNG and restored.image("idle") is None
        assert restored.connection.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 1
    finally:
        restored.close()


@pytest.mark.parametrize("size", [20 * 1024 * 1024, 20 * 1024 * 1024 + 1])
async def test_cdn_image_limit_is_twenty_mib_and_logs_only_sizes(tmp_path, caplog, size):
    provider, _ = inbox_provider()
    persistent(provider, tmp_path)
    body = b"x" * size
    provider.image_handler = lambda request: httpx.Response(
        200, content=body, headers={"content-type": "image/png"}
    )
    url = "https://i0.hdslb.com/PATH_SECRET.png?token=QUERY_SECRET"
    caplog.set_level("INFO", logger="bili_downloader")
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        result = await api.cdn_image(url)
        if size == 20 * 1024 * 1024:
            assert result == body
            assert await api.cdn_image(url) == body
        else:
            assert result == b"" and api.cache.image(url) is None
        assert len(provider.images) == 1
    own_logs = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("bili_downloader.")
    )
    assert "endpoint=cdn.image" in own_logs
    assert "SECRET" not in own_logs and "SESSDATA" not in own_logs


async def test_corrupt_cache_falls_back_to_memory_without_overwriting(tmp_path):
    provider, _ = inbox_provider()
    persistent(provider, tmp_path)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.inbox.refresh(42)
        await api.inbox.open(7, 42)
    path = account_folder(tmp_path, 42) / "cache.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE records SET value=? WHERE kind='thread'", (b"corrupt synthetic JSON",))
    with sqlite3.connect(path) as db:
        original = db.execute("SELECT kind,key,value FROM records ORDER BY kind,key").fetchall()
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert api.cache is None and "仅使用内存" in api.cache_notice
        assert not api.inbox.high_water
        await api.inbox.refresh(42)
        await api.inbox.open(7, 42)
    with sqlite3.connect(path) as db:
        assert (
            db.execute("SELECT kind,key,value FROM records ORDER BY kind,key").fetchall()
            == original
        )


async def test_symlink_cache_keeps_memory_login_without_touching_unrelated_data(tmp_path):
    provider, _ = inbox_provider()
    provider.cache_root = tmp_path / "cache"
    outside = tmp_path / "unrelated"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("unrelated data")
    provider.cache_root.symlink_to(outside, target_is_directory=True)
    async with provider.api() as api:
        session = await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert session.mid == 42 and api.cache is None and "仅使用内存" in api.cache_notice
        await api.inbox.refresh(42)
        await api.inbox.open(7, 42)
    assert list(outside.iterdir()) == [marker] and marker.read_text() == "unrelated data"


async def test_uncertain_send_is_not_saved_as_an_action_or_replayed_after_restart(tmp_path):
    provider, state = inbox_provider()
    persistent(provider, tmp_path)
    state["send_mode"] = "lost"
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42; bili_jct=synthetic-csrf")
        await api.inbox.refresh(42)
        receipt = await api.send_message(7, "不应自动重放的操作")
        assert not receipt.confirmed and api.inbox.pending is not None
    posts = state["posts"]
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42; bili_jct=synthetic-csrf")
        assert api.inbox.pending is None and state["posts"] == posts


async def test_forget_fences_late_fetch_and_removes_only_current_account_cache(tmp_path):
    provider, _ = inbox_provider()
    persistent(provider, tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    handler = provider.handler

    async def delayed(request):
        if request.url.path.endswith("fetch_session_msgs"):
            entered.set()
            await release.wait()
        return handler(request)

    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.inbox.refresh(42)
        provider.handler = delayed
        task = asyncio.create_task(api.inbox.open(7, 42))
        await entered.wait()
        await api.forget_account_cache(42)
        release.set()
        with pytest.raises(DownloadError, match="关闭"):
            await task
        assert not account_folder(tmp_path, 42).exists() and api.session is None
