import asyncio
import time

import pytest
from conftest import BV, Provider
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
def window(app, monkeypatch):
    class Settings:
        def value(self, key, default):
            return default

        def setValue(self, *args):
            pass

    monkeypatch.setattr(gui, "QSettings", lambda *args: Settings())
    provider = Provider(pages=2)
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
        options["report"](Progress("convert", "合成转换"))
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
    assert widget.identity.text() == "已登录"
    assert widget.account_identity.text() == "Synthetic user · UID 42"
    assert not provider.requests
