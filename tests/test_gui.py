import asyncio
import threading
import time

import httpx
import pytest
from conftest import BV, PNG, Provider, library_provider
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

from bili_downloader import gui
from bili_downloader.auth import Session
from bili_downloader.download import Progress, Result
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
    assert widget.account_panel.isVisible() and not provider.requests


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
    widget._download_job = True
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

    widget.launch(denied, library=True)
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
    assert widget.account_identity.text() == "Synthetic user · UID 42"
    assert not provider.requests
