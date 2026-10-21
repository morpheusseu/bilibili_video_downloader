"""PyQt5 UI; one persistent asyncio worker owns the bpi-py session and request gate."""

import asyncio
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from PyQt5.QtCore import QSettings, Qt, QThread, QUrl, pyqtSignal
from PyQt5.QtGui import QDesktopServices, QFontDatabase, QIcon
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
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
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .api import Bilibili, safe_error
from .auth import Session, forget_session, load_session, save_session
from .browser_login import chrome_login
from .download import Progress, download_source
from .sources import DownloadError, parse_source
from .theme import STYLE


class Engine(QThread):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    cancelled = pyqtSignal()
    notice = pyqtSignal(str)
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
            self.cancelled.emit()
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
        self.setWindowTitle("Bilibili 下载器")
        self.setWindowIcon(QIcon(str(Path(__file__).with_name("icon.png"))))
        self.resize(960, 900)
        self.setMinimumSize(760, 650)
        font = QFontDatabase.systemFont(QFontDatabase.GeneralFont)
        font.setPixelSize(14)
        self.setFont(font)
        self.setStyleSheet(STYLE)
        self.settings = QSettings("bilibili-video-downloader", "desktop")
        self.engine = Engine(self, api_factory)
        self.engine.result.connect(self.show_result)
        self.engine.error.connect(self.show_error)
        self.engine.cancelled.connect(self.show_cancelled)
        self.engine.notice.connect(self.show_notice)
        self.engine.progress.connect(self.show_progress)
        self.engine.session_changed.connect(self.show_session)
        self.engine.idle.connect(self.on_idle)
        self.engine.start()
        self.busy = False
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QWidget()
        header.setObjectName("header")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(32, 16, 32, 16)
        header_layout.addWidget(self.label("Bilibili 下载器", "brand"))
        header_layout.addStretch()
        self.identity = self.label("未登录 · 公开内容无需登录", "hint")
        self.identity.setWordWrap(False)
        header_layout.addWidget(self.identity)
        self.account_toggle = QPushButton("账号与登录")
        self.account_toggle.setCheckable(True)
        header_layout.addWidget(self.account_toggle)
        layout.addWidget(header)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QScrollArea.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(32, 24, 32, 24)
        body_layout.setSpacing(20)
        intro = QVBoxLayout()
        intro.setSpacing(8)
        intro.addWidget(self.label("喜欢的内容，留在身边。", "hero"))
        intro.addWidget(self.label("视频或音频，按你的方式保存。", "subtitle"))
        body_layout.addLayout(intro)
        self.account_panel, account_layout = self.section("账号与登录", "可选")
        self.account_identity = self.label("需要账号权限时，再在 Chrome 中登录。", "body")
        account_layout.addWidget(self.account_identity)
        auth_row = QHBoxLayout()
        self.login = QPushButton("在 Chrome 中登录")
        self.login.setObjectName("secondary")
        self.restore = QPushButton("读取钥匙串登录")
        self.forget = QPushButton("退出并忘记")
        self.remember = QCheckBox("记住登录（系统钥匙串）")
        for widget in (self.login, self.restore, self.forget):
            auth_row.addWidget(widget)
        auth_row.addStretch()
        account_layout.addLayout(auth_row)
        account_layout.addWidget(self.remember)
        account_layout.addWidget(
            self.label(
                "使用独立 Chrome 临时会话，不读取现有浏览器配置。"
                "只有勾选后才保存凭证；启动时不会自动读取。",
                "hint",
            )
        )
        self.advanced = QCheckBox("手动使用 Cookie")
        account_layout.addWidget(self.advanced)
        advanced_panel = QWidget()
        cookie_row = QHBoxLayout()
        cookie_row.setContentsMargins(0, 0, 0, 0)
        self.cookie = QLineEdit()
        self.cookie.setPlaceholderText("粘贴 Cookie · 隐藏显示，不写入配置文件")
        self.cookie.setAccessibleName("已有 Cookie")
        self.cookie.setEchoMode(QLineEdit.Password)
        self.import_cookie = QPushButton("使用 Cookie")
        cookie_row.addWidget(self.cookie)
        cookie_row.addWidget(self.import_cookie)
        advanced_panel.setLayout(cookie_row)
        advanced_panel.hide()
        self.advanced.toggled.connect(advanced_panel.setVisible)
        account_layout.addWidget(advanced_panel)
        self.account_panel.hide()
        self.account_toggle.toggled.connect(self.toggle_account)
        body_layout.addWidget(self.account_panel)

        source_box, source_layout = self.section("内容来源", "01")
        form = QFormLayout()
        self.source_form = form
        form.setVerticalSpacing(12)
        form.setHorizontalSpacing(24)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.mode = QComboBox()
        self.mode.setAccessibleName("来源类型")
        self.mode.addItem("视频 / 番剧 / 收藏夹链接", "auto")
        self.mode.addItem("收藏夹 ID", "favorite")
        self.mode.addItem("UID + 收藏夹名称正则", "user")
        self.source = QLineEdit()
        self.source.setObjectName("sourceInput")
        self.source.setClearButtonEnabled(True)
        self.source.setAccessibleName("内容链接或 ID")
        self.pattern = QLineEdit()
        self.pattern.setAccessibleName("收藏夹名称正则")
        self.pattern.setPlaceholderText("例如 piano.*；留空匹配该 UID 的所有收藏夹")
        self.pattern.setEnabled(False)
        form.addRow(self.label("来源类型"), self.mode)
        form.addRow(self.label("链接或 ID"), self.source)
        form.addRow(self.label("收藏夹名称"), self.pattern)
        parts_row = QHBoxLayout()
        self.page = QSpinBox()
        self.page.setAccessibleName("指定分 P")
        self.page.setRange(1, 100000)
        self.page.setFixedWidth(96)
        self.all_parts = QCheckBox("全部分 P")
        parts_row.addWidget(self.page)
        parts_row.addSpacing(12)
        parts_row.addWidget(self.all_parts)
        parts_row.addStretch()
        form.addRow(self.label("分 P"), parts_row)
        source_layout.addLayout(form)
        source_layout.addWidget(
            self.label("链接中的 ?p=2 可指定分 P；选择“全部”时以全部为准。", "hint")
        )
        self.preview_hint = self.label("先检查来源，可预览前 10 个分 P，不下载媒体。", "hint")
        source_layout.addWidget(self.preview_hint)
        self.preview_items = QTreeWidget()
        self.preview_items.setHeaderLabels(["视频 / 番剧", "分 P", "标识"])
        self.preview_items.setRootIsDecorated(False)
        self.preview_items.setAlternatingRowColors(False)
        self.preview_items.setIndentation(0)
        self.preview_items.setMinimumHeight(120)
        self.preview_items.setMaximumHeight(170)
        self.preview_items.setAccessibleName("来源预览")
        self.preview_items.hide()
        source_layout.addWidget(self.preview_items)
        body_layout.addWidget(source_box)

        output_box, output_layout = self.section("保存设置", "02")
        output_form = QFormLayout()
        self.output_form = output_form
        output_form.setVerticalSpacing(12)
        output_form.setHorizontalSpacing(24)
        output_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        output_form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.format = QComboBox()
        self.format.addItems(["mp4", "mp3", "wav"])
        self.format.setAccessibleName("输出格式")
        self.quality = QComboBox()
        for label, value in (("720P", 64), ("1080P", 80), ("480P", 32), ("360P", 16)):
            self.quality.addItem(label, value)
        self.quality.setAccessibleName("请求画质")
        media_row = QHBoxLayout()
        media_row.addWidget(self.format, 1)
        media_row.addSpacing(12)
        self.quality_label = self.label("画质")
        media_row.addWidget(self.quality_label)
        media_row.addWidget(self.quality, 1)
        output_form.addRow(self.label("输出格式"), media_row)
        destination_row = QHBoxLayout()
        self.destination = QLineEdit(
            self.settings.value("output", str(Path.home() / "Downloads" / "bilibili"))
        )
        self.destination.setAccessibleName("保存位置")
        self.choose = QPushButton("更改…")
        destination_row.addWidget(self.destination)
        destination_row.addWidget(self.choose)
        output_form.addRow(self.label("保存位置"), destination_row)
        self.ffmpeg = QLineEdit(self.settings.value("ffmpeg", ""))
        self.ffmpeg.setAccessibleName("FFmpeg 路径")
        self.ffmpeg.setPlaceholderText("留空：优先使用系统 FFmpeg，否则使用依赖内自带版本")
        self.ffmpeg_advanced = QCheckBox("指定 FFmpeg 路径")
        output_form.addRow(self.ffmpeg_advanced)
        output_form.addRow(self.label("FFmpeg"), self.ffmpeg)
        self.ffmpeg.hide()
        output_form.labelForField(self.ffmpeg).hide()
        self.ffmpeg_advanced.toggled.connect(self.toggle_ffmpeg)
        output_layout.addLayout(output_form)
        output_layout.addWidget(
            self.label("已有文件自动跳过，不会覆盖。未完成的临时文件会在取消后清理。", "hint")
        )
        body_layout.addWidget(output_box)
        body_layout.addWidget(
            self.label("仅保存你有权下载的内容，不绕过付费、DRM 或地区限制。", "hint")
        )
        body_layout.addStretch()
        self.scroll.setWidget(body)
        layout.addWidget(self.scroll, 1)

        task = QFrame()
        task.setObjectName("taskPanel")
        task_layout = QVBoxLayout(task)
        task_layout.setContentsMargins(32, 16, 32, 16)
        task_layout.setSpacing(10)
        status_row = QHBoxLayout()
        self.task_title = self.label("准备就绪", "taskTitle")
        status_row.addWidget(self.task_title)
        status_row.addStretch()
        self.open_output = QPushButton("打开文件夹")
        self.open_output.setObjectName("link")
        self.details = QPushButton("操作详情")
        self.details.setObjectName("link")
        self.details.setCheckable(True)
        status_row.addWidget(self.open_output)
        status_row.addWidget(self.details)
        task_layout.addLayout(status_row)
        self.status = self.label("粘贴内容链接，检查来源或直接开始下载。", "hint")
        task_layout.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        task_layout.addWidget(self.bar)
        actions = QHBoxLayout()
        actions.setSpacing(12)
        pacing = self.label("请求间隔 0.5–1.5 秒 · 无后台请求", "finePrint")
        pacing.setWordWrap(False)
        actions.addWidget(pacing)
        actions.addStretch()
        self.preview = QPushButton("检查来源")
        self.preview.setObjectName("secondary")
        self.cancel = QPushButton("取消任务")
        self.cancel.setEnabled(False)
        self.start_download = QPushButton("开始下载")
        self.start_download.setObjectName("primary")
        actions.addWidget(self.preview)
        actions.addWidget(self.cancel)
        actions.addWidget(self.start_download)
        task_layout.addLayout(actions)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(500)
        self.log.setFixedHeight(100)
        self.log.setPlaceholderText("操作详情会显示在这里；不记录 Cookie 或带签名的下载地址。")
        self.log.hide()
        self.details.toggled.connect(self.log.setVisible)
        task_layout.addWidget(self.log)
        layout.addWidget(task)
        self.setCentralWidget(root)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.all_parts.toggled.connect(self.options_changed)
        self.format.currentIndexChanged.connect(self.options_changed)
        for widget in (self.source, self.pattern):
            widget.textChanged.connect(self.clear_preview)
        self.page.valueChanged.connect(self.clear_preview)
        self.all_parts.toggled.connect(self.clear_preview)
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

    @staticmethod
    def label(text, role="body"):
        widget = QLabel(text)
        widget.setObjectName(role)
        widget.setTextFormat(Qt.PlainText)
        widget.setWordWrap(True)
        return widget

    def section(self, title, number):
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        heading = QHBoxLayout()
        heading.addWidget(self.label(title, "sectionTitle"))
        heading.addStretch()
        heading.addWidget(self.label(number, "hint"))
        layout.addLayout(heading)
        return frame, layout

    def clear_preview(self):
        self.preview_items.clear()
        self.preview_items.hide()
        self.preview_hint.setText("先检查来源，可预览前 10 个分 P，不下载媒体。")

    def toggle_account(self, visible):
        self.account_panel.setVisible(visible)
        if visible:
            self.scroll.verticalScrollBar().setValue(0)

    def mode_changed(self):
        self.clear_preview()
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
        self.options_changed()

    def options_changed(self):
        self.page.setEnabled(not self.busy and not self.all_parts.isChecked())
        video = self.format.currentText() == "mp4"
        self.quality.setEnabled(not self.busy and video)
        self.quality_label.setEnabled(video)

    def toggle_ffmpeg(self, visible):
        self.ffmpeg.setVisible(visible)
        self.output_form.labelForField(self.ffmpeg).setVisible(visible)

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
        self.pattern.setEnabled(not busy and self.mode.currentData() == "user")
        self.options_changed()
        self.cancel.setEnabled(busy)

    def launch(self, operation, title="正在处理", note="请求串行执行，可随时取消。"):
        if self.busy:
            return
        self.set_busy(True)
        self.task_title.setText(title)
        self.status.setText(note)
        self.bar.setRange(0, 0)
        try:
            self.engine.submit(operation)
        except Exception as error:
            self.show_error(safe_error(error))
            self.on_idle()

    def on_idle(self):
        self.set_busy(False)
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(0)

    def show_error(self, text):
        self.task_title.setText("未能完成")
        self.status.setText(text)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.log.appendPlainText(text)

    def show_cancelled(self):
        self.task_title.setText("已取消")
        self.status.setText("任务已取消；没有发布不完整文件，已完成文件会保留。")
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.log.appendPlainText(self.status.text())

    def show_notice(self, text):
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
        self.identity.setText("已登录" if session else "未登录 · 公开内容无需登录")
        self.account_identity.setText(
            f"{session.name} · UID {session.mid}"
            if session
            else "需要账号权限时，再在 Chrome 中登录。"
        )

    def show_result(self, result):
        self.task_title.setText("已完成")
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        if isinstance(result, Session):
            self.task_title.setText("登录成功")
            self.status.setText("登录成功；没有后台心跳或自动下载")
            self.show()
            self.raise_()
            self.activateWindow()
        elif result == "logout":
            self.status.setText("已退出并忘记本应用保存的登录；未操作其他客户端。")
        elif isinstance(result, list):
            self.status.setText(
                f"任务完成：{sum(not r.skipped for r in result)} 个文件已生成，"
                f"{sum(r.skipped for r in result)} 个已存在"
            )
        elif isinstance(result, tuple):
            parts, truncated = result
            self.preview_items.clear()
            for part in parts:
                item = QTreeWidgetItem(
                    [part.title, f"P{part.number} · {part.part_title}", part.bvid]
                )
                for column in range(3):
                    item.setToolTip(column, item.text(column))
                self.preview_items.addTopLevelItem(item)
                self.log.appendPlainText(
                    f"P{part.number} · {part.title} · {part.part_title} · {part.bvid}"
                )
            self.preview_items.setVisible(bool(parts))
            self.preview_items.setColumnWidth(0, 250)
            self.preview_items.setColumnWidth(1, 210)
            self.preview_hint.setText(
                f"来源可读取：预览 {len(parts)} 个分 P；未下载媒体"
                + ("（已达到 10 条预览上限）" if truncated else "")
            )
            self.task_title.setText("来源已检查" if parts else "没有找到内容")
            self.status.setText(self.preview_hint.text())
        self.log.appendPlainText(self.status.text())

    def finish_login(self, session, remember):
        if remember:
            try:
                # Keep native credential operations on the single owner worker.
                # Cancelling an executor future would not stop its underlying write,
                # which could otherwise race a subsequent "forget login".
                save_session(session)
            except DownloadError as error:
                raise DownloadError(f"当前登录可用，但未能保存到钥匙串：{error}") from error
        return session

    def browser_login(self):
        remember = self.remember.isChecked()

        async def operation(api):
            session = await chrome_login(api, self.engine.notice.emit)
            return self.finish_login(session, remember)

        self.launch(
            operation, "等待 Chrome 登录", "请在打开的 Chrome 中登录，完成后会自动回到这里。"
        )

    def cookie_login(self):
        value, remember = self.cookie.text(), self.remember.isChecked()
        self.cookie.clear()

        async def operation(api):
            return self.finish_login(await api.use_cookie(value), remember)

        self.launch(operation, "正在验证登录")

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

        self.launch(operation, "正在读取钥匙串登录")

    def logout(self):
        async def operation(api):
            api.clear_session()
            forget_session()
            return "logout"

        self.launch(operation, "正在退出并忘记登录")

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
            ),
            "正在下载",
            "正在读取内容，随后下载并转换；已有文件会跳过。",
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

        self.launch(operation, "正在检查来源", "只读取内容信息，不下载媒体；最多预览 10 个分 P。")

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
