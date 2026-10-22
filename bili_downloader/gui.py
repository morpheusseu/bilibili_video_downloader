"""PyQt5 UI; one persistent asyncio worker owns the bpi-py session and request gate."""

import asyncio
import sys
import threading
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from PyQt5.QtCore import (
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt5.QtGui import (
    QDesktopServices,
    QFontDatabase,
    QIcon,
    QPixmap,
)
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
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

from .account_dialog import AccountDialog
from .api import Bilibili, safe_error
from .artwork import avatar_icon, decode_image
from .auth import Session, forget_session, load_session, save_session
from .browser_login import chrome_login
from .download import Progress, download_source
from .download_button import DownloadButton
from .inbox import InboxView, MessageView, SendReceipt, validate_reply
from .inbox_panel import InboxPanel
from .library import FavoriteView
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
    artwork_ready = pyqtSignal(object, str, object, str)

    def __init__(self, parent=None, api_factory: Callable = Bilibili):
        super().__init__(parent)
        self.api_factory = api_factory
        self.ready = threading.Event()
        self.future = None
        self.active = None
        self.cancel_requested = False
        self.artwork_task = None
        self.closing = False

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.lifetime())
        finally:
            self.closing = True
            self.ready.clear()
            self.loop.close()

    async def lifetime(self):
        self.stop = asyncio.Event()
        async with self.api_factory() as self.api:
            self.ready.set()
            await self.stop.wait()
            if self.active:
                self.active.cancel()
                await asyncio.gather(self.active, return_exceptions=True)
            await self.stop_artwork()

    def submit(self, operation):
        if self.closing or not self.isRunning():
            raise DownloadError("后台已关闭，不再接受新操作")
        if not self.ready.wait(5):
            raise DownloadError("后台任务尚未就绪")
        self.cancel_requested = False
        self.future = asyncio.run_coroutine_threadsafe(self.perform(operation), self.loop)

    async def perform(self, operation):
        self.active = asyncio.current_task()
        try:
            await self.stop_artwork()
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

    async def stop_artwork(self):
        if self.artwork_task:
            self.artwork_task.cancel()
            await asyncio.gather(self.artwork_task, return_exceptions=True)

    def request_artwork(self, url):
        if not self.closing and self.ready.is_set():
            asyncio.run_coroutine_threadsafe(self.load_artwork(url), self.loop)

    async def load_artwork(self, url):
        session = self.api.session
        if session is None or self.active:
            self.artwork_ready.emit(session.mid if session else None, url, None, "")
            return
        self.artwork_task = asyncio.current_task()
        body, error = None, ""
        try:
            body = await self.api.cdn_image(url)
        except asyncio.CancelledError:
            pass  # Foreground operations preempt optional images; retry only when visible and idle.
        except Exception as caught:
            error = safe_error(caught)
        finally:
            self.artwork_task = None
            self.artwork_ready.emit(session.mid, url, body, error)

    def cancel(self):
        if self.ready.is_set():
            self.loop.call_soon_threadsafe(self._cancel)

    def _cancel(self):
        self.cancel_requested = True
        if self.active:
            self.active.cancel()

    def shutdown(self):
        self.closing = True
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
        self.engine.artwork_ready.connect(self.show_artwork)
        self.engine.start()
        self.busy = False
        self._session_mid = None
        self._pending_library = False
        self._job = None
        self._artwork = OrderedDict()
        self._artwork_pending = None
        self._artwork_paused = False
        self._library_view = FavoriteView()
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
        self.inbox_toggle = QPushButton()
        self.inbox_toggle.setObjectName("inboxMail")
        self.inbox_toggle.setIcon(QIcon(str(Path(__file__).with_name("mail.svg"))))
        self.inbox_toggle.setIconSize(QSize(26, 26))
        self.inbox_toggle.setFixedSize(44, 44)
        self.inbox_toggle.setAccessibleName("私信")
        self.inbox_toggle.setToolTip("私信（登录后可用）")
        self.inbox_toggle.setCheckable(True)
        self.inbox_toggle.setEnabled(False)
        header_layout.addWidget(self.inbox_toggle)
        self.library_toggle = QPushButton()
        self.library_toggle.setObjectName("favoritesStar")
        star_path = str(Path(__file__).with_name("star.svg"))
        star = QIcon(star_path)
        star.addFile(star_path, QSize(26, 26), QIcon.Disabled)
        self.library_toggle.setIcon(star)
        self.library_toggle.setIconSize(QSize(26, 26))
        self.library_toggle.setFixedSize(44, 44)
        self.library_toggle.setAccessibleName("我的收藏")
        self.library_toggle.setToolTip("我的收藏（登录后可用）")
        self.library_toggle.setCheckable(True)
        self.library_toggle.setEnabled(False)
        header_layout.addWidget(self.library_toggle)
        self.identity = self.label("", "identity")
        self.identity.setWordWrap(False)
        self.identity.setMaximumWidth(220)
        self.identity.hide()
        header_layout.addWidget(self.identity)
        self.account_toggle = QPushButton("登录")
        self.account_toggle.setObjectName("accountAvatar")
        self.account_toggle.setFixedSize(44, 44)
        self.account_toggle.setIconSize(QSize(36, 36))
        self.account_toggle.setAccessibleName("登录：账号与登录")
        self.account_toggle.setToolTip("登录 · 账号与登录")
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
        self.intro = QWidget()
        intro = QVBoxLayout(self.intro)
        intro.setContentsMargins(0, 0, 0, 0)
        intro.setSpacing(8)
        intro.addWidget(self.label("喜欢的内容，留在身边。", "hero"))
        intro.addWidget(self.label("视频或音频，按你的方式保存。", "subtitle"))
        body_layout.addWidget(self.intro)
        self.account_dialog = AccountDialog(self)
        self.account_toggle.toggled.connect(self.toggle_account)
        self.account_dialog.finished.connect(lambda: self.account_toggle.setChecked(False))

        self.library_panel, library_layout = self.section("我的收藏", "仅当前账号")
        folders_row = QHBoxLayout()
        self.folders = QComboBox()
        self.folders.setAccessibleName("我的收藏夹")
        self.refresh_library = QPushButton("强制刷新")
        folders_row.addWidget(self.folders, 1)
        folders_row.addWidget(self.refresh_library)
        library_layout.addLayout(folders_row)
        self.library_hint = self.label(
            "登录后读取收藏夹；按需加载内容，不会一次扫描全部收藏。", "hint"
        )
        library_layout.addWidget(self.library_hint)
        self.favorite_items = QTreeWidget()
        self.favorite_items.setHeaderLabels(["内容", "UP 主", "时长", "状态"])
        self.favorite_items.setRootIsDecorated(False)
        self.favorite_items.setMinimumHeight(150)
        self.favorite_items.setMaximumHeight(220)
        self.favorite_items.setAccessibleName("收藏夹内容")
        self.favorite_items.setIconSize(QSize(96, 54))
        self._cover_placeholder = QIcon(str(Path(__file__).with_name("cover-placeholder.svg")))
        self.favorite_items.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            self.favorite_items.header().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        library_layout.addWidget(self.favorite_items)
        library_actions = QHBoxLayout()
        self.more_favorites = QPushButton("加载更多")
        self.use_favorite = QPushButton("填入所选视频")
        self.use_folder = QPushButton("填入此收藏夹")
        for button in (self.more_favorites, self.use_favorite, self.use_folder):
            button.setEnabled(False)
            library_actions.addWidget(button)
        library_actions.addStretch()
        library_layout.addLayout(library_actions)
        self.auto_library = QCheckBox("每 60 秒检查当前收藏夹变化（仅应用前台、空闲时）")
        self.auto_library.setChecked(True)
        library_layout.addWidget(self.auto_library)
        self.library_panel.hide()
        body_layout.addWidget(self.library_panel)
        self.inbox_panel = InboxPanel(self.avatar_for)
        self.inbox_panel.hide()
        body_layout.addWidget(self.inbox_panel)

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
        self.task_details = QWidget()
        detail_layout = QVBoxLayout(self.task_details)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(10)
        status_row = QHBoxLayout()
        self.task_title = self.label("准备就绪", "taskTitle")
        status_row.addWidget(self.task_title)
        status_row.addStretch()
        self.open_output = QPushButton("打开文件夹")
        self.open_output.setObjectName("link")
        status_row.addWidget(self.open_output)
        detail_layout.addLayout(status_row)
        self.status = self.label("粘贴内容链接，检查来源或直接开始下载。", "hint")
        detail_layout.addWidget(self.status)
        self.bar = QProgressBar()
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        detail_layout.addWidget(self.bar)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.document().setMaximumBlockCount(500)
        self.log.setFixedHeight(100)
        self.log.setAccessibleName("下载与操作详情")
        self.log.setPlaceholderText("操作详情会显示在这里；不记录 Cookie 或带签名的下载地址。")
        detail_layout.addWidget(self.log)
        self.task_details.hide()
        task_layout.addWidget(self.task_details)
        actions = QHBoxLayout()
        actions.setSpacing(12)
        self.details = DownloadButton()
        self.details.toggled.connect(self.task_details.setVisible)
        actions.addWidget(self.details)
        pacing = self.label("串行请求 · 间隔 0.5–1.5 秒", "finePrint")
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
        self.account_dialog.login_requested.connect(self.browser_login)
        self.account_dialog.restore_requested.connect(self.restore_login)
        self.account_dialog.cookie_requested.connect(self.cookie_login)
        self.account_dialog.forget_requested.connect(self.logout)
        self.account_dialog.cancel_requested.connect(self.engine.cancel)
        self.preview.clicked.connect(self.preview_source)
        self.open_output.clicked.connect(self.open_directory)
        self.library_toggle.toggled.connect(self.toggle_library)
        self.refresh_library.clicked.connect(lambda: self.load_library(force=True))
        self.folders.currentIndexChanged.connect(self.folder_changed)
        self.more_favorites.clicked.connect(self.load_more_favorites)
        self.use_favorite.clicked.connect(self.choose_favorite)
        self.use_folder.clicked.connect(self.choose_folder)
        self.favorite_items.itemSelectionChanged.connect(self.library_controls)
        self.favorite_items.itemDoubleClicked.connect(lambda *args: self.choose_favorite())
        self.inbox_toggle.toggled.connect(self.toggle_inbox)
        self.inbox_panel.refresh_requested.connect(self.load_inbox)
        self.inbox_panel.more_requested.connect(lambda: self.load_inbox(more=True))
        self.inbox_panel.open_requested.connect(self.load_thread)
        self.inbox_panel.older_requested.connect(lambda: self.load_thread(older=True))
        self.inbox_panel.send_requested.connect(self.send_reply)
        self.inbox_panel.reconcile_requested.connect(self.reconcile_reply)
        self.artwork_timer = QTimer(self)
        self.artwork_timer.setSingleShot(True)
        self.artwork_timer.setInterval(180)
        self.artwork_timer.timeout.connect(self.load_visible_artwork)
        self.favorite_items.verticalScrollBar().valueChanged.connect(
            lambda: self.artwork_timer.start()
        )
        self.inbox_panel.sessions.verticalScrollBar().valueChanged.connect(
            lambda: self.artwork_timer.start()
        )
        self.inbox_panel.body.verticalScrollBar().valueChanged.connect(
            lambda: self.artwork_timer.start()
        )
        self.scroll.verticalScrollBar().valueChanged.connect(lambda: self.artwork_timer.start())
        QApplication.instance().applicationStateChanged.connect(self.schedule_artwork)
        self.library_timer = QTimer(self)
        self.library_timer.setInterval(60000)
        self.library_timer.timeout.connect(self.check_library)
        self.library_timer.start()
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
        self.account_dialog.setVisible(visible)
        if visible:
            self.account_dialog.raise_()
            self.account_dialog.activateWindow()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "inbox_panel"):
            self.inbox_panel.set_compact(self.height() < 760)

    def toggle_library(self, visible):
        self.library_panel.setVisible(visible)
        if visible:
            self.inbox_toggle.setChecked(False)
            self.scroll.verticalScrollBar().setValue(0)
            if self._session_mid and not self._library_view.folders:
                self.load_library()
            self.artwork_timer.start()

    def toggle_inbox(self, visible):
        self.inbox_panel.setVisible(visible)
        self.intro.setVisible(not visible)
        if visible:
            self.library_toggle.setChecked(False)
            self.scroll.verticalScrollBar().setValue(0)
            if self._session_mid and self.inbox_panel.view is None:
                self.load_inbox()
            self.artwork_timer.start()

    def load_inbox(self, *, more=False):
        if self.busy or self._session_mid is None:
            return

        async def operation(api):
            if api.session is None:
                raise DownloadError("请先登录当前账号")
            return await (
                api.inbox.more(api.session.mid) if more else api.inbox.refresh(api.session.mid)
            )

        self.launch(
            operation, "正在读取私信会话", "只读取列表，不标记已读或发送消息。", kind="inbox"
        )

    def load_thread(self, talker_id=None, refresh=False, *, older=False):
        if self.busy or self._session_mid is None:
            return

        async def operation(api):
            if api.session is None:
                raise DownloadError("请先登录当前账号")
            if older:
                return await api.inbox.older(api.session.mid)
            return await api.inbox.open(talker_id, api.session.mid, refresh=refresh)

        self.launch(
            operation,
            "正在读取私信消息",
            "只读取所选会话，图片与未知类型显示占位文字。",
            kind="inbox",
        )

    def confirm_reply(self, talker_id, name, text):
        dialog = QMessageBox(
            QMessageBox.Question,
            "确认发送私信",
            f"将发送给 {name}（UID {talker_id}）。这是真实发送，不会自动重试。",
            QMessageBox.Yes | QMessageBox.No,
            self,
        )
        dialog.setTextFormat(Qt.PlainText)
        dialog.setInformativeText(text)
        dialog.setDefaultButton(QMessageBox.No)
        return dialog.exec_() == QMessageBox.Yes

    def send_reply(self, talker_id, text):
        if self.busy or self._session_mid is None or self.inbox_panel.pending:
            return
        try:
            validate_reply(text)
            item = self.inbox_panel.sessions.currentItem()
            if item is None or talker_id != self.inbox_panel.selected():
                raise DownloadError("收件人已变化，未发送")
            account_id = self._session_mid
            if not self.confirm_reply(talker_id, item.text(0), text):
                return
            if (
                self._session_mid != account_id
                or self.inbox_panel.selected() != talker_id
                or self.inbox_panel.reply.toPlainText() != text
            ):
                raise DownloadError("账号、收件人或草稿已变化，请重新确认；未发送")
        except DownloadError as error:
            self.show_error(str(error))
            return

        async def operation(api):
            if api.session is None or api.session.mid != account_id:
                raise DownloadError("登录身份已变化，未发送")
            return await api.send_message(talker_id, text)

        self.launch(
            operation,
            "正在发送私信",
            "单次提交并回读确认；发送中不可取消，避免误判后重复发送。",
            kind="send",
        )

    def reconcile_reply(self):
        if self.busy or self._session_mid is None:
            return

        async def operation(api):
            if api.session is None:
                raise DownloadError("请先登录当前账号")
            return await api.inbox.reconcile(api.session.mid)

        self.launch(operation, "正在核对发送结果", "只回读消息，不会再次发送。", kind="inbox")

    def check_library(self):
        if (
            self.auto_library.isChecked()
            and self.library_panel.isVisible()
            and not self.isMinimized()
            and QApplication.applicationState() == Qt.ApplicationActive
        ):
            self.load_library()

    def load_library(self, *, force=False):
        if self.busy or self._session_mid is None:
            return
        if force:
            self._artwork_paused = False
            self._artwork = OrderedDict(
                (url, pixmap) for url, pixmap in self._artwork.items() if not pixmap.isNull()
            )

        async def operation(api):
            if api.session is None:
                raise DownloadError("请先登录当前账号")
            return await api.favorites.refresh(api.session.mid, force=force)

        self.launch(
            operation,
            "正在检查收藏夹",
            "检查资源标识，仅在内容变化时重新读取条目。",
            kind="library",
        )

    def folder_changed(self):
        folder_id = self.folders.currentData()
        if self.busy or folder_id is None:
            return
        self.launch(lambda api: api.favorites.open(folder_id), "正在读取收藏夹", kind="library")

    def load_more_favorites(self):
        if not self.busy and self._library_view.has_more:
            self.launch(lambda api: api.favorites.more(), "正在加载下一页", kind="library")

    def library_controls(self):
        ready = not self.busy and self._session_mid is not None
        selected = self.favorite_items.currentItem()
        self.folders.setEnabled(ready)
        self.refresh_library.setEnabled(ready)
        self.more_favorites.setEnabled(ready and self._library_view.has_more)
        self.use_folder.setEnabled(ready and self._library_view.folder_id is not None)
        self.use_favorite.setEnabled(
            ready and selected is not None and bool(selected.data(0, Qt.UserRole))
        )

    def choose_favorite(self):
        selected = self.favorite_items.currentItem()
        if selected and selected.data(0, Qt.UserRole):
            self.mode.setCurrentIndex(self.mode.findData("auto"))
            self.source.setText(selected.data(0, Qt.UserRole))
            self.library_toggle.setChecked(False)
            self.scroll.ensureWidgetVisible(self.source)

    def choose_folder(self):
        if self._library_view.folder_id is not None:
            self.mode.setCurrentIndex(self.mode.findData("favorite"))
            self.source.setText(str(self._library_view.folder_id))
            self.library_toggle.setChecked(False)
            self.scroll.ensureWidgetVisible(self.source)

    def show_library(self, view):
        previous = self._library_view
        self._library_view = view
        self.folders.blockSignals(True)
        self.folders.clear()
        for folder in view.folders:
            self.folders.addItem(folder.title, folder.id)
            self.folders.setItemData(
                self.folders.count() - 1,
                f"ID {folder.id} · 服务端计数 {folder.media_count} 项（可能延迟更新）",
                Qt.ToolTipRole,
            )
        self.folders.setCurrentIndex(self.folders.findData(view.folder_id))
        self.folders.blockSignals(False)
        if previous.folder_id != view.folder_id or previous.items is not view.items:
            self.render_favorites(view.items, preserve=previous.folder_id == view.folder_id)
        if not view.folders:
            text = "当前账号没有创建的收藏夹。"
        else:
            change = "已更新条目" if view.refreshed else "标识未变化，复用缓存"
            text = f"{change} · 当前显示 {len(view.items)} 项" + (
                "，可加载下一页" if view.has_more else "，已到列表末尾"
            )
            if not view.digest:
                text += "；标识与条目暂不一致，下次检查不会跳过读取"
        self.library_hint.setText(text)
        self.library_hint.setToolTip(
            f"资源标识的本地 SHA-256：{view.digest}" if view.digest else ""
        )
        self.status.setText(text)
        self.library_controls()

    def render_favorites(self, items, *, preserve=False):
        current = self.favorite_items.currentItem()
        selected = current.data(0, Qt.UserRole) if current and preserve else None
        position = self.favorite_items.verticalScrollBar().value() if preserve else 0
        self.favorite_items.clear()
        for media in items:
            available = (
                media.type_name == 2
                and bool(media.bvid or media.bv_id)
                and media.attr not in {1, 9}
            )
            state = (
                "可读取" if available else ("已失效" if media.attr in {1, 9} else "暂不支持下载")
            )
            item = QTreeWidgetItem(
                [
                    media.title,
                    media.upper.name,
                    f"{media.duration // 60}:{media.duration % 60:02}",
                    state,
                ]
            )
            item.setData(0, Qt.UserRole, (media.bvid or media.bv_id) if available else "")
            item.setData(0, Qt.UserRole + 1, media.cover)
            pixmap = self._artwork.get(media.cover)
            item.setIcon(
                0,
                QIcon(pixmap)
                if pixmap is not None and not pixmap.isNull()
                else self._cover_placeholder,
            )
            for column in range(4):
                item.setToolTip(column, item.text(column))
            self.favorite_items.addTopLevelItem(item)
            if selected and item.data(0, Qt.UserRole) == selected:
                self.favorite_items.setCurrentItem(item)
        self.favorite_items.verticalScrollBar().setValue(position)

    def avatar_for(self, url, name):
        return avatar_icon(self._artwork.get(url, QPixmap()), name)

    def schedule_artwork(self, state):
        self.artwork_timer.start()

    def load_visible_artwork(self):
        if (
            self.busy
            or self._artwork_pending
            or self._artwork_paused
            or self._session_mid is None
            or self.isMinimized()
            or QApplication.applicationState() != Qt.ApplicationActive
        ):
            return
        urls = self.inbox_panel.visible_avatar_urls() if self.inbox_panel.isVisible() else []
        if self.library_panel.isVisible():
            visible = self.favorite_items.viewport().visibleRegion()
            for index in range(self.favorite_items.topLevelItemCount()):
                item = self.favorite_items.topLevelItem(index)
                if visible.intersects(self.favorite_items.visualItemRect(item)):
                    urls.append(item.data(0, Qt.UserRole + 1))
        for url in urls:
            if url and url not in self._artwork:
                self._artwork_pending = (self._session_mid, url)
                self.engine.request_artwork(url)
                return

    def show_artwork(self, mid, url, body, error):
        if self._artwork_pending != (mid, url):
            return
        self._artwork_pending = None
        if mid != self._session_mid:
            return
        if error:
            self._artwork_paused = True
            self.library_hint.setText(f"封面读取已暂停，条目仍可用：{error}")
            self.inbox_panel.send_notice.setText(f"头像读取已暂停，消息仍可用：{error}")
        elif body is not None:
            pixmap = decode_image(body)
            self._artwork[url] = pixmap
            self._artwork.move_to_end(url)
            while len(self._artwork) > 128:
                self._artwork.popitem(last=False)
            for index in range(self.favorite_items.topLevelItemCount()):
                item = self.favorite_items.topLevelItem(index)
                if item.data(0, Qt.UserRole + 1) == url and not pixmap.isNull():
                    item.setIcon(0, QIcon(pixmap))
            self.inbox_panel.update_avatar(url)
        self.artwork_timer.start()

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
        self.account_dialog.set_busy(busy, authorizing=busy and self._job == "login")
        for widget in (
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
            self.ffmpeg_advanced,
        ):
            widget.setEnabled(not busy)
        self.pattern.setEnabled(not busy and self.mode.currentData() == "user")
        self.options_changed()
        self.cancel.setEnabled(busy and self._job != "send")
        self.library_controls()
        self.inbox_panel.set_busy(busy, self._session_mid is not None)

    def launch(
        self,
        operation,
        title="正在处理",
        note="请求串行执行，可随时取消。",
        *,
        kind="task",
    ):
        if self.busy or self.engine.closing:
            return
        self._job = kind
        self.set_busy(True)
        if kind == "login":
            self.account_dialog.status.setText(note)
        self.details.set_progress(kind == "download")
        if kind != "library":
            self.details.setChecked(True)
        self.task_title.setText(title)
        self.status.setText(note)
        self.bar.setRange(0, 0)
        try:
            self.engine.submit(operation)
        except Exception as error:
            self.show_error(safe_error(error))
            self.on_idle()

    def on_idle(self):
        self._job = None
        self.details.set_progress(False)
        self.set_busy(False)
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
        if self._pending_library:
            self._pending_library = False
            QTimer.singleShot(0, self.load_library)
        self.artwork_timer.start()

    def show_error(self, text):
        self.details.setChecked(True)
        if self._job == "login":
            self.account_dialog.status.setText(text)
        if self._job in {"inbox", "send"}:
            self.inbox_panel.hint.setText(f"{text}；已读取的内容保留，可手动刷新。")
            self.inbox_panel.title.setText("本次私信读取未完成")
        if self._job == "library":
            self.auto_library.setChecked(False)
            self.library_hint.setText(f"{text}；自动检查已暂停，原有缓存保留。")
        self.task_title.setText("未能完成")
        self.status.setText(text)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.log.appendPlainText(text)

    def show_cancelled(self):
        self.details.setChecked(True)
        if self._job == "login":
            self.account_dialog.status.setText("登录已取消；没有保存新的凭证。")
        if self._job == "inbox":
            self.inbox_panel.title.setText("已取消读取；已有内容保留")
        if self._job == "library":
            self.auto_library.setChecked(False)
        self.task_title.setText("已取消")
        self.status.setText("任务已取消；没有发布不完整文件，已完成文件会保留。")
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.log.appendPlainText(self.status.text())

    def show_notice(self, text):
        self.status.setText(text)
        if self._job == "login":
            self.account_dialog.status.setText(text)
        self.log.appendPlainText(text)

    def show_progress(self, event: Progress):
        self.status.setText(event.message)
        if event.phase == "bytes":
            megabytes = event.completed / 1024 / 1024
            self.status.setText(
                f"{event.message} · {megabytes:.1f} MiB"
                + (f" / {event.total / 1024 / 1024:.1f} MiB" if event.total else "")
            )
        else:
            self.log.appendPlainText(event.message)
        if event.phase != "summary":
            self.bar.setRange(0, 100 if event.percent is not None else 0)
            if event.percent is not None:
                self.bar.setValue(event.percent)
            if self._job == "download":
                self.details.set_progress(True, event.percent)
                if event.phase == "done":
                    self.details.completed()

    def show_session(self, session):
        mid = session.mid if session else None
        if mid != self._session_mid:
            self._library_view = FavoriteView()
            self.folders.blockSignals(True)
            self.folders.clear()
            self.folders.blockSignals(False)
            self.favorite_items.clear()
            self._artwork.clear()
            self._artwork_pending = None
            self._artwork_paused = False
            self.inbox_panel.clear()
        self._session_mid = mid
        self.library_toggle.setEnabled(session is not None)
        self.inbox_toggle.setEnabled(session is not None)
        self.identity.setText(
            self.identity.fontMetrics().elidedText(session.name, Qt.ElideRight, 200)
            if session
            else ""
        )
        self.identity.setVisible(session is not None)
        self.identity.setToolTip(f"{session.name} · UID {session.mid}" if session else "")
        self.inbox_panel.set_account(mid, self.show_avatar(session))
        if session is None:
            self.library_toggle.setChecked(False)
            self.inbox_toggle.setChecked(False)
        self.library_controls()
        self.inbox_panel.set_busy(self.busy, session is not None)
        self.account_dialog.identity.setText(
            f"{session.name} · UID {session.mid}"
            if session
            else "需要账号权限时，再在 Chrome 中登录。"
        )

    def show_avatar(self, session):
        self.account_toggle.setIcon(QIcon())
        if session is None:
            self.account_toggle.setText("登录")
            self.account_toggle.setAccessibleName("登录：账号与登录")
            self.account_toggle.setToolTip("登录 · 账号与登录")
            return avatar_icon(QPixmap(), "我")
        self.account_toggle.setText("")
        self.account_toggle.setAccessibleName(f"{session.name}的头像：账号与登录")
        self.account_toggle.setToolTip(f"{session.name} · UID {session.mid}\n账号与登录")
        pixmap = decode_image(session.avatar)
        icon = avatar_icon(pixmap, session.name, 36)
        self.account_toggle.setIcon(icon)
        return icon

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
            self.account_dialog.accept()
            self.account_dialog.status.setText("登录成功，可在此管理登录或退出账号。")
            self._pending_library = True
            self.auto_library.setChecked(True)
            self.library_toggle.setChecked(True)
        elif result == "logout":
            self.status.setText("已退出并忘记本应用保存的登录；未操作其他客户端。")
        elif isinstance(result, FavoriteView):
            self.task_title.setText("收藏夹已检查")
            self.show_library(result)
        elif isinstance(result, InboxView):
            self.task_title.setText("私信会话已读取")
            self.inbox_panel.show_inbox(result, self._session_mid)
            self.status.setText(self.inbox_panel.hint.text())
        elif isinstance(result, MessageView):
            self.task_title.setText("私信消息已读取")
            self.inbox_panel.show_messages(result)
            self.status.setText(f"本次显示 {len(result.messages)} 条私信，查看未标记已读。")
        elif isinstance(result, SendReceipt):
            self.inbox_panel.show_receipt(result)
            self.task_title.setText("发送已确认" if result.confirmed else "发送待核对")
            self.status.setText(result.notice)
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
        self.details.setChecked(isinstance(result, SendReceipt) and not result.confirmed)

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

    def browser_login(self, remember):
        async def operation(api):
            session = await chrome_login(api, self.engine.notice.emit)
            return self.finish_login(session, remember)

        self.launch(
            operation,
            "等待 Chrome 登录",
            "请在打开的 Chrome 中登录，完成后会自动回到这里。",
            kind="login",
        )

    def cookie_login(self, value, remember):
        async def operation(api):
            return self.finish_login(await api.use_cookie(value), remember)

        self.launch(operation, "正在验证登录", kind="login")

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

        self.launch(operation, "正在读取钥匙串登录", kind="login")

    def logout(self):
        if (
            self.inbox_panel.pending
            and QMessageBox.question(
                self,
                "发送结果尚待核对",
                "退出会清除本地核对信息，但不会重发。请先在官方客户端确认，仍要退出并忘记吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            return

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
            kind="download",
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
        if self.busy and self._job == "send":
            self.status.setText("正在提交私信，请等候结果；不能取消后直接重发。")
            self.details.setChecked(True)
            event.ignore()
            return
        if (
            self.inbox_panel.pending
            and QMessageBox.question(
                self,
                "发送结果尚待核对",
                "请先在官方客户端确认。退出后不会保存核对信息，也不会自动重发，仍要退出吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            != QMessageBox.Yes
        ):
            event.ignore()
            return
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
        self._pending_library = False
        self.artwork_timer.stop()
        self.library_timer.stop()
        self.account_dialog.hide()
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
