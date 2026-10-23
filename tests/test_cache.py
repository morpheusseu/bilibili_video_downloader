import asyncio
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
from bili_downloader.sources import DownloadError


def persistent(provider, root):
    provider.cache_root = root
    return provider


async def test_received_messages_images_and_selection_restore_without_refetch(tmp_path):
    provider, state = inbox_provider()
    persistent(provider, tmp_path)
    url = "https://i0.hdslb.com/bfs/face/synthetic.png"
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
    assert url.encode() in raw
    assert b"SESSDATA" not in raw


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
            assert len(cached.items) == 20 and cached.cached
            start = len(provider.requests)
            view = await api.read_account_content(kind, more=True)
            assert len(view.items) == 21 and not view.has_more
            assert len(provider.requests) == start + 1
        assert provider.requests[-1].url.params["max"] == "2"


async def test_over_content_cache_cap_does_not_save_cursor_past_missing_pages(tmp_path):
    provider = persistent(content_provider(221), tmp_path)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.read_account_content("dynamic")
        for _ in range(10):
            await api.read_account_content("dynamic", more=True)
        assert len(api.account_content.views["dynamic"].items) == 220
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert len(api.account_content.views["dynamic"].items) == 200
        view = await api.read_account_content("dynamic", more=True)
        assert len(view.items) == 220 and provider.requests[-1].url.params["offset"] == "200"


def test_account_isolation_limits_expiry_and_precise_forget(tmp_path, monkeypatch):
    clock = [100000.0]
    monkeypatch.setattr(storage, "MAX_IMAGES", 2)
    one = AccountCache(tmp_path, 42, clock=lambda: clock[0])
    two = AccountCache(tmp_path, 43, clock=lambda: clock[0])
    if os.name == "posix":
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        assert stat.S_IMODE(one.folder.stat().st_mode) == 0o700
        assert stat.S_IMODE((one.folder / "cache.sqlite3").stat().st_mode) == 0o600
    for i in range(3):
        one.put_image(f"image-{i}", PNG)
        clock[0] += 1
    assert one.image("image-0") is None and one.image("image-2") == PNG
    assert two.image("image-2") is None
    two.put_image("image-2", b"another account")
    clock[0] += storage.IMAGE_AGE + 1
    assert one.image("image-2") is None
    one.close()
    AccountCache.forget(tmp_path, 42)
    assert not account_folder(tmp_path, 42).exists()
    assert account_folder(tmp_path, 43).exists()
    with pytest.raises(DownloadError, match="关闭"):
        one.put_many({("inbox", "head"): []})
    assert not account_folder(tmp_path, 42).exists()
    two.close()


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
