"""Following directory, collection progress and the selected creator's pages."""

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .account_content import local_time

INTERACTIONS = (
    ("点赞", "liked", "已赞", "未赞"),
    ("投币", "coined", "已投", "未投"),
    ("转发", "reposted", "已转", "未转"),
)


def interaction_label(entry, field, yes, no, now):
    value = getattr(entry, field)
    if value is None:
        return "未知"
    label = yes if value else no
    return label if entry.is_current(field, now) else label + "（待核验）"


def interaction_detail(entry, field, now):
    confirmed = getattr(entry, field + "_at")
    stamp = local_time(confirmed) if confirmed is not None else "暂无"
    detail = "上次确认：" + stamp + "。"
    if getattr(entry, field) is None:
        detail += "未知表示尚无可用的个人状态，不能视为未操作。"
    elif not entry.is_current(field, now):
        detail += "待核验表示保存的上次状态尚未重新确认，不参与当前比例。"
    else:
        detail += "已核验，参与当前比例。"
    if field == "reposted":
        detail += "个人转发不能用转发总数推断。"
    return detail


def interaction_text(entry, now):
    return "\n".join(
        f"{name} {interaction_label(entry, field, yes, no, now)} · "
        + interaction_detail(entry, field, now)
        for name, field, yes, no in INTERACTIONS
    )


class FollowingPanel(QFrame):
    list_requested = pyqtSignal(bool)
    creator_requested = pyqtSignal(object, str, int, bool, bool)
    creator_cancel_requested = pyqtSignal()
    open_requested = pyqtSignal(str)
    collect_requested = pyqtSignal()
    pause_requested = pyqtSignal()
    recent_requested = pyqtSignal()

    def __init__(self, avatars, images):
        super().__init__()
        self.setObjectName("card")
        self.avatars, self.images = avatars, images
        self.view, self.creator_view = None, None
        self.busy, self.logged_in, self.page = False, False, 1
        self.collection, self.collecting = None, False
        self.loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        heading = QHBoxLayout()
        title = QLabel("关注")
        title.setObjectName("sectionTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.refresh = QPushButton("刷新关注列表")
        self.more = QPushButton("更多关注")
        heading.addWidget(self.refresh)
        heading.addWidget(self.more)
        layout.addLayout(heading)
        self.hint = QLabel("关注名单每页 50 人；可分轮收齐所有关注者的最新动态页与投稿页。")
        self.hint.setTextFormat(Qt.PlainText)
        self.hint.setWordWrap(True)
        self.hint.setObjectName("hint")
        layout.addWidget(self.hint)
        collection_controls = QHBoxLayout()
        self.collect = QPushButton("采集全部最新页")
        self.collect.setToolTip(
            "分轮收齐关注名单和每人的最新动态/投稿页；个人互动仅在浏览当前页时核验，不回溯历史。"
        )
        self.pause = QPushButton("暂停采集")
        self.recent = QPushButton("检查近期操作")
        self.recent.setToolTip(
            "只读取本账号最近点赞、投币和本人动态首屏；不是全部历史。"
            "列表缺失不算取消，不遍历所有关注者的视频。"
        )
        collection_controls.addWidget(self.collect)
        collection_controls.addWidget(self.pause)
        collection_controls.addWidget(self.recent)
        collection_controls.addStretch()
        layout.addLayout(collection_controls)
        self.collection_status = QLabel("尚未开始全部采集；随时可暂停，重启后可从保存的进度继续。")
        self.collection_status.setTextFormat(Qt.PlainText)
        self.collection_status.setWordWrap(True)
        self.collection_status.setObjectName("hint")
        layout.addWidget(self.collection_status)
        self.special_only = QCheckBox("只看特别关注（当前已加载名单）")
        layout.addWidget(self.special_only)
        self.splitter = QSplitter(Qt.Horizontal)
        self.people = QTreeWidget()
        self.people.setAccessibleName("关注的人列表")
        self.people.setHeaderLabels(["UP 主"])
        self.people.setRootIsDecorated(False)
        self.people.setIconSize(QSize(32, 32))
        self.people.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.splitter.addWidget(self.people)
        right = QWidget()
        content = QVBoxLayout(right)
        content.setContentsMargins(8, 0, 0, 0)
        self.author = QLabel("选择一位关注的 UP 主")
        self.author.setTextFormat(Qt.PlainText)
        content.addWidget(self.author)
        controls = QHBoxLayout()
        self.tabs = QTabBar()
        self.tabs.addTab("动态")
        self.tabs.addTab("视频投稿")
        controls.addWidget(self.tabs)
        controls.addStretch()
        self.refresh_creator = QPushButton("检查最新页")
        self.verify = QPushButton("校验本页互动")
        self.verify.setToolTip("只读核验本页每个视频的点赞和投币状态；共用请求间隔，可取消。")
        controls.addWidget(self.refresh_creator)
        controls.addWidget(self.verify)
        content.addLayout(controls)
        self.stats = QLabel("点赞 / 投币 / 转发：选择 UP 主后统计本页；未知不计为未互动。")
        self.stats.setTextFormat(Qt.PlainText)
        self.stats.setWordWrap(True)
        self.stats.setObjectName("hint")
        self.stats.setToolTip(
            "按本页每条内容是否有该个人行为统计，投币按是否投过币计数，不累加币数。"
            "动态点赞与视频点赞是不同目标；动态页投币指附带视频。"
            "比例的分母仅含当前已核验的条目；待核验的上次状态与未知均不参与。"
            "缓存保留上次状态，待核验不表示未操作。"
        )
        content.addWidget(self.stats)
        self.items = QTreeWidget()
        self.items.setAccessibleName("关注者内容列表")
        self.items.setHeaderLabels(["内容", "点赞", "投币", "转发"])
        self.items.setRootIsDecorated(False)
        self.items.setIconSize(QSize(48, 40))
        self.items.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 4):
            self.items.header().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        content.addWidget(self.items, 1)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setMaximumHeight(110)
        self.body.setPlaceholderText("选择条目查看正文；未知表示接口未提供或尚待核验。")
        content.addWidget(self.body)
        paging = QHBoxLayout()
        self.previous = QPushButton("上一页")
        self.next = QPushButton("下一页")
        self.page_label = QLabel("第 1 页")
        self.open = QPushButton("打开原文")
        for widget in (self.previous, self.page_label, self.next, self.open):
            paging.addWidget(widget)
        paging.addStretch()
        content.addLayout(paging)
        self.notice = QLabel("动态点赞指动态本身；动态页的投币指附带视频。转发通常无法完整判定。")
        self.notice.setTextFormat(Qt.PlainText)
        self.notice.setWordWrap(True)
        self.notice.setObjectName("hint")
        content.addWidget(self.notice)
        self.splitter.addWidget(right)
        self.splitter.setSizes([250, 650])
        self.splitter.setMinimumHeight(350)
        layout.addWidget(self.splitter)
        self.refresh.clicked.connect(lambda: self.list_requested.emit(False))
        self.more.clicked.connect(lambda: self.list_requested.emit(True))
        self.collect.clicked.connect(self.collect_requested.emit)
        self.pause.clicked.connect(self.pause_requested.emit)
        self.recent.clicked.connect(self.recent_requested.emit)
        self.special_only.toggled.connect(self.render_people)
        self.people.itemSelectionChanged.connect(self.select_person)
        self.tabs.currentChanged.connect(self.select_person)
        self.refresh_creator.clicked.connect(lambda: self.request_page(1, refresh=True))
        self.verify.clicked.connect(self.verify_current)
        self.previous.clicked.connect(lambda: self.request_page(self.page - 1))
        self.next.clicked.connect(lambda: self.request_page(self.page + 1))
        self.items.itemSelectionChanged.connect(self.select_item)
        self.open.clicked.connect(self.open_item)
        self.update_controls()

    @property
    def kind(self):
        return ("dynamic", "videos")[self.tabs.currentIndex()]

    def selected(self):
        item = self.people.currentItem()
        return int(item.data(0, Qt.UserRole).key) if item else None

    def show_followings(self, view):
        self.view = view
        self.render_people()

    def render_people(self):
        selected = self.selected()
        self.people.blockSignals(True)
        self.people.clear()
        rows = sorted(self.view.items, key=lambda row: row.special is not True) if self.view else ()
        for row in rows:
            if self.special_only.isChecked() and row.special is not True:
                continue
            item = QTreeWidgetItem([row.title])
            item.setData(0, Qt.UserRole, row)
            item.setToolTip(0, row.body)
            item.setIcon(0, self.avatars(row.image, row.title, special=row.special is True))
            self.people.addTopLevelItem(item)
            if row.key == str(selected):
                self.people.setCurrentItem(item)
        self.people.blockSignals(False)
        if self.selected() is None:
            if selected is not None:
                self.creator_cancel_requested.emit()
            self.creator_view = None
            self.items.clear()
            self.body.clear()
            self.author.setText("选择一位关注的 UP 主")
            self.stats.setText("选择 UP 主查看当前页统计；全部采集进度显示在上方。")
        if self.view:
            special = sum(row.special is True for row in self.view.items)
            unknown = sum(row.special is None for row in self.view.items)
            self.hint.setText(
                ("本地缓存 · " if self.view.cached else "")
                + f"已加载 {len(self.view.items)} 人 · 特别关注 {special} 人"
                + f" · 未核验 {unknown} 人。"
                + (" 可加载更多。" if self.view.has_more else " 名单已到末尾。")
            )
        self.update_controls()

    def select_person(self):
        self.creator_view, self.page = None, 1
        self.items.clear()
        self.body.clear()
        row = self.people.currentItem()
        self.author.setText(row.text(0) if row else "选择一位关注的 UP 主")
        self.stats.setText("后台读取最新页，可继续切换 UP 主…" if row else "选择 UP 主后统计本页。")
        self.update_controls()
        if row and not self.busy:
            self.request_page(1)
        elif row is None:
            self.creator_cancel_requested.emit()

    def verify_current(self):
        if self.loading:
            self.creator_cancel_requested.emit()
        else:
            self.request_page(self.page, verify=True)

    def request_page(self, page, *, refresh=False, verify=False):
        mid = self.selected()
        if self.busy or mid is None or page < 1:
            return
        self.page = page
        self.creator_requested.emit(mid, self.kind, page, refresh, verify)

    def show_creator(self, view):
        if (view.mid, view.kind, view.page) != (self.selected(), self.kind, self.page):
            return
        previous = self.items.currentItem()
        selected = previous.data(0, Qt.UserRole).content.key if previous else None
        self.creator_view = view
        self.items.clear()
        self.body.clear()
        for entry in view.entries:
            row = entry.content
            item = QTreeWidgetItem(
                [
                    row.title,
                    *(
                        interaction_label(entry, field, yes, no, view.observed_at)
                        for _, field, yes, no in INTERACTIONS
                    ),
                ]
            )
            item.setData(0, Qt.UserRole, entry)
            item.setToolTip(0, row.summary)
            for column, (_, field, _, _) in enumerate(INTERACTIONS, 1):
                item.setTextAlignment(column, Qt.AlignCenter)
                item.setToolTip(
                    column,
                    interaction_detail(entry, field, view.observed_at),
                )
            item.setIcon(
                0,
                self.avatars(row.image, row.title)
                if row.image_role == "avatar"
                else QIcon(self.images(row.image)),
            )
            self.items.addTopLevelItem(item)
            if row.key == selected:
                self.items.setCurrentItem(item)
        self.page_label.setText(f"第 {view.page} 页 · {len(view.entries)} 条")
        self.stats.setText(view.statistics())
        self.notice.setText(
            ("本地缓存 · " if view.cached else "")
            + "正文采集于 "
            + local_time(view.checked_at)
            + "。 "
            + (view.notice or "个人互动仅核验当前页；待核验保留上次状态，可用“校验本页互动”更新。")
        )
        self.update_controls()

    def select_item(self):
        item = self.items.currentItem()
        entry = item.data(0, Qt.UserRole) if item else None
        self.body.setPlainText(
            entry.content.body
            + "\n\n我的互动：\n"
            + interaction_text(entry, self.creator_view.observed_at)
            if entry
            else ""
        )
        self.update_controls()

    def open_item(self):
        item = self.items.currentItem()
        if item and self.open.isEnabled():
            self.open_requested.emit(item.data(0, Qt.UserRole).content.url)

    def set_busy(self, busy, logged_in):
        self.busy, self.logged_in = busy, logged_in
        self.update_controls()

    def set_loading(self, loading):
        self.loading = loading
        if not loading and self.creator_view is not None:
            self.page = self.creator_view.page
        if not loading and self.creator_view is None:
            self.stats.setText(
                "暂无可统计内容；可重新检查最新页。"
                if self.selected() is not None
                else "选择 UP 主后统计本页。"
            )
        self.verify.setText("停止读取" if loading else "校验本页互动")
        self.verify.setToolTip(
            "停止当前只读采集，已取得的内容和互动保留。"
            if loading
            else "只读核验本页点赞和投币；切换 UP 主会优先读取新的选择。"
        )
        self.update_controls()

    def show_collection(self, progress, *, running=False):
        self.collection, self.collecting = progress, running
        if progress is not None:
            self.collection_status.setText(
                progress.notice + (" · 已暂停" if not running and not progress.done else "")
            )
        self.collect.setText(
            "重新采集全部最新页"
            if progress and progress.done
            else "继续分轮采集"
            if progress
            else "采集全部最新页"
        )
        self.update_controls()

    def update_controls(self):
        ready = self.logged_in and not self.busy
        self.collect.setEnabled(ready and not self.collecting and not self.loading)
        self.pause.setEnabled(self.logged_in and self.collecting)
        self.recent.setEnabled(ready and not self.collecting and not self.loading)
        target = ready and self.selected() is not None
        self.people.setEnabled(ready)
        self.special_only.setEnabled(ready)
        self.refresh.setEnabled(ready)
        self.more.setEnabled(ready and self.view is not None and self.view.has_more)
        self.tabs.setEnabled(ready)
        self.refresh_creator.setEnabled(target)
        self.verify.setEnabled(target and (self.loading or self.creator_view is not None))
        self.previous.setEnabled(target and not self.loading and self.page > 1)
        self.next.setEnabled(
            target
            and not self.loading
            and self.creator_view is not None
            and self.creator_view.has_more
        )
        self.open.setEnabled(ready and self.items.currentItem() is not None)

    def clear(self):
        self.view, self.creator_view, self.page = None, None, 1
        self.set_loading(False)
        self.special_only.setChecked(False)
        self.show_collection(None)
        self.collection_status.setText("尚未开始全部采集；随时可暂停，重启后可从保存的进度继续。")
        self.render_people()
        self.hint.setText("登录后按需读取关注名单。")
        self.set_busy(False, False)

    def visible_artwork_urls(self):
        urls = []
        for tree in (self.people, self.items):
            region = tree.viewport().visibleRegion()
            for index in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(index)
                if region.intersects(tree.visualItemRect(item)):
                    row = item.data(0, Qt.UserRole)
                    urls.append(row.image if tree is self.people else row.content.image)
        return urls

    def update_artwork(self, url):
        for tree in (self.people, self.items):
            for index in range(tree.topLevelItemCount()):
                item = tree.topLevelItem(index)
                row = item.data(0, Qt.UserRole)
                row = row if tree is self.people else row.content
                if row.image == url:
                    item.setIcon(
                        0,
                        self.avatars(row.image, row.title, special=row.special is True)
                        if tree is self.people
                        else self.avatars(row.image, row.title)
                        if row.image_role == "avatar"
                        else QIcon(self.images(row.image)),
                    )
