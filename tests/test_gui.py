import asyncio
import time

import pytest
from conftest import BV, Provider
from PyQt5.QtWidgets import QApplication

from bili_downloader import gui
from bili_downloader.sources import DownloadError


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
