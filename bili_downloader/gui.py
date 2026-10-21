"""PyQt5 UI; one persistent asyncio worker owns the bpi-py session and request gate."""

import asyncio
import sys
import threading
from collections.abc import Callable
from pathlib import Path

from PyQt5.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
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
    QImageReader,
    QPainter,
    QPainterPath,
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

from .api import Bilibili, safe_error
from .auth import Session, forget_session, load_session, save_session
from .browser_login import chrome_login
from .download import Progress, download_source
from .download_button import DownloadButton
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
        self._session_mid = None
        self._pending_library = False
        self._library_job = False
        self._download_job = False
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
        self.login.clicked.connect(self.browser_login)
        self.restore.clicked.connect(self.restore_login)
        self.import_cookie.clicked.connect(self.cookie_login)
        self.forget.clicked.connect(self.logout)
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
        self.account_panel.setVisible(visible)
        if visible:
            self.scroll.verticalScrollBar().setValue(0)

    def toggle_library(self, visible):
        self.library_panel.setVisible(visible)
        if visible:
            self.scroll.verticalScrollBar().setValue(0)
            if self._session_mid and not self._library_view.folders:
                self.load_library()

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

        async def operation(api):
            if api.session is None:
                raise DownloadError("请先登录当前账号")
            return await api.favorites.refresh(api.session.mid, force=force)

        self.launch(
            operation, "正在检查收藏夹", "检查资源标识，仅在内容变化时重新读取条目。", library=True
        )

    def folder_changed(self):
        folder_id = self.folders.currentData()
        if self.busy or folder_id is None:
            return
        self.launch(lambda api: api.favorites.open(folder_id), "正在读取收藏夹", library=True)

    def load_more_favorites(self):
        if not self.busy and self._library_view.has_more:
            self.launch(lambda api: api.favorites.more(), "正在加载下一页", library=True)

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
            for column in range(4):
                item.setToolTip(column, item.text(column))
            self.favorite_items.addTopLevelItem(item)
            if selected and item.data(0, Qt.UserRole) == selected:
                self.favorite_items.setCurrentItem(item)
        self.favorite_items.verticalScrollBar().setValue(position)

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
        self.library_controls()

    def launch(
        self,
        operation,
        title="正在处理",
        note="请求串行执行，可随时取消。",
        *,
        library=False,
        download=False,
    ):
        if self.busy:
            return
        self.set_busy(True)
        self._library_job = library
        self._download_job = download
        self.details.set_progress(download)
        if not library:
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
        self._library_job = False
        self._download_job = False
        self.details.set_progress(False)
        self.set_busy(False)
        if self.bar.maximum() == 0:
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
        if self._pending_library:
            self._pending_library = False
            QTimer.singleShot(0, self.load_library)

    def show_error(self, text):
        self.details.setChecked(True)
        if self._library_job:
            self.auto_library.setChecked(False)
            self.library_hint.setText(f"{text}；自动检查已暂停，原有缓存保留。")
        self.task_title.setText("未能完成")
        self.status.setText(text)
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.log.appendPlainText(text)

    def show_cancelled(self):
        self.details.setChecked(True)
        if self._library_job:
            self.auto_library.setChecked(False)
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
                f"{event.message} · {megabytes:.1f} MiB"
                + (f" / {event.total / 1024 / 1024:.1f} MiB" if event.total else "")
            )
        else:
            self.log.appendPlainText(event.message)
        if event.phase != "summary":
            self.bar.setRange(0, 100 if event.percent is not None else 0)
            if event.percent is not None:
                self.bar.setValue(event.percent)
            if self._download_job:
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
        self._session_mid = mid
        self.library_toggle.setEnabled(session is not None)
        self.identity.setText(
            self.identity.fontMetrics().elidedText(session.name, Qt.ElideRight, 200)
            if session
            else ""
        )
        self.identity.setVisible(session is not None)
        self.identity.setToolTip(f"{session.name} · UID {session.mid}" if session else "")
        self.show_avatar(session)
        if session is None:
            self.library_toggle.setChecked(False)
        self.library_controls()
        self.account_identity.setText(
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
            return
        self.account_toggle.setText("")
        self.account_toggle.setAccessibleName(f"{session.name}的头像：账号与登录")
        self.account_toggle.setToolTip(f"{session.name} · UID {session.mid}\n账号与登录")
        if session.avatar:
            buffer = QBuffer()
            buffer.setData(QByteArray(session.avatar))
            buffer.open(QIODevice.ReadOnly)
            reader = QImageReader(buffer)
            size = reader.size()
            if size.isValid() and max(size.width(), size.height()) <= 4096:
                reader.setScaledSize(size.scaled(QSize(72, 72), Qt.KeepAspectRatioByExpanding))
                image = reader.read()
                if not image.isNull():
                    source = QPixmap.fromImage(image).scaled(
                        36, 36, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
                    )
                    pixmap = QPixmap(36, 36)
                    pixmap.fill(Qt.transparent)
                    painter = QPainter(pixmap)
                    painter.setRenderHint(QPainter.Antialiasing)
                    clip = QPainterPath()
                    clip.addEllipse(0, 0, 36, 36)
                    painter.setClipPath(clip)
                    painter.drawPixmap(
                        (36 - source.width()) // 2, (36 - source.height()) // 2, source
                    )
                    painter.end()
                    self.account_toggle.setIcon(QIcon(pixmap))
                    return
        self.account_toggle.setText(session.name[:1] or "B")

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
            self.account_toggle.setChecked(False)
            self._pending_library = True
            self.auto_library.setChecked(True)
            self.library_toggle.setChecked(True)
        elif result == "logout":
            self.status.setText("已退出并忘记本应用保存的登录；未操作其他客户端。")
        elif isinstance(result, FavoriteView):
            self.task_title.setText("收藏夹已检查")
            self.show_library(result)
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
        self.details.setChecked(False)

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
            download=True,
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
