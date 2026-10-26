import asyncio
import threading
import time
from dataclasses import replace
from functools import partial

import httpx
import pytest
from account_content_provider import content_provider, dynamic
from broadcast_peer import BroadcastPeer, notice
from conftest import (
    BV,
    PNG,
    Provider,
    inbox_provider,
    library_provider,
    private_message,
)
from PyQt5.QtCore import QSize, Qt, QTimer
from PyQt5.QtGui import QInputMethodEvent, QPalette, QPixmap
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QMessageBox
from websockets.exceptions import ConnectionClosedError

from bili_downloader import gui
from bili_downloader.account_content import ContentItem, ContentView
from bili_downloader.auth import Session
from bili_downloader.broadcast import BroadcastClient, BroadcastState
from bili_downloader.download import Progress, Result
from bili_downloader.inbox_panel import ReplyEditor
from bili_downloader.message_rendering import CONTENT_ROLE
from bili_downloader.sources import DownloadError, Source
from bili_downloader.theme import COLORS


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_for(app, condition):
    deadline = time.monotonic() + 5
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert condition()


def history_text(panel):
    return "\n\n".join(panel.body.item(index).text() for index in range(panel.body.count()))


@pytest.fixture
def window(app, monkeypatch, request):
    class Settings:
        def value(self, key, default):
            return default

        def setValue(self, *args):
            pass

    monkeypatch.setattr(gui, "QSettings", lambda *args: Settings())
    factory = getattr(request, "param", lambda: Provider(pages=2))
    provider = factory()
    window = gui.MainWindow(api_factory=provider.api)
    window.show()
    app.processEvents()
    yield window, provider
    assert window.engine.shutdown()
    window.busy = False
    window.close()


def test_preview_uses_real_worker_without_idle_network_calls(app, window):
    widget, provider = window
    assert not provider.requests
    widget.source.setText(BV)
    widget.all_parts.setChecked(True)
    widget.preview.click()
    assert widget.busy and not widget.start_download.isEnabled()
    wait_for(app, lambda: not widget.busy)
    assert len(provider.requests) == 1
    assert "part 1" in widget.log.toPlainText() and "part 2" in widget.log.toPlainText()
    assert widget.start_download.isEnabled() and not widget.cancel.isEnabled()
    assert widget.preview_items.isVisible()
    assert widget.preview_items.topLevelItemCount() == 2
    assert widget.preview_items.topLevelItem(0).text(0) == "测试 / video"
    assert "part 1" in widget.preview_items.topLevelItem(0).text(1)
    assert widget.preview_items.topLevelItem(1).text(2) == BV
    widget.source.setText("av233802576")
    app.processEvents()
    assert widget.preview_items.topLevelItemCount() == 0
    assert widget.preview_items.isHidden()
    assert len(provider.requests) == 1


def test_profile_renders_from_login_memory_without_fetching_again(app, window):
    widget, provider = window
    assert widget.account_toggle.text() == "登录"
    assert widget.identity.isHidden()
    session = Session(42, "头像测试", "synthetic-cookie", avatar=PNG)
    widget.show_session(session)
    assert widget.identity.text() == "头像测试"
    assert not widget.account_toggle.icon().isNull()
    assert widget.account_toggle.text() == "" and widget.identity.isVisible()
    assert widget.account_toggle.width() == widget.account_toggle.height() == 44
    assert widget.library_toggle.text() == "" and not widget.library_toggle.icon().isNull()
    assert widget.library_toggle.accessibleName() == "我的收藏"
    widget.resize(760, 650)
    app.processEvents()
    assert widget.rect().contains(
        widget.account_toggle.mapTo(widget, widget.account_toggle.rect().bottomRight())
    )
    widget.show_session(session)
    assert not provider.requests
    widget.show_session(None)
    assert widget.identity.isHidden() and not widget.library_toggle.isEnabled()
    assert widget.account_toggle.text() == "登录" and widget.account_toggle.icon().isNull()
    widget.account_toggle.click()
    assert widget.account_dialog.isVisible() and not provider.requests


def test_byte_progress_preserves_video_and_audio_stream_labels(app, window):
    widget, provider = window
    for label in ("视频流（画面）", "音频流（声音）", "音视频片段 1/2（同一视频）"):
        widget.show_progress(
            Progress("bytes", f"下载{label}", 1024 * 1024, 2 * 1024 * 1024, percent=50)
        )
        assert label in widget.status.text()
        assert "1.0 MiB / 2.0 MiB" in widget.status.text()
        assert widget.bar.value() == 50
    assert not provider.requests


@pytest.mark.parametrize("window", [lambda: inbox_provider()[0]], indirect=True)
def test_floating_login_remembers_by_default_and_user_can_opt_out(app, window, monkeypatch):
    widget, provider = window
    saved = []
    monkeypatch.setattr(gui, "save_session", saved.append)

    async def browser(api, report):
        report("3/4 · 合成身份验证")
        return await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")

    monkeypatch.setattr(gui, "chrome_login", browser)
    dialog = widget.account_dialog
    assert dialog.isWindow() and dialog.remember.isChecked()
    widget.account_toggle.click()
    assert dialog.isVisible()
    dialog.login.click()
    wait_for(app, lambda: not widget.busy and bool(widget._library_view.items))
    assert len(saved) == 1 and saved[0].mid == 42
    assert dialog.isHidden() and not widget.account_toggle.isChecked()
    widget.account_toggle.click()
    dialog.remember.setChecked(False)
    dialog.login.click()
    wait_for(app, lambda: not widget.busy)
    assert len(saved) == 1 and dialog.isHidden()


@pytest.mark.parametrize("window", [lambda: inbox_provider()[0]], indirect=True)
def test_closing_login_float_cancels_before_saving(app, window, monkeypatch):
    widget, provider = window
    entered = threading.Event()
    saved = []
    monkeypatch.setattr(gui, "save_session", saved.append)

    async def browser(api, report):
        entered.set()
        report("2/4 · 等待合成扫码")
        await asyncio.Event().wait()

    monkeypatch.setattr(gui, "chrome_login", browser)
    widget.account_toggle.click()
    widget.account_dialog.login.click()
    wait_for(app, lambda: entered.is_set() and "2/4" in widget.account_dialog.status.text())
    widget.account_dialog.reject()
    wait_for(app, lambda: not widget.busy)
    assert not saved and widget.account_dialog.isHidden()
    assert not widget.account_toggle.isChecked() and "取消" in widget.status.text()


def test_reply_enter_and_ime_commit_do_not_submit(app):
    editor = ReplyEditor()
    submitted = []
    editor.submit_requested.connect(lambda: submitted.append(True))
    editor.show()
    editor.setPlainText("你好")
    editor.moveCursor(editor.textCursor().End)
    QTest.keyClick(editor, Qt.Key_Return)
    assert not submitted and "\n" in editor.toPlainText()
    QApplication.sendEvent(editor, QInputMethodEvent("候选词", []))
    QTest.keyClick(editor, Qt.Key_Return, Qt.ControlModifier)
    assert not submitted
    QApplication.sendEvent(editor, QInputMethodEvent("", []))
    QTest.keyClick(editor, Qt.Key_Return, Qt.ControlModifier)
    assert submitted == [True]
    editor.close()


def test_closed_worker_rejects_late_login_refresh_and_artwork(app, window):
    widget, provider = window
    assert widget.engine.shutdown()
    widget._session_mid = 42
    widget.load_library()
    widget.engine.request_artwork("https://i0.hdslb.com/bfs/face/late.png")
    with pytest.raises(DownloadError, match="后台已关闭"):
        widget.engine.submit(lambda api: api.sdk.login.nav())
    app.processEvents()
    assert not widget.busy and not provider.requests and not provider.images


def avatar_inbox_provider():
    provider, state = inbox_provider()
    state["faces"] = {
        7: "https://i0.hdslb.com/bfs/face/seven.png",
        8: "https://i0.hdslb.com/bfs/face/eight.png",
    }
    provider.image_handler = lambda request: httpx.Response(
        200, content=PNG, headers={"content-type": "image/png"}
    )
    return provider


def open_test_inbox(widget, app):
    widget.launch(
        lambda api: api.use_cookie("SESSDATA=synthetic; DedeUserID=42; bili_jct=synthetic-csrf")
    )
    wait_for(app, lambda: not widget.busy and bool(widget._library_view.items))
    widget.inbox_toggle.click()
    wait_for(app, lambda: not widget.busy)
    widget.inbox_panel.sessions.setCurrentItem(widget.inbox_panel.sessions.topLevelItem(0))
    wait_for(app, lambda: not widget.busy)


@pytest.mark.parametrize("window", [lambda: inbox_provider()[0]], indirect=True)
def test_private_reply_and_status_remain_visible_in_small_window(app, window):
    widget, provider = window
    open_test_inbox(widget, app)
    widget.resize(760, 650)
    app.processEvents()
    app.processEvents()
    for control in (
        widget.inbox_panel.reply,
        widget.inbox_panel.send,
        widget.inbox_panel.send_notice,
    ):
        assert control.visibleRegion().contains(control.rect().center())
        assert widget.rect().contains(control.mapTo(widget, control.rect().bottomRight()))


@pytest.mark.parametrize("window", [avatar_inbox_provider], indirect=True)
def test_avatars_drafts_and_real_confirmation_to_single_post_receipt(app, window, monkeypatch):
    widget, provider = window
    monkeypatch.setattr(gui.QApplication, "applicationState", lambda: Qt.ApplicationActive)
    open_test_inbox(widget, app)
    panel = widget.inbox_panel
    face = "https://i0.hdslb.com/bfs/face/seven.png"
    wait_for(app, lambda: face in widget._artwork and widget._artwork_pending is None)
    assert sum(str(request.url) == face for request in provider.images) == 1
    assert all(not panel.body.item(index).icon().isNull() for index in range(panel.body.count()))
    panel.reply.setPlainText("文字回复 <plain>\n第二行")
    panel.sessions.setCurrentItem(panel.sessions.topLevelItem(1))
    wait_for(app, lambda: not widget.busy)
    assert not panel.reply.toPlainText()
    panel.reply.setPlainText("另一个人的草稿")
    panel.sessions.setCurrentItem(panel.sessions.topLevelItem(0))
    wait_for(app, lambda: not widget.busy)
    assert panel.reply.toPlainText() == "文字回复 <plain>\n第二行"
    confirmations = []

    def decide(answer):
        box = app.activeModalWidget()
        confirmations.append((box.text(), box.informativeText(), box.textFormat()))
        box.done(answer)

    QTimer.singleShot(0, lambda: decide(QMessageBox.No))
    panel.send.click()
    assert provider.inbox_state["posts"] == 0 and panel.reply.toPlainText()
    QTimer.singleShot(0, lambda: decide(QMessageBox.Yes))
    panel.send.click()
    assert widget.busy and not widget.cancel.isEnabled()
    wait_for(app, lambda: not widget.busy)
    assert all("UID 7" in row[0] and row[2] == Qt.PlainText for row in confirmations)
    assert confirmations[-1][1] == "文字回复 <plain>\n第二行"
    assert provider.inbox_state["posts"] == 1
    assert not panel.pending and not panel.reply.toPlainText()
    assert "回读确认" in panel.send_notice.text()
    assert panel.body.item(panel.body.count() - 1).icon().cacheKey() == panel.self_icon.cacheKey()
    assert "第二行" in history_text(panel) and "第二行" not in widget.log.toPlainText()


@pytest.mark.parametrize("window", [lambda: inbox_provider()[0]], indirect=True)
def test_uncertain_reply_locks_resend_until_explicit_readback(app, window, monkeypatch):
    widget, provider = window
    open_test_inbox(widget, app)
    panel = widget.inbox_panel
    state = provider.inbox_state
    state["send_mode"] = "lost"
    monkeypatch.setattr(widget, "confirm_reply", lambda *args: True)
    panel.reply.setPlainText("待核对的私信")
    panel.send.click()
    wait_for(app, lambda: not widget.busy)
    assert panel.pending and not panel.send.isEnabled() and panel.check_send.isEnabled()
    assert panel.reply.isReadOnly() and panel.reply.toPlainText() == "待核对的私信"
    assert not panel.sessions.isEnabled()
    panel.send.click()
    assert state["posts"] == 1
    state["sent"][7] = [
        private_message(26, sender_uid=42, receiver_id=7, content='{"content":"待核对的私信"}')
    ]
    panel.check_send.click()
    wait_for(app, lambda: not widget.busy)
    assert not panel.pending and not panel.reply.toPlainText()
    assert not panel.check_send.isEnabled() and state["posts"] == 1


@pytest.mark.parametrize("window", [lambda: inbox_provider()[0]], indirect=True)
def test_mail_button_reads_only_selected_thread_and_scrubs_on_logout(app, window, monkeypatch):
    widget, provider = window
    monkeypatch.setattr(gui, "forget_session", lambda account_id: None)
    assert not widget.inbox_toggle.isEnabled()
    widget.launch(lambda api: api.use_cookie("SESSDATA=synthetic; DedeUserID=42"))
    wait_for(app, lambda: not widget.busy and bool(widget._library_view.items))
    assert not any(r.url.host == "api.vc.bilibili.com" for r in provider.requests)
    widget.inbox_toggle.click()
    wait_for(app, lambda: not widget.busy)
    panel = widget.inbox_panel
    assert panel.isVisible() and not widget.library_panel.isVisible()
    assert panel.sessions.topLevelItemCount() == 3 and not panel.body.count()
    panel.sessions.setCurrentItem(panel.sessions.topLevelItem(0))
    wait_for(app, lambda: not widget.busy)
    assert panel.thread.talker_id == 7 and len(panel.thread.messages) == 20
    assert "合成私信 25" in history_text(panel)
    assert panel.sessions.topLevelItem(0).text(2) == "2"
    panel.older.click()
    wait_for(app, lambda: not widget.busy)
    text = history_text(panel)
    assert len(panel.thread.messages) == 25 and not panel.older.isEnabled()
    assert text.index("\n合成私信 1\n\n") < text.index("\n合成私信 20\n\n")
    count = len(provider.requests)
    widget.inbox_toggle.click()
    widget.inbox_toggle.click()
    assert len(provider.requests) == count  # Opening the cached list never polls.
    widget.account_dialog.forget.click()
    wait_for(app, lambda: not widget.busy)
    assert not widget.inbox_toggle.isEnabled() and panel.isHidden()
    assert not panel.body.count() and not panel.sessions.topLevelItemCount()
    assert widget.engine.api.inbox.thread is None
    assert all(r.method == "GET" for r in provider.requests)
    assert "合成私信" not in widget.log.toPlainText()


def cover_provider(count=21):
    provider, state = library_provider(count)
    for index, item in enumerate(state["items"]):
        item["cover"] = f"http://i0.hdslb.com/bfs/archive/cover{max(1, index)}.png"

    def image(request):
        assert request.url.scheme == "https" and "cookie" not in request.headers
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    provider.image_handler = image
    return provider


def visible_cover_urls(widget):
    region = widget.favorite_items.viewport().visibleRegion()
    return {
        item.data(0, Qt.UserRole + 1)
        for index in range(widget.favorite_items.topLevelItemCount())
        if region.intersects(
            widget.favorite_items.visualItemRect(item := widget.favorite_items.topLevelItem(index))
        )
    }


@pytest.mark.parametrize("window", [cover_provider], indirect=True)
def test_visible_favorite_covers_are_cached_and_never_fetch_hidden_rows(app, window, monkeypatch):
    widget, provider = window
    monkeypatch.setattr(gui.QApplication, "applicationState", lambda: Qt.ApplicationActive)
    widget.launch(lambda api: api.use_cookie("SESSDATA=synthetic; DedeUserID=42"))
    wait_for(app, lambda: not widget.busy and bool(widget._library_view.items))
    app.processEvents()
    visible = visible_cover_urls(widget)
    assert visible and len(visible) < 20
    wait_for(app, lambda: visible <= widget._artwork.keys() and widget._artwork_pending is None)
    assert len(provider.images) == len(visible)  # Same URL on two entries is downloaded once.
    first = widget.favorite_items.topLevelItem(0)
    first_url = first.data(0, Qt.UserRole + 1)
    assert first.icon(0).cacheKey() != widget._cover_placeholder.cacheKey()
    count = len(provider.images)
    widget.load_library()
    wait_for(app, lambda: not widget.busy)
    widget.load_visible_artwork()
    assert len(provider.images) == count
    assert widget.engine.api.gate.count == len(provider.requests) + len(provider.images)
    widget.library_toggle.click()
    widget.load_visible_artwork()
    assert len(provider.images) == count
    widget.show_session(None)
    assert not widget._artwork and widget._artwork_pending is None
    widget.show_artwork(42, first_url, PNG, "")
    assert not widget._artwork  # Late image replies cannot repopulate a logged-out account.


@pytest.mark.parametrize("window", [lambda: cover_provider(1)], indirect=True)
def test_slow_cover_is_preempted_by_foreground_operation(app, window, monkeypatch):
    widget, provider = window
    entered, cancelled = threading.Event(), threading.Event()
    monkeypatch.setattr(gui.QApplication, "applicationState", lambda: Qt.ApplicationActive)

    async def image(request):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    provider.image_handler = image
    widget.launch(lambda api: api.use_cookie("SESSDATA=synthetic; DedeUserID=42"))
    wait_for(app, lambda: not widget.busy and entered.is_set())
    assert not widget.busy and widget._artwork_pending
    widget.library_toggle.click()
    widget.source.setText(BV)
    widget.preview.click()
    wait_for(app, lambda: not widget.busy and widget._artwork_pending is None)
    assert cancelled.is_set() and len(provider.images) == 1
    assert widget.preview_items.isVisible() and widget.task_title.text() == "来源已检查"
    assert "未能完成" not in widget.log.toPlainText()


def test_download_tray_counts_files_not_streams_and_acknowledges_on_open(
    app, window, monkeypatch, tmp_path, media
):
    widget, provider = window
    real_download = gui.download_source
    requests = []

    def cdn(request):
        requests.append(request)
        return httpx.Response(200, content=media[request.url.path.lstrip("/")])

    async def download(*args, **kwargs):
        return await real_download(*args, **kwargs, media_transport=httpx.MockTransport(cdn))

    monkeypatch.setattr(gui, "download_source", download)
    assert widget.task_details.isHidden() and not widget.details.isChecked()
    assert widget.details.new_count == 0 and not widget.details.active
    widget.source.setText(BV)
    widget.all_parts.setChecked(True)
    widget.destination.setText(str(tmp_path))
    widget.start_download.click()
    assert widget.task_details.isVisible() and widget.details.active
    wait_for(app, lambda: not widget.busy)
    assert len(list(tmp_path.glob("*.mp4"))) == 2 and len(requests) == 4
    assert widget.task_details.isHidden() and not widget.details.active
    assert widget.details.new_count == 2 and "新完成 2 个文件" in widget.details.accessibleName()
    assert "视频流（画面）" in widget.log.toPlainText()
    assert "音频流（声音）" in widget.log.toPlainText()
    assert widget.start_download.isVisible() and widget.start_download.isEnabled()
    widget.details.click()
    assert widget.log.isVisible() and widget.details.new_count == 0
    widget.start_download.click()
    wait_for(app, lambda: not widget.busy)
    assert widget.task_details.isHidden() and widget.details.new_count == 0
    assert "0 个文件已生成，2 个已存在" in widget.status.text()
    assert len(requests) == 4  # No media transfers and no badge for existing output.


@pytest.mark.parametrize("interrupt", ["cancel", "error"])
def test_collapsed_download_keeps_cancel_and_partial_results(
    app, window, monkeypatch, tmp_path, media, interrupt
):
    widget, provider = window
    real_download = gui.download_source
    second_video, release = threading.Event(), asyncio.Event()
    videos = 0

    async def cdn(request):
        nonlocal videos
        if request.url.path == "/video":
            videos += 1
            if videos == 2:
                second_video.set()
                await release.wait()
                return httpx.Response(503)
        return httpx.Response(200, content=media[request.url.path.lstrip("/")])

    async def download(*args, **kwargs):
        return await real_download(*args, **kwargs, media_transport=httpx.MockTransport(cdn))

    monkeypatch.setattr(gui, "download_source", download)
    widget.source.setText(BV)
    widget.all_parts.setChecked(True)
    widget.destination.setText(str(tmp_path))
    widget.start_download.click()
    wait_for(
        app,
        lambda: second_video.is_set()
        and widget.details.new_count == 1
        and widget.details.percent == 0,
    )
    widget.details.click()
    assert widget.task_details.isHidden() and widget.details.active
    assert widget.details.new_count == 1  # First output is durable, second is still pending.
    assert widget.cancel.isVisible() and widget.cancel.isEnabled()
    assert widget.details.percent == 0 and "当前文件约 0%" in widget.details.toolTip()
    if interrupt == "cancel":
        widget.cancel.click()
    else:
        widget.engine.loop.call_soon_threadsafe(release.set)
    wait_for(app, lambda: not widget.busy)
    assert widget.task_details.isVisible() and not widget.details.active
    assert (
        "取消" in widget.status.text() if interrupt == "cancel" else "503" in widget.status.text()
    )
    assert len(list(tmp_path.glob("*.mp4"))) == 1
    assert not list(tmp_path.glob(".bili-download-*"))
    assert widget.start_download.isEnabled()


def test_ring_preserves_known_percentage_and_marks_unknown_lengths(app, window):
    widget, provider = window
    widget._job = "download"
    widget.show_progress(Progress("bytes", "下载视频流（画面）", 2, 4, percent=24))
    assert widget.details.active and widget.details.percent == widget.bar.value() == 24
    assert "24%" in widget.details.accessibleName()
    widget.show_progress(Progress("bytes", "下载音频流（声音）", 1, None))
    assert widget.details.active and widget.details.percent is None
    assert widget.bar.maximum() == 0 and "进度未知" in widget.details.accessibleName()
    assert not provider.requests


@pytest.mark.parametrize("window", [lambda: library_provider()[0]], indirect=True)
def test_login_favorites_browse_cache_and_fill_download_source(app, window, monkeypatch):
    widget, provider = window
    widget.launch(lambda api: api.use_cookie("SESSDATA=synthetic; DedeUserID=42"))
    wait_for(app, lambda: not widget.busy and widget.favorite_items.topLevelItemCount() == 20)
    assert widget.identity.text() == "Synthetic" and widget.library_panel.isVisible()
    assert widget.folders.currentData() == 10
    first_item = widget.favorite_items.topLevelItem(0)
    widget.favorite_items.setCurrentItem(first_item)
    body_count = sum(r.url.path.endswith("resource/list") for r in provider.requests)
    monkeypatch.setattr(gui.QApplication, "applicationState", lambda: Qt.ApplicationActive)
    widget.check_library()
    wait_for(app, lambda: not widget.busy)
    assert widget.favorite_items.currentItem() is first_item
    assert sum(r.url.path.endswith("resource/list") for r in provider.requests) == body_count
    assert sum(r.url.path.endswith("list-all") for r in provider.requests) == 1
    widget.refresh_library.click()
    wait_for(app, lambda: not widget.busy)
    assert sum(r.url.path.endswith("list-all") for r in provider.requests) == 2
    assert sum(r.url.path.endswith("resource/list") for r in provider.requests) == body_count + 1
    count = len(provider.requests)
    widget.set_busy(True)
    widget.check_library()
    widget.set_busy(False)
    with monkeypatch.context() as context:
        context.setattr(widget, "isMinimized", lambda: True)
        widget.check_library()
    assert len(provider.requests) == count
    widget.more_favorites.click()
    wait_for(app, lambda: not widget.busy and widget.favorite_items.topLevelItemCount() == 21)
    count = len(provider.requests)
    widget.use_favorite.click()
    assert widget.source.text() == "BV0000000001" and widget.mode.currentData() == "auto"
    assert not widget.library_panel.isVisible() and len(provider.requests) == count
    widget.check_library()
    assert len(provider.requests) == count  # Collapsed panel never polls.
    widget.library_toggle.click()
    monkeypatch.setattr(gui.QApplication, "applicationState", lambda: Qt.ApplicationInactive)
    widget.check_library()
    assert len(provider.requests) == count
    widget.use_folder.click()
    assert widget.source.text() == "10" and widget.mode.currentData() == "favorite"
    assert len(provider.requests) == count  # Filling a source never starts a download.

    async def denied(api):
        raise DownloadError("合成刷新失败")

    widget.launch(denied, kind="library")
    wait_for(app, lambda: not widget.busy)
    assert not widget.auto_library.isChecked()
    assert widget.favorite_items.topLevelItemCount() == 21
    assert "自动检查已暂停" in widget.library_hint.text()


def test_cancel_and_error_leave_ui_usable(app, window):
    widget, _ = window

    async def wait(api):
        await asyncio.Event().wait()

    widget.launch(wait)
    widget.cancel.click()
    wait_for(app, lambda: not widget.busy)
    assert "取消" in widget.status.text()

    async def fail(api):
        raise DownloadError("synthetic safe error")

    widget.launch(fail)
    wait_for(app, lambda: not widget.busy)
    assert widget.status.text() == "synthetic safe error"
    assert widget.preview.isEnabled()


def test_invalid_input_does_not_start_request(app, window):
    widget, provider = window
    widget.source.setText("https://example.com/not-bilibili")
    widget.start_download.click()
    app.processEvents()
    assert not widget.busy and not provider.requests
    assert "bilibili.com" in widget.status.text()


def test_source_and_output_options_keep_their_semantics_without_requests(app, window):
    widget, provider = window
    assert widget.pattern.isHidden()
    assert widget.page.isEnabled()
    assert widget.quality.isEnabled()

    widget.mode.setCurrentIndex(widget.mode.findData("user"))
    assert widget.pattern.isVisible() and widget.pattern.isEnabled()
    widget.pattern.setText("piano.*")
    widget.all_parts.setChecked(True)
    widget.format.setCurrentText("mp3")
    assert not widget.page.isEnabled()
    assert not widget.quality.isEnabled()

    widget.set_busy(True)
    assert not widget.pattern.isEnabled()
    assert not widget.preview.isEnabled()
    assert widget.cancel.isEnabled()
    widget.set_busy(False)
    assert widget.pattern.isEnabled()
    assert not widget.page.isEnabled()
    assert not widget.quality.isEnabled()

    widget.all_parts.setChecked(False)
    widget.format.setCurrentText("mp4")
    widget.mode.setCurrentIndex(widget.mode.findData("favorite"))
    assert widget.page.isEnabled() and widget.quality.isEnabled()
    assert widget.pattern.isHidden()
    widget.account_toggle.click()
    widget.details.click()
    app.processEvents()
    assert not provider.requests


def test_small_window_keeps_task_actions_reachable_and_cancellable(app, window):
    widget, provider = window
    widget.resize(760, 650)
    widget.account_toggle.setChecked(True)
    widget.details.setChecked(True)
    app.processEvents()

    for button in (widget.preview, widget.start_download, widget.cancel):
        assert button.isVisible()
        assert button.visibleRegion().contains(button.rect().center())
        assert widget.rect().contains(button.mapTo(widget, button.rect().bottomRight()))

    async def pending(api):
        await asyncio.Event().wait()

    widget.launch(pending)
    assert widget.cancel.isEnabled()
    assert widget.cancel.visibleRegion().contains(widget.cancel.rect().center())
    widget.cancel.click()
    wait_for(app, lambda: not widget.busy)
    assert "取消" in widget.status.text()
    assert widget.preview.isEnabled() and widget.start_download.isEnabled()
    assert not provider.requests


def test_download_captures_options_and_keeps_completed_progress(app, window, monkeypatch, tmp_path):
    widget, provider = window
    captured = {}
    persisted = {}
    monkeypatch.setattr(widget.settings, "setValue", persisted.__setitem__)

    async def synthetic_download(api, source, output, **options):
        captured.update(source=source, output=output, options=options.copy())
        assert api is widget.engine.api
        options["report"](Progress("convert", "合成转换", percent=95))
        return [Result(output / "generated.wav"), Result(output / "existing.wav", skipped=True)]

    monkeypatch.setattr(gui, "download_source", synthetic_download)
    widget.mode.setCurrentIndex(widget.mode.findData("user"))
    widget.source.setText("398421039")
    widget.pattern.setText("piano.*")
    widget.page.setValue(3)
    widget.all_parts.setChecked(True)
    widget.quality.setCurrentIndex(widget.quality.findData(32))
    widget.format.setCurrentText("wav")
    widget.destination.setText(str(tmp_path))
    widget.ffmpeg.setText("synthetic-ffmpeg")
    widget.start_download.click()
    wait_for(app, lambda: not widget.busy)

    assert captured["source"] == Source("user", "398421039", pattern="piano.*")
    assert captured["output"] == tmp_path.resolve()
    options = captured["options"]
    assert {key: value for key, value in options.items() if key != "report"} == {
        "format": "wav",
        "quality": 32,
        "all_parts": True,
        "page": 3,
        "ffmpeg": "synthetic-ffmpeg",
    }
    assert callable(options["report"])
    assert persisted == {"output": str(tmp_path.resolve()), "ffmpeg": "synthetic-ffmpeg"}
    assert widget.bar.maximum() == 100 and widget.bar.value() == 100
    assert "1 个文件已生成" in widget.status.text()
    assert "1 个已存在" in widget.status.text()
    assert widget.start_download.isEnabled() and not widget.cancel.isEnabled()
    assert not provider.requests


def test_local_save_failure_stays_visible_without_losing_memory_login(app, window, monkeypatch):
    widget, provider = window
    session = Session(mid=42, name="Synthetic user", cookie="fake-cookie")

    def cannot_save(value):
        raise DownloadError("合成本机存储不可用")

    monkeypatch.setattr(gui, "save_session", cannot_save)

    async def login(api):
        api.session = session
        return widget.finish_login(session, remember=True)

    widget.launch(login)
    wait_for(app, lambda: not widget.busy)
    assert widget.task_title.text() == "未能完成"
    assert "当前登录可用，但未能保存" in widget.status.text()
    assert widget.identity.text() == "Synthetic user"
    assert widget.account_dialog.identity.text() == "Synthetic user · UID 42"
    assert not provider.requests


def live_inbox_provider():
    provider, _ = inbox_provider()
    provider.broadcast_peer = BroadcastPeer()
    provider.broadcast_factory = partial(
        BroadcastClient, connect_factory=provider.broadcast_peer.connect
    )
    return provider


def paused_live_inbox_provider():
    provider = live_inbox_provider()
    provider.allow_connect = asyncio.Event()

    async def connect(*args, **kwargs):
        await provider.allow_connect.wait()
        return await provider.broadcast_peer.connect(*args, **kwargs)

    provider.broadcast_factory = partial(BroadcastClient, connect_factory=connect)
    return provider


@pytest.mark.parametrize("window", [paused_live_inbox_provider], indirect=True)
def test_foreground_resume_retry_indicator_and_gap_recovery_through_real_worker(
    app, window, monkeypatch
):
    import bili_downloader.broadcast as module

    monkeypatch.setattr(module, "RECONNECT_BASE_SECONDS", 0.6)
    widget, provider = window
    panel, peer = widget.inbox_panel, provider.broadcast_peer
    widget.launch(
        lambda api: api.use_cookie(
            "SESSDATA=synthetic; DedeUserID=42", user_agent="Synthetic Browser"
        )
    )
    wait_for(app, lambda: not widget.busy and widget._live_kind == "connecting")
    widget.inbox_toggle.click()

    def dot():
        return panel.connection_dot.grab().toImage().pixelColor(5, 5).name()

    assert dot() == COLORS["yellow"]
    widget.engine.loop.call_soon_threadsafe(provider.allow_connect.set)
    wait_for(
        app,
        lambda: not widget.busy
        and panel.sessions.topLevelItemCount()
        and widget._live_kind == "connected",
    )
    panel.sessions.setCurrentItem(panel.sessions.topLevelItem(0))
    wait_for(app, lambda: not widget.busy and panel.thread is not None)
    panel.reply.setPlainText("重连后保留的草稿")
    assert dot() == COLORS["green"]
    heartbeat = peer.heartbeat_count
    app.applicationStateChanged.emit(Qt.ApplicationInactive)
    app.applicationStateChanged.emit(Qt.ApplicationActive)
    wait_for(app, lambda: peer.heartbeat_count == heartbeat + 1)
    assert peer.connections == 1
    widget._last_activity_check = time.time() - 60
    widget.activity_timer.timeout.emit()
    wait_for(app, lambda: peer.heartbeat_count == heartbeat + 2)
    widget.activity_timer.timeout.emit()
    QTest.qWait(30)
    assert peer.heartbeat_count == heartbeat + 2  # Ordinary local timer ticks send nothing.
    new = private_message(26)
    provider.inbox_state["messages"].append(new)
    provider.inbox_state["lanes"][1][0].update(max_seqno=26, last_msg=new)
    widget.engine.loop.call_soon_threadsafe(
        peer.incoming.put_nowait, ConnectionClosedError(None, None)
    )
    wait_for(app, lambda: widget._live_kind == "retry_wait")
    assert dot() == COLORS["red"] and "断开" in panel.connection_dot.toolTip()
    wait_for(
        app,
        lambda: peer.connections == 2
        and widget._live_kind == "connected"
        and "合成私信 26" in history_text(panel),
    )
    assert dot() == COLORS["green"] and panel.reply.toPlainText() == "重连后保留的草稿"
    assert panel.selected() == 7 and all(request.method == "GET" for request in provider.requests)
    widget.logout()
    wait_for(app, lambda: not widget.busy and widget._session is None and peer.closed)
    assert dot() == COLORS["red"]
    app.applicationStateChanged.emit(Qt.ApplicationActive)
    widget._last_activity_check = time.time() - 60
    widget.activity_timer.timeout.emit()
    QTest.qWait(30)
    assert peer.connections == 2


@pytest.mark.parametrize("window", [live_inbox_provider], indirect=True)
def test_broadcast_to_sdk_to_ui_without_manual_refresh_or_read_ack(app, window, monkeypatch):
    widget, provider = window
    monkeypatch.setattr(gui, "forget_session", lambda account_id: None)
    widget.launch(
        lambda api: api.use_cookie(
            "SESSDATA=synthetic; DedeUserID=42", user_agent="Synthetic Browser"
        )
    )
    wait_for(app, lambda: not widget.busy and widget.inbox_panel.view is not None)
    widget.inbox_toggle.click()
    panel = widget.inbox_panel
    panel.sessions.setCurrentItem(panel.sessions.topLevelItem(0))
    wait_for(app, lambda: not widget.busy and panel.thread is not None)
    panel.reply.setPlainText("不要清除这份草稿")
    peer = provider.broadcast_peer
    before = len(provider.requests)
    new = private_message(26)
    state = provider.inbox_state
    state["messages"].append(new)
    state["lanes"][1][0].update(last_msg=new, max_seqno=26, unread_count=3)
    widget.engine.loop.call_soon_threadsafe(peer.incoming.put_nowait, notice())
    widget.engine.loop.call_soon_threadsafe(peer.incoming.put_nowait, notice(metadata=True))
    wait_for(
        app,
        lambda: panel.thread.messages[-1].msg_seqno == 26
        and panel.sessions.currentItem().text(2) == "3",
    )
    assert "合成私信 26" in history_text(panel)
    assert panel.reply.toPlainText() == "不要清除这份草稿" and panel.selected() == 7
    reads = [r for r in provider.requests[before:] if r.url.path.endswith("fetch_session_msgs")]
    assert len(reads) == 1 and reads[0].url.params["begin_seqno"] == "25"
    assert "合成私信" not in widget.log.toPlainText() and all(
        r.method == "GET" for r in provider.requests
    )
    assert peer.connections == 1 and "通知 2 次" in panel.live_status.text()

    # A foreground task does not own the socket or discard incoming wakeups.
    entered, release = threading.Event(), asyncio.Event()

    async def foreground(api):
        entered.set()
        await release.wait()

    widget.launch(foreground)
    wait_for(app, entered.is_set)
    state["messages"].append(private_message(27))
    widget.engine.loop.call_soon_threadsafe(peer.incoming.put_nowait, notice())
    wait_for(app, lambda: panel.thread.messages[-1].msg_seqno == 27)
    assert widget.busy and panel.reply.toPlainText() == "不要清除这份草稿"
    widget.engine.loop.call_soon_threadsafe(release.set)
    wait_for(app, lambda: not widget.busy)

    owner, old_view, old_thread = widget._session, panel.view, panel.thread
    widget.logout()
    wait_for(app, lambda: not widget.busy and widget._session is None and peer.closed)
    widget.show_live_inbox(owner, old_view, old_thread, 1)
    widget.show_live_status(owner, BroadcastState("connected", "stale callback"))
    assert not panel.body.count() and not panel.sessions.topLevelItemCount()
    assert "stale callback" not in panel.live_status.text()


@pytest.mark.parametrize("window", [content_provider], indirect=True)
def test_account_browser_real_worker_tabs_pagination_caching_and_logout(app, window, monkeypatch):
    widget, provider = window
    monkeypatch.setattr(gui, "forget_session", lambda account_id: None)
    assert not widget.content_toggle.isEnabled()
    widget.launch(lambda api: api.use_cookie("SESSDATA=synthetic; DedeUserID=42"))
    wait_for(app, lambda: not widget.busy and bool(widget._library_view.items))
    assert not any(
        "/feed/" in r.url.path or "history/" in r.url.path or "followings" in r.url.path
        for r in provider.requests
    )
    widget.content_toggle.click()
    panel = widget.content_panel
    wait_for(app, lambda: not widget.busy and panel.items.topLevelItemCount() == 20)
    assert panel.isVisible() and widget.library_panel.isHidden() and widget.inbox_panel.isHidden()
    assert panel.more.isEnabled() and panel.auto.isVisible()
    panel.items.setCurrentItem(panel.items.topLevelItem(0))
    assert "<plain>" in panel.body.toPlainText() and "图片 1 张" in panel.body.toPlainText()
    panel.more.click()
    wait_for(app, lambda: not widget.busy and panel.items.topLevelItemCount() == 21)
    assert not panel.more.isEnabled()
    for index, expected in ((1, "合成签名"), (2, "播放进度 12 秒")):
        panel.tabs.setCurrentIndex(index)
        wait_for(app, lambda: not widget.busy and panel.items.topLevelItemCount() == 20)
        assert panel.auto.isHidden()
        panel.items.setCurrentItem(panel.items.topLevelItem(0))
        assert expected in panel.body.toPlainText()
    count = len(provider.requests)
    panel.tabs.setCurrentIndex(0)
    assert panel.items.topLevelItemCount() == 21 and len(provider.requests) == count
    opened = []
    monkeypatch.setattr(gui.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    panel.items.setCurrentItem(panel.items.topLevelItem(0))
    panel.open.click()
    assert opened == ["https://t.bilibili.com/21"]
    assert len(provider.requests) == count  # Opening the fixed canonical URL is user initiated.
    widget.resize(760, 650)
    app.processEvents()
    assert widget.rect().contains(
        widget.account_toggle.mapTo(widget, widget.account_toggle.rect().bottomRight())
    )
    assert panel.refresh.visibleRegion().contains(panel.refresh.rect().center())
    assert "合成动态" not in widget.log.toPlainText() and "合成观看" not in widget.log.toPlainText()
    widget.logout()
    wait_for(app, lambda: not widget.busy)
    assert not panel.views and not panel.items.topLevelItemCount() and not panel.body.toPlainText()
    assert not widget.content_toggle.isEnabled() and panel.isHidden()
    assert not widget.engine.api.account_content.views
    assert all(r.method == "GET" for r in provider.requests)


@pytest.mark.parametrize("window", [content_provider], indirect=True)
def test_dynamic_checks_only_visible_foreground_idle_and_pause_on_error(app, window, monkeypatch):
    widget, provider = window
    widget.launch(lambda api: api.use_cookie("SESSDATA=synthetic; DedeUserID=42"))
    wait_for(app, lambda: not widget.busy and bool(widget._library_view.items))
    widget.content_toggle.click()
    panel = widget.content_panel
    wait_for(app, lambda: not widget.busy)
    first = panel.items.topLevelItem(0)
    panel.items.setCurrentItem(first)
    monkeypatch.setattr(gui.QApplication, "applicationState", lambda: Qt.ApplicationActive)
    count = len(provider.requests)
    widget.check_account_content()
    wait_for(app, lambda: not widget.busy)
    assert len(provider.requests) == count + 1
    assert provider.requests[-1].url.path.endswith("/update")
    assert panel.items.currentItem() is first and "暂无新动态" in panel.hint.text()
    provider.content_state["dynamic"].insert(0, dynamic(99))
    provider.content_state["new"] = 1
    widget.check_account_content()
    wait_for(app, lambda: not widget.busy)
    assert panel.items.topLevelItem(0).data(0, Qt.UserRole).key == "99"
    assert panel.items.currentItem().data(0, Qt.UserRole).key == "21"
    assert "合成动态 21" in panel.body.toPlainText()
    count = len(provider.requests)
    widget.set_busy(True)
    widget.check_account_content()
    widget.set_busy(False)
    with monkeypatch.context() as context:
        context.setattr(widget, "isMinimized", lambda: True)
        widget.check_account_content()
    panel.auto.setChecked(False)
    widget.check_account_content()
    panel.auto.setChecked(True)
    widget.content_toggle.click()
    widget.check_account_content()
    assert len(provider.requests) == count
    widget.content_toggle.click()
    monkeypatch.setattr(gui.QApplication, "applicationState", lambda: Qt.ApplicationInactive)
    widget.check_account_content()
    assert len(provider.requests) == count
    panel.tabs.setCurrentIndex(1)
    wait_for(app, lambda: not widget.busy)
    count = len(provider.requests)
    monkeypatch.setattr(gui.QApplication, "applicationState", lambda: Qt.ApplicationActive)
    widget.check_account_content()
    assert len(provider.requests) == count  # Followings and history never auto-poll.
    panel.tabs.setCurrentIndex(0)
    provider.content_state["risk"] = 429
    widget.check_account_content()
    wait_for(app, lambda: not widget.busy)
    assert not panel.auto.isChecked() and "保留" in panel.hint.text()
    assert panel.items.topLevelItemCount() == 20 and widget.engine.api.gate.stopped
    count = len(provider.requests)
    widget.check_account_content()
    panel.refresh.click()
    wait_for(app, lambda: not widget.busy)
    assert len(provider.requests) == count


@pytest.mark.parametrize("window", [content_provider], indirect=True)
def test_account_content_visible_images_reuse_shared_private_free_cache(app, window, monkeypatch):
    widget, provider = window
    monkeypatch.setattr(gui.QApplication, "applicationState", lambda: Qt.ApplicationActive)
    face = "https://i0.hdslb.com/bfs/face/content.png"
    for row in provider.content_state["dynamic"]:
        row["modules"]["module_author"]["face"] = face
    for row in provider.content_state["followings"]:
        row["face"] = face

    def image(request):
        assert "cookie" not in request.headers
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    provider.image_handler = image
    widget.launch(lambda api: api.use_cookie("SESSDATA=synthetic; DedeUserID=42"))
    wait_for(app, lambda: not widget.busy and bool(widget._library_view.items))
    widget.content_toggle.click()
    wait_for(app, lambda: face in widget._artwork and widget._artwork_pending is None)
    assert len(provider.images) == 1
    widget.content_panel.tabs.setCurrentIndex(1)
    wait_for(app, lambda: not widget.busy)
    widget.load_visible_artwork()
    assert len(provider.images) == 1
    assert widget.engine.api.gate.count == len(provider.requests) + len(provider.images)


@pytest.mark.parametrize("window", [content_provider], indirect=True)
def test_restart_restores_ui_then_broadcast_catches_up_and_forget_scrubs_cache(app, window):
    widget, provider = window
    root = gui.local_store.data_root()
    provider.cache_root = root
    assert widget.engine.ready.wait(5)
    # The worker may have been constructed before these test-boundary settings.
    widget.engine.api.cache_root = root
    open_test_inbox(widget, app)
    gui.save_session(replace(widget._session, user_agent="Synthetic Browser"))
    widget.content_toggle.click()
    wait_for(app, lambda: not widget.busy)
    widget.content_panel.tabs.setCurrentIndex(2)
    wait_for(app, lambda: not widget.busy)
    assert widget.engine.shutdown()
    widget.close()

    state = provider.inbox_state
    state["messages"].extend(private_message(i) for i in range(26, 71))
    state["lanes"][1][0].update(last_msg=private_message(70), max_seqno=70, unread_count=4)
    peer = BroadcastPeer()
    provider.broadcast_factory = partial(BroadcastClient, connect_factory=peer.connect)
    second = gui.MainWindow(api_factory=provider.api)
    second.show()
    try:
        before = len(provider.requests)
        second.account_dialog.restore.click()
        wait_for(app, lambda: second.inbox_panel.thread is not None)
        assert second.inbox_panel.selected() == 7
        assert "合成私信 25" in history_text(second.inbox_panel)
        assert set(second.content_panel.views) == {"dynamic", "history"}
        assert all(view.cached for view in second.content_panel.views.values())
        wait_for(
            app, lambda: second.inbox_panel.thread.messages[-1].msg_seqno == 70 and not second.busy
        )
        assert "合成私信 26" in history_text(second.inbox_panel)
        assert "合成私信 70" in history_text(second.inbox_panel)
        reads = [r for r in provider.requests[before:] if r.url.path.endswith("fetch_session_msgs")]
        assert len(reads) == 3 and all(r.url.params["begin_seqno"] == "25" for r in reads)
        assert not any(
            "/feed/all" in r.url.path or "history/cursor" in r.url.path
            for r in provider.requests[before:]
        )
        second.logout()
        wait_for(app, lambda: not second.busy and second._session is None)
        assert not list(root.glob("*/cache.sqlite3")) and gui.load_session() is None
        assert not list(root.glob("accounts/*/session.json"))
        assert not second.inbox_panel.body.count()
        assert not second.content_panel.views and peer.closed
    finally:
        assert second.engine.shutdown()
        second.busy = False
        second.close()


def test_late_image_from_same_uid_previous_login_cannot_refill_current_cache(app, window):
    widget, _ = window
    old = Session(42, "old", "synthetic")
    current = Session(42, "current", "synthetic")
    url = "https://i0.hdslb.com/bfs/face/same.png"
    widget.show_session(old)
    widget.show_session(current)
    widget._artwork_pending = (current, url)
    widget.show_artwork(old, url, PNG, "")
    assert not widget._artwork and widget._artwork_pending == (current, url)
    widget.show_artwork(current, url, PNG, "")
    assert url in widget._artwork and widget._artwork_pending is None


def test_image_memory_cache_uses_bytes_not_entry_count_and_resets_on_logout(
    app, window, monkeypatch
):
    widget, provider = window
    widget.show_session(Session(42, "Synthetic", "synthetic"))
    pixmap = QPixmap(2, 2)
    pixmap.fill(Qt.blue)
    size = pixmap.toImage().sizeInBytes()
    for index in range(200):
        widget._cache_artwork(f"image-{index}", pixmap)
    assert len(widget._artwork) == 200
    assert widget._artwork_bytes == 200 * size
    monkeypatch.setattr(gui, "ARTWORK_BYTES", 2 * size)
    widget._cache_artwork("image-200", pixmap)
    assert set(widget._artwork) == {"image-199", "image-200"}
    widget._cache_artwork("image-200", pixmap)
    assert widget._artwork_bytes == 2 * size
    widget.show_session(None)
    assert not widget._artwork and widget._artwork_bytes == 0
    assert not provider.requests


def test_cover_and_avatar_shapes_stay_consistent_after_load_and_rerender(app, window):
    widget, _ = window
    url = "https://i0.hdslb.com/shared-cover-avatar.png"
    pixmap = QPixmap(160, 90)
    pixmap.fill(Qt.blue)
    cover = ContentItem("video", "视频", "视频信息", "视频正文", image=url, image_role="cover")
    avatar = ContentItem("forward", "转发", "转发信息", "转发正文", image=url, image_role="avatar")
    panel = widget.content_panel
    view = ContentView("dynamic", (cover, avatar), False, "")
    panel.show_view(view)
    widget._cache_artwork(url, pixmap)

    def shapes():
        cover_size = panel.items.topLevelItem(0).icon(0).pixmap(QSize(48, 40)).size()
        avatar_size = panel.items.topLevelItem(1).icon(0).pixmap(QSize(48, 40)).size()
        assert cover_size.width() > cover_size.height()
        assert avatar_size.width() == avatar_size.height()

    panel.update_artwork(url)
    shapes()
    panel.render()
    shapes()
    panel.show_view(ContentView("dynamic", tuple([cover, avatar]), False, "刷新"))
    shapes()


def emoji_inbox_provider():
    provider, state = inbox_provider()
    url = "https://i0.hdslb.com/bfs/emote/synthetic.png"
    state["messages"][-1]["content"] = '{"content":"<b>普通文字</b>[热词系列测试]"}'
    state["emojis"] = [{"text": "[热词系列测试]", "url": url, "size": 2}]
    state["messages"][-2].update(msg_type=10, content='{"title":"通知标题","text":"通知正文"}')
    state["messages"][-3].update(msg_type=18, content='{"content":"[{\\"text\\":\\"系统提示\\"}]"}')
    provider.image_handler = lambda request: httpx.Response(
        200, content=PNG, headers={"content-type": "image/png"}
    )
    return provider


@pytest.mark.parametrize("window", [emoji_inbox_provider], indirect=True)
def test_message_emoji_http_to_qt_renders_safe_image_and_retains_copyable_text(app, window):
    widget, provider = window
    open_test_inbox(widget, app)
    url = provider.inbox_state["emojis"][0]["url"]
    wait_for(app, lambda: url in widget._artwork and widget._artwork_pending is None)
    panel = widget.inbox_panel
    assert "通知正文" in history_text(panel) and "系统提示" in history_text(panel)
    item = panel.body.item(panel.body.count() - 1)
    document = panel.message_delegate.document(panel.body.indexFromItem(item), 300)
    assert "bili-image:" in document.toHtml()
    assert "<b>普通文字</b>" in document.toPlainText()
    assert "<b>普通文字</b>[热词系列测试]" in item.text()
    assert len(provider.images) == 1 and "cookie" not in provider.images[0].headers
    assert widget.engine.api.gate.count == len(provider.requests) + len(provider.images)
    # Exercise the actual delegate paint, not just its generated document: the
    # full 72px image must remain visible inside the row at the bottom of the list.
    pixmap = QPixmap(72, 72)
    pixmap.fill(Qt.magenta)
    widget._cache_artwork(url, pixmap)
    panel.update_artwork(url)
    panel.body.scrollToBottom()
    app.processEvents()
    image = panel.body.viewport().grab().toImage()
    colored = sum(
        image.pixelColor(x, y).name() == "#ff00ff"
        for y in range(image.height())
        for x in range(image.width())
    )
    assert colored >= 70 * 70
    provider.inbox_state["messages"][-1]["msg_status"] = 1
    widget.load_thread(7, True)
    wait_for(app, lambda: not widget.busy)
    item = panel.body.item(panel.body.count() - 1)
    assert item.data(CONTENT_ROLE).text == "[消息已撤回]"
    assert (
        "bili-image:"
        not in panel.message_delegate.document(panel.body.indexFromItem(item), 300).toHtml()
    )
    assert len(provider.images) == 1


@pytest.mark.parametrize("window", [emoji_inbox_provider], indirect=True)
def test_messages_keep_contrast_with_a_dark_system_palette_and_selection(app, window):
    widget, _ = window
    original = app.palette()
    dark = QPalette(original)
    dark.setColor(QPalette.Text, Qt.white)
    dark.setColor(QPalette.Base, Qt.black)
    app.setPalette(dark)
    try:
        open_test_inbox(widget, app)
        panel = widget.inbox_panel
        item = panel.body.item(panel.body.count() - 1)
        index = panel.body.indexFromItem(item)
        document = panel.message_delegate.document(index, 300)
        assert document.find("普通文字").charFormat().foreground().color().name() == COLORS["ink"]
        selected = panel.message_delegate.document(index, 300, selected=True)
        assert selected.find("普通文字").charFormat().foreground().color().name() == COLORS["white"]
        panel.body.clearSelection()
        panel.body.scrollToBottom()
        app.processEvents()
        image = panel.body.viewport().grab().toImage().copy(panel.body.visualItemRect(item))
        visible_ink = sum(
            image.pixelColor(x, y).name() == COLORS["ink"]
            for y in range(image.height())
            for x in range(image.width())
        )
        assert visible_ink > 20
    finally:
        app.setPalette(original)
