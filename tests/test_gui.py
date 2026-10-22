import asyncio
import threading
import time

import httpx
import pytest
from conftest import BV, PNG, Provider, inbox_provider, library_provider, private_message
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QInputMethodEvent
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QMessageBox

from bili_downloader import gui
from bili_downloader.auth import Session
from bili_downloader.download import Progress, Result
from bili_downloader.inbox_panel import ReplyEditor
from bili_downloader.sources import DownloadError, Source


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
    monkeypatch.setattr(gui, "forget_session", lambda: None)
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


def test_keychain_save_failure_stays_visible_without_losing_memory_login(app, window, monkeypatch):
    widget, provider = window
    session = Session(mid=42, name="Synthetic user", cookie="fake-cookie")

    def cannot_save(value):
        raise DownloadError("合成钥匙串不可用")

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
