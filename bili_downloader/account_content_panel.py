"""Plain-text, read-only unified feed and history; followings have their own panel."""

from bilibili_client.account_content import TITLES
from bilibili_client.cache import canonical_image_url
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
    QTabBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)


class AccountContentPanel(QFrame):
    read_requested = pyqtSignal(bool, bool)
    tab_changed = pyqtSignal()
    open_requested = pyqtSignal(str)

    def __init__(self, avatars, images):
        super().__init__()
        self.setObjectName("card")
        self.avatars, self.images = avatars, images
        self.kinds = ("dynamic", "history")
        self.views = {}
        self.busy, self.logged_in = False, False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        heading = QHBoxLayout()
        self.tabs = QTabBar()
        self.tabs.setAccessibleName("账号内容类别")
        for kind in self.kinds:
            self.tabs.addTab(TITLES[kind])
        heading.addWidget(self.tabs, 1)
        self.refresh = QPushButton("刷新")
        heading.addWidget(self.refresh)
        layout.addLayout(heading)
        self.hint = QLabel()
        self.hint.setTextFormat(Qt.PlainText)
        self.hint.setWordWrap(True)
        self.hint.setObjectName("hint")
        layout.addWidget(self.hint)
        self.items = QTreeWidget()
        self.items.setHeaderLabels(["内容", "信息"])
        self.items.setRootIsDecorated(False)
        self.items.setIconSize(QSize(48, 40))
        self.items.setMinimumHeight(140)
        self.items.setMaximumHeight(210)
        self.items.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.items.header().setSectionResizeMode(1, QHeaderView.Interactive)
        self.items.setColumnWidth(1, 230)
        layout.addWidget(self.items)
        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setAccessibleName("账号内容详情")
        self.body.setPlaceholderText("选择条目查看正文与信息；不会标记已读或执行写操作。")
        self.body.setMaximumHeight(180)
        layout.addWidget(self.body)
        actions = QHBoxLayout()
        self.more = QPushButton("加载更多")
        self.open = QPushButton("打开原文")
        actions.addWidget(self.more)
        actions.addWidget(self.open)
        actions.addStretch()
        layout.addLayout(actions)
        self.auto = QCheckBox("每 60 秒检查新动态（仅此标签可见、应用前台且空闲时）")
        self.auto.setChecked(True)
        layout.addWidget(self.auto)
        self.tabs.currentChanged.connect(self.change_tab)
        self.refresh.clicked.connect(lambda: self.read_requested.emit(False, False))
        self.more.clicked.connect(lambda: self.read_requested.emit(True, False))
        self.items.itemSelectionChanged.connect(self.selection_changed)
        self.open.clicked.connect(self.open_selected)
        self.render()

    @property
    def kind(self):
        return self.kinds[self.tabs.currentIndex()]

    def change_tab(self):
        self.render()
        self.tab_changed.emit()

    def show_view(self, view):
        previous = self.views.get(view.kind)
        self.views[view.kind] = view
        if view.kind == self.kind:
            self.render(
                preserve=previous is not None and previous.items is view.items,
                keep_selection=previous is not None,
            )

    def render(self, *, preserve=False, keep_selection=False):
        view = self.views.get(self.kind)
        self.items.setAccessibleName(TITLES[self.kind] + "列表")
        self.auto.setVisible(self.kind == "dynamic")
        if not preserve:
            selected = self.items.currentItem()
            key = selected.data(0, Qt.UserRole).key if selected and keep_selection else None
            position = self.items.verticalScrollBar().value() if keep_selection else 0
            self.items.clear()
            self.body.clear()
            for row in view.items if view else ():
                item = QTreeWidgetItem([row.title, row.summary])
                item.setData(0, Qt.UserRole, row)
                for column in (0, 1):
                    item.setToolTip(column, item.text(column))
                item.setIcon(0, self.icon_for(row))
                self.items.addTopLevelItem(item)
                if row.key == key:
                    self.items.setCurrentItem(item)
            self.items.verticalScrollBar().setValue(position)
        self.hint.setText(
            ("本地缓存，刷新可校验最新内容。 " if view.cached else "")
            + f"已加载 {len(view.items)} 项 · "
            + ("可加载更多。 " if view.has_more else "本次列表已结束。 ")
            + view.notice
            if view
            else "首次只读取当前页；已加载页在本机缓存，可按需加载更早内容。"
        )
        self.update_controls()

    def set_compact(self, compact):
        self.items.setMinimumHeight(110 if compact else 140)
        self.items.setMaximumHeight(150 if compact else 210)
        self.body.setMaximumHeight(120 if compact else 180)

    def selection_changed(self):
        item = self.items.currentItem()
        self.body.setPlainText(item.data(0, Qt.UserRole).body if item else "")
        self.update_controls()

    def open_selected(self):
        item = self.items.currentItem()
        if item and self.open.isEnabled():
            self.open_requested.emit(item.data(0, Qt.UserRole).url)

    def set_busy(self, busy, logged_in):
        self.busy, self.logged_in = busy, logged_in
        self.update_controls()

    def update_controls(self):
        ready = self.logged_in and not self.busy
        view = self.views.get(self.kind)
        self.tabs.setEnabled(not self.busy)
        self.refresh.setEnabled(ready)
        self.more.setEnabled(ready and view is not None and view.has_more)
        item = self.items.currentItem()
        self.open.setEnabled(ready and item is not None and bool(item.data(0, Qt.UserRole).url))

    def clear(self):
        self.views.clear()
        self.auto.setChecked(True)
        self.render()
        self.set_busy(False, False)

    def visible_artwork_urls(self):
        region = self.items.viewport().visibleRegion()
        return [
            item.data(0, Qt.UserRole).image
            for index in range(self.items.topLevelItemCount())
            if region.intersects(self.items.visualItemRect(item := self.items.topLevelItem(index)))
        ]

    def icon_for(self, row):
        if self.kind == "followings" or row.image_role == "avatar":
            return self.avatars(row.image, row.title)
        return QIcon(self.images(row.image))

    def update_artwork(self, urls):
        urls = {canonical_image_url(url) for url in urls}
        for index in range(self.items.topLevelItemCount()):
            item = self.items.topLevelItem(index)
            row = item.data(0, Qt.UserRole)
            if canonical_image_url(row.image) in urls:
                item.setIcon(0, self.icon_for(row))
