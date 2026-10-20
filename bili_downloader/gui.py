"""PyQt5 UI; one persistent asyncio worker owns the bpi-py session and request gate."""

import asyncio
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from PyQt5.QtCore import QSettings, Qt, QThread, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .api import Bilibili, safe_error
from .auth import Session, forget_session, load_session, save_session
from .browser_login import chrome_login
from .download import Progress, download_source
from .sources import DownloadError, parse_source


class Engine(QThread):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    idle = pyqtSignal()
    progress = pyqtSignal(object)
    session_changed = pyqtSignal(object)

    def __init__(self, parent=None, api_factory: Callable = Bilibili):
        super().__init__(parent)
        self.api_factory = api_factory
        self.ready = threading.Event()
        self.future = None
        self.active = None
        self.cancel_requested = False

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.lifetime())
        finally:
            self.loop.close()

    async def lifetime(self):
        self.stop = asyncio.Event()
        async with self.api_factory() as self.api:
            self.ready.set()
            await self.stop.wait()
            if self.active:
                self.active.cancel()
                await asyncio.gather(self.active, return_exceptions=True)

    def submit(self, operation):
        if not self.ready.wait(5):
            raise DownloadError("后台任务尚未就绪")
        self.cancel_requested = False
        self.future = asyncio.run_coroutine_threadsafe(self.perform(operation), self.loop)

    async def perform(self, operation):
        self.active = asyncio.current_task()
        try:
            if self.cancel_requested:
                raise asyncio.CancelledError
            self.result.emit(await operation(self.api))
        except asyncio.CancelledError:
            self.error.emit("任务已取消；没有发布不完整文件")
        except Exception as error:
            self.error.emit(safe_error(error))
        finally:
            self.active = None
            self.session_changed.emit(self.api.session)
            self.idle.emit()

    def cancel(self):
        if self.ready.is_set():
            self.loop.call_soon_threadsafe(self._cancel)

    def _cancel(self):
        self.cancel_requested = True
        if self.active:
            self.active.cancel()

    def shutdown(self):
        if not self.isRunning():
            return True
        if self.ready.is_set():
            if not self.loop.is_closed():
                self.loop.call_soon_threadsafe(self.stop.set)
        return self.wait(7000)


class MainWindow(QMainWindow):
    def __init__(self, *, api_factory: Callable = Bilibili):
        super().__init__()
        self.setWindowTitle("Bilibili 视频 / 音频下载器 · bpi-py")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("icon.png"))))
        self.resize(940, 880)
        self.setMinimumSize(800, 650)
        self.setStyleSheet("""
            QMainWindow, QScrollArea { background: #f4f6f9; }
            QWidget { color: #243047; font-size: 13px; }
            QGroupBox { background: #ffffff; border: 1px solid #dde3eb; border-radius: 12px;
                margin-top: 14px; padding: 20px 16px 14px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 18px; padding: 0 6px; }
            QLineEdit, QComboBox, QSpinBox { background: #fff; border: 1px solid #cbd4e0;
                border-radius: 6px; padding: 7px; min-height: 18px; }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #e45b8b; }
            QPushButton { background: #fff; border: 1px solid #cbd4e0; border-radius: 7px;
                padding: 9px 14px; }
            QPushButton:hover { background: #f0f3f8; }
            QPushButton:disabled { color: #8b96a6; background: #f0f2f6; }
            QPushButton#primary { background: #d94e80; border: none; color: white; }
            QPushButton#primary:hover { background: #c24270; }
            QPushButton#primary:disabled { background: #dbadbe; }
            QPlainTextEdit { background: #fff; border: 1px solid #dde3eb; border-radius: 8px; }
            QProgressBar { border: none; background: #e4e9f0; border-radius: 4px; height: 8px; }
            QProgressBar::chunk { background: #d94e80; border-radius: 4px; }
            QLabel#hint { color: #64748b; font-size: 12px; }
        """)
        self.settings = QSettings("bilibili-video-downloader", "desktop")
        self.engine = Engine(self, api_factory)
        self.engine.result.connect(self.show_result)
        self.engine.error.connect(self.show_error)
        self.engine.progress.connect(self.show_progress)
        self.engine.session_changed.connect(self.show_session)
        self.engine.idle.connect(self.on_idle)
        self.engine.start()
        self.busy = False
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 18, 24, 18)
        heading = QLabel("Bilibili 下载器")
        heading.setStyleSheet("font-size: 25px; font-weight: 600;")
        layout.addWidget(heading)
        subtitle = QLabel("保存你有权下载的内容。公开内容无需登录；不会绕过付费、DRM 或地区限制。")
        subtitle.setObjectName("hint")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 8, 4)
        account = QGroupBox("1 · 账号（可选）")
        account_layout = QVBoxLayout(account)
        self.identity = QLabel("未登录：可下载公开内容；扫码用于账号可访问的内容")
        self.identity.setTextFormat(Qt.PlainText)
        account_layout.addWidget(self.identity)
        auth_row = QHBoxLayout()
        self.login = QPushButton("在 Chrome 中登录")
        self.restore = QPushButton("使用系统保存的登录")
        self.forget = QPushButton("退出并忘记登录")
        self.remember = QCheckBox("记住登录（系统钥匙串）")
        for widget in (self.login, self.restore, self.forget, self.remember):
            auth_row.addWidget(widget)
        account_layout.addLayout(auth_row)
        privacy = QLabel(
            "独立 Chrome 临时会话，不读取你的已有浏览器配置。只有勾选后才写入系统钥匙串。"
        )
        privacy.setObjectName("hint")
        privacy.setWordWrap(True)
        account_layout.addWidget(privacy)
        self.advanced = QCheckBox("高级：使用已有 Cookie")
        account_layout.addWidget(self.advanced)
        advanced_panel = QWidget()
        cookie_row = QHBoxLayout()
        cookie_row.setContentsMargins(0, 0, 0, 0)
        self.cookie = QLineEdit()
        self.cookie.setPlaceholderText("可选：手动粘贴 Cookie（不显示、不写入配置文件）")
        self.cookie.setEchoMode(QLineEdit.Password)
        self.import_cookie = QPushButton("使用 Cookie")
        cookie_row.addWidget(self.cookie)
        cookie_row.addWidget(self.import_cookie)
        advanced_panel.setLayout(cookie_row)
        advanced_panel.hide()
        self.advanced.toggled.connect(advanced_panel.setVisible)
        account_layout.addWidget(advanced_panel)
        body_layout.addWidget(account)
        source_box = QGroupBox("2 · 选择内容与输出")
        form = QFormLayout()
        self.source_form = form
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.mode = QComboBox()
        self.mode.addItem("视频 / 番剧 / 收藏夹链接", "auto")
        self.mode.addItem("收藏夹 ID", "favorite")
        self.mode.addItem("UID + 收藏夹名称正则", "user")
        self.source = QLineEdit()
        self.source.setPlaceholderText("BV178411i7AD 或完整 bilibili.com 链接")
        self.pattern = QLineEdit()
        self.pattern.setPlaceholderText("例如 piano.*；留空匹配该 UID 的所有收藏夹")
        self.pattern.setEnabled(False)
        form.addRow("来源类型", self.mode)
        form.addRow("来源", self.source)
        form.addRow("收藏夹名称", self.pattern)
        parts_row = QHBoxLayout()
        self.page = QSpinBox()
        self.page.setRange(1, 100000)
        self.all_parts = QCheckBox("下载全部分 P（批量模式也适用）")
        parts_row.addWidget(self.page)
        parts_row.addWidget(self.all_parts)
        form.addRow("分 P（从 1 开始）", parts_row)
        self.format = QComboBox()
        self.format.addItems(["mp4", "mp3", "wav"])
        self.quality = QComboBox()
        for label, value in (("720P", 64), ("1080P", 80), ("480P", 32), ("360P", 16)):
            self.quality.addItem(label, value)
        form.addRow("输出格式", self.format)
        form.addRow("请求画质", self.quality)
        destination_row = QHBoxLayout()
        self.destination = QLineEdit(
            self.settings.value("output", str(Path.home() / "Downloads" / "bilibili"))
        )
        self.choose = QPushButton("选择目录")
        destination_row.addWidget(self.destination)
        destination_row.addWidget(self.choose)
        form.addRow("保存位置", destination_row)
        self.ffmpeg = QLineEdit(self.settings.value("ffmpeg", ""))
        self.ffmpeg.setPlaceholderText("留空：优先使用系统 FFmpeg，否则使用依赖内自带版本")
        self.ffmpeg_advanced = QCheckBox("高级：指定 FFmpeg")
        form.addRow(self.ffmpeg_advanced)
        form.addRow("FFmpeg 路径", self.ffmpeg)
        self.ffmpeg.hide()
        form.labelForField(self.ffmpeg).hide()
        self.ffmpeg_advanced.toggled.connect(self.toggle_ffmpeg)
        hint = QLabel(
            "视频链接里的 ?p=2 会指定第 2 P；勾选全部分 P 时以全部为准。"
            "已有文件默认跳过，绝不覆盖。"
        )
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        form.addRow(hint)
        source_box.setLayout(form)
        body_layout.addWidget(source_box)
        actions = QHBoxLayout()
        self.start_download = QPushButton("开始下载")
        self.start_download.setObjectName("primary")
        self.preview = QPushButton("检查来源（不下载）")
        self.cancel = QPushButton("取消当前任务")
        self.cancel.setEnabled(False)
        actions.addWidget(self.preview)
        actions.addWidget(self.start_download)
        actions.addWidget(self.cancel)
        body_layout.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)
        layout.addLayout(actions)  # Primary actions remain visible when the form scrolls.
        self.status = QLabel("就绪 · 请求串行，随机间隔 0.5～1.5 秒；无后台心跳或自动重试")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(500)
        self.log.setMaximumHeight(100)
        self.log.setPlaceholderText("操作详情会显示在这里；不记录 Cookie 或带签名的下载地址。")
        layout.addWidget(self.status)
        layout.addWidget(self.bar)
        layout.addWidget(self.log)
        self.open_output = QPushButton("打开输出目录")
        layout.addWidget(self.open_output, alignment=Qt.AlignRight)
        self.setCentralWidget(root)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.choose.clicked.connect(self.choose_directory)
        self.start_download.clicked.connect(self.download)
        self.cancel.clicked.connect(self.engine.cancel)
        self.login.clicked.connect(self.browser_login)
        self.restore.clicked.connect(self.restore_login)
        self.import_cookie.clicked.connect(self.cookie_login)
        self.forget.clicked.connect(self.logout)
        self.preview.clicked.connect(self.preview_source)
        self.open_output.clicked.connect(self.open_directory)
        self.mode_changed()

    def mode_changed(self):
        user_mode = self.mode.currentData() == "user"
        self.pattern.setEnabled(not self.busy and user_mode)
        self.pattern.setVisible(user_mode)
        self.source_form.labelForField(self.pattern).setVisible(user_mode)
        self.source.setPlaceholderText(
            {
                "auto": "BV178411i7AD、ep ID 或完整 bilibili.com 链接",
                "favorite": "收藏夹数字 ID 或包含 fid= 的收藏夹链接",
                "user": "UP 主的数字 UID，例如 398421039",
            }[self.mode.currentData()]
        )

    def toggle_ffmpeg(self, visible):
        self.ffmpeg.setVisible(visible)
        self.source_form.labelForField(self.ffmpeg).setVisible(visible)

    def set_busy(self, busy):
        self.busy = busy
        for widget in (
            self.login,
            self.restore,
            self.forget,
            self.remember,
            self.cookie,
            self.import_cookie,
            self.mode,
            self.source,
            self.pattern,
            self.page,
            self.all_parts,
            self.format,
            self.quality,
            self.destination,
            self.choose,
            self.ffmpeg,
            self.start_download,
            self.preview,
            self.advanced,
            self.ffmpeg_advanced,
        ):
            widget.setEnabled(not busy)
        self.mode_changed()
        self.cancel.setEnabled(busy)

    def launch(self, operation):
        if self.busy:
            return
        self.set_busy(True)
        self.bar.setRange(0, 0)
        try:
            self.engine.submit(operation)
        except Exception as error:
            self.show_error(safe_error(error))
            self.on_idle()

    def on_idle(self):
        self.set_busy(False)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)

    def show_error(self, text):
        self.status.setText(text)
        self.log.appendPlainText(text)

    def show_progress(self, event: Progress):
        self.status.setText(event.message)
        if event.phase == "bytes":
            megabytes = event.completed / 1024 / 1024
            self.status.setText(
                f"下载中 · {megabytes:.1f} MiB"
                + (f" / {event.total / 1024 / 1024:.1f} MiB" if event.total else "")
            )
            self.bar.setRange(0, 100 if event.total else 0)
            if event.total:
                self.bar.setValue(min(100, int(100 * event.completed / event.total)))
        else:
            self.bar.setRange(0, 0)
            self.log.appendPlainText(event.message)

    def show_session(self, session):
        self.identity.setText(
            f"已登录：{session.name}（UID {session.mid}）"
            if session
            else "未登录：可以下载公开内容；需要账号权限时再登录"
        )

    def show_result(self, result):
        if isinstance(result, Session):
            self.status.setText("登录成功；没有后台心跳或自动下载")
            self.show()
            self.raise_()
            self.activateWindow()
        elif result == "logout":
            self.identity.setText("已退出并忘记本应用保存的登录；未操作其他客户端")
        elif isinstance(result, list):
            self.status.setText(
                f"任务完成：{sum(not r.skipped for r in result)} 个文件已生成，"
                f"{sum(r.skipped for r in result)} 个已存在"
            )
        elif isinstance(result, tuple):
            parts, truncated = result
            for part in parts:
                self.log.appendPlainText(
                    f"P{part.number} · {part.title} · {part.part_title} · {part.bvid}"
                )
            self.status.setText(
                f"来源可读取：预览 {len(parts)} 个分 P；未下载媒体"
                + ("（已达到 10 条预览上限）" if truncated else "")
            )

    def finish_login(self, session, remember):
        if remember:
            try:
                # Keep native credential operations on the single owner worker.
                # Cancelling an executor future would not stop its underlying write,
                # which could otherwise race a subsequent "forget login".
                save_session(session)
            except DownloadError as error:
                self.engine.error.emit(str(error))
        return session

    def browser_login(self):
        remember = self.remember.isChecked()

        async def operation(api):
            session = await chrome_login(api, self.engine.error.emit)
            return self.finish_login(session, remember)

        self.launch(operation)

    def cookie_login(self):
        value, remember = self.cookie.text(), self.remember.isChecked()
        self.cookie.clear()

        async def operation(api):
            return self.finish_login(await api.use_cookie(value), remember)

        self.launch(operation)

    def restore_login(self):
        async def operation(api):
            session = load_session()
            if session is None:
                raise DownloadError("系统钥匙串中没有保存的登录，请扫码")
            restored = await api.use_cookie(session.cookie, user_agent=session.user_agent)
            if restored.mid != session.mid:
                api.clear_session()
                raise DownloadError("保存的登录与当前账号不一致，已清除本次内存登录")
            return restored

        self.launch(operation)

    def logout(self):
        async def operation(api):
            api.clear_session()
            forget_session()
            return "logout"

        self.launch(operation)

    def choose_directory(self):
        value = QFileDialog.getExistingDirectory(self, "选择保存目录", self.destination.text())
        if value:
            self.destination.setText(value)

    def download(self):
        try:
            source = parse_source(self.source.text(), self.mode.currentData(), self.pattern.text())
            if not self.destination.text().strip():
                raise DownloadError("请选择保存目录")
            output = Path(self.destination.text()).expanduser().resolve()
        except (DownloadError, OSError, ValueError) as error:
            self.show_error(safe_error(error))
            return
        format, quality = self.format.currentText(), self.quality.currentData()
        all_parts, page, ffmpeg = self.all_parts.isChecked(), self.page.value(), self.ffmpeg.text()
        self.settings.setValue("output", str(output))
        self.settings.setValue("ffmpeg", ffmpeg)
        self.launch(
            lambda api: download_source(
                api,
                source,
                output,
                format=format,
                quality=quality,
                all_parts=all_parts,
                page=page,
                ffmpeg=ffmpeg,
                report=self.engine.progress.emit,
            )
        )

    def preview_source(self):
        try:
            source = parse_source(self.source.text(), self.mode.currentData(), self.pattern.text())
        except DownloadError as error:
            self.show_error(str(error))
            return
        all_parts, page = self.all_parts.isChecked(), self.page.value()

        async def operation(api):
            parts = []
            async for part in api.parts(source, all_parts=all_parts, page=page):
                parts.append(part)
                if len(parts) == 10:
                    return parts, True
            return parts, False

        self.launch(operation)

    def open_directory(self):
        path = Path(self.destination.text()).expanduser()
        if path.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
        else:
            self.show_error("输出目录尚不存在；开始下载时会创建")

    def closeEvent(self, event):
        if (
            self.busy
            and QMessageBox.question(
                self,
                "取消任务并退出？",
                "未完成的临时下载会被清理，已完成文件会保留。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            event.ignore()
            return
        if self.engine.shutdown():
            event.accept()
        else:
            self.status.setText("正在等待下载/转换取消，请稍后再次关闭")
            event.ignore()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Bilibili Downloader")
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
