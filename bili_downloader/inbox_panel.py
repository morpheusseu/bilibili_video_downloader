"""Native, plain-text inbox presentation; never performs provider requests itself."""

from datetime import datetime

from PyQt5.QtCore import QSize, Qt, pyqtSignal
from PyQt5.QtGui import QKeySequence, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .artwork import avatar_icon
from .cache import canonical_image_url
from .inbox import message_text
from .message_content import MessageImage, message_content
from .message_rendering import CONTENT_ROLE, HEADER_ROLE, MessageDelegate
from .theme import COLORS


class ReplyEditor(QPlainTextEdit):
    submit_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.composing = False

    def inputMethodEvent(self, event):
        self.composing = bool(event.preeditString())
        super().inputMethodEvent(event)

    def keyPressEvent(self, event):
        if (
            event.key() in (Qt.Key_Return, Qt.Key_Enter)
            and event.modifiers() & (Qt.ControlModifier | Qt.MetaModifier)
            and not self.composing
        ):
            self.submit_requested.emit()
            event.accept()
        else:
            super().keyPressEvent(event)


class MessageList(QListWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.scheduleDelayedItemsLayout()

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy):
            QApplication.clipboard().setText(
                "\n\n".join(item.text() for item in self.selectedItems())
            )
            event.accept()
        else:
            super().keyPressEvent(event)


class InboxPanel(QFrame):
    refresh_requested = pyqtSignal()
    more_requested = pyqtSignal()
    open_requested = pyqtSignal(object, bool)
    older_requested = pyqtSignal()
    send_requested = pyqtSignal(object, str)
    reconcile_requested = pyqtSignal()
    reconnect_requested = pyqtSignal()
    expand_requested = pyqtSignal()

    def __init__(self, avatars, images):
        super().__init__()
        self.setObjectName("card")
        self.thread = None
        self.view = None
        self.account_id = None
        self.avatars = avatars
        self.self_icon = avatar_icon(QPixmap(), "我")
        self.pending = False
        self.busy = False
        self.logged_in = False
        self.drafts = {}
        self.draft_talker = None
        self.live_kind = "idle"
        self.expanded = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        heading = QHBoxLayout()
        title = QLabel("私信")
        title.setObjectName("sectionTitle")
        heading.addWidget(title)
        self.connection_dot = QLabel()
        self.connection_dot.setFixedSize(10, 10)
        self.set_connection_indicator("idle", "未连接实时私信")
        heading.addWidget(self.connection_dot)
        heading.addStretch()
        self.expand = QPushButton("↗")
        self.expand.setObjectName("link")
        self.expand.setFixedWidth(36)
        self.expand.setAccessibleName("在独立窗口打开私信")
        self.expand.setToolTip("扩大私信，在独立窗口查看会话与消息")
        heading.addWidget(self.expand)
        self.refresh = QPushButton("刷新会话")
        self.more = QPushButton("更多会话")
        self.reconnect = QPushButton("恢复实时连接")
        heading.addWidget(self.refresh)
        heading.addWidget(self.more)
        heading.addWidget(self.reconnect)
        layout.addLayout(heading)
        self.live_status = QLabel("登录后连接实时私信；网络断线有界恢复，鉴权拒绝或风控停止。")
        self.live_status.setTextFormat(Qt.PlainText)
        self.live_status.setWordWrap(True)
        self.live_status.setObjectName("hint")
        layout.addWidget(self.live_status)
        self.hint = QLabel("通知驱动更新，不标记已读、不定时轮询。文字回复仍需确认收件人。")
        self.hint.setWordWrap(True)
        self.hint.setObjectName("hint")
        layout.addWidget(self.hint)
        self.participated = QCheckBox("仅最近 20 条有我发言")
        self.participated.setToolTip(
            "只判断已读取的最近页，不扫描完整聊天。未读取或尚待补增量的会话为“未判断”，"
            "开启筛选后暂不显示；取消筛选可查看并按需打开。"
        )
        self.participated.toggled.connect(self.filter_changed)
        layout.addWidget(self.participated)
        splitter = QSplitter(Qt.Horizontal)
        self.splitter = splitter
        self.sessions = QTreeWidget()
        self.sessions.setAccessibleName("私信会话列表")
        self.sessions.setHeaderLabels(["会话", "最近消息", "未读"])
        self.sessions.setRootIsDecorated(False)
        self.sessions.setIconSize(QSize(32, 32))
        self.sessions.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.sessions.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.sessions.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        splitter.addWidget(self.sessions)
        history = QWidget()
        self.message_pane = history
        history_layout = QVBoxLayout(history)
        history_layout.setContentsMargins(8, 0, 0, 0)
        self.title = QLabel("选择会话以读取消息")
        self.title.setTextFormat(Qt.PlainText)
        history_layout.addWidget(self.title)
        self.body = MessageList()
        self.message_delegate = MessageDelegate(self.body, images)
        self.body.setItemDelegate(self.message_delegate)
        self.body.setIconSize(QSize(32, 32))
        self.body.setWordWrap(True)
        self.body.setTextElideMode(Qt.ElideNone)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.body.setSpacing(6)
        self.body.setSelectionMode(QListWidget.ExtendedSelection)
        self.body.setAccessibleName("私信消息历史")
        history_layout.addWidget(self.body)
        actions = QHBoxLayout()
        self.refresh_thread = QPushButton("刷新消息")
        self.older = QPushButton("更早消息")
        actions.addWidget(self.refresh_thread)
        actions.addWidget(self.older)
        history_layout.addLayout(actions)
        self.reply = ReplyEditor()
        self.reply.setAccessibleName("私信回复内容")
        self.reply.setPlaceholderText("文字回复 · Enter 换行，⌘/Ctrl+Enter 发送")
        self.reply.setMaximumHeight(64)
        history_layout.addWidget(self.reply)
        send_row = QHBoxLayout()
        self.check_send = QPushButton("核对发送结果")
        self.send = QPushButton("发送")
        self.send.setObjectName("primary")
        send_row.addWidget(self.check_send)
        send_row.addWidget(self.send)
        history_layout.addLayout(send_row)
        self.send_notice = QLabel("只发给选中的单聊；不会自动重试发送。")
        self.send_notice.setWordWrap(True)
        self.send_notice.setTextFormat(Qt.PlainText)
        self.send_notice.setObjectName("hint")
        history_layout.addWidget(self.send_notice)
        splitter.addWidget(history)
        splitter.setSizes([320, 360])
        self.set_compact(False)
        layout.addWidget(splitter, 1)
        self.refresh.clicked.connect(self.refresh_requested)
        self.more.clicked.connect(self.more_requested)
        self.reconnect.clicked.connect(self.reconnect_requested)
        self.expand.clicked.connect(self.expand_requested)
        self.sessions.itemSelectionChanged.connect(self.selection_changed)
        self.refresh_thread.clicked.connect(lambda: self.open_requested.emit(self.selected(), True))
        self.older.clicked.connect(self.older_requested)
        self.send.clicked.connect(self.request_send)
        self.reply.submit_requested.connect(self.request_send)
        self.reply.textChanged.connect(self.update_controls)
        self.check_send.clicked.connect(self.reconcile_requested)
        self.set_busy(False, False)

    def set_compact(self, compact):
        # QSplitter derives its cross-axis maximum from its children.
        self.message_pane.setMaximumHeight(16777215 if self.expanded else 300 if compact else 440)
        self.splitter.setMinimumHeight(270 if compact else 340)

    def set_expanded(self, expanded):
        self.expanded = expanded
        self.expand.setText("↙" if expanded else "↗")
        self.expand.setAccessibleName("返回主窗口私信" if expanded else "在独立窗口打开私信")
        self.expand.setToolTip("返回主窗口" if expanded else "扩大私信，在独立窗口查看会话与消息")
        self.set_compact(not expanded and self.window().height() < 760)

    def selected(self):
        item = self.sessions.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def selection_changed(self):
        talker_id = self.selected()
        if self.draft_talker != talker_id:
            if self.draft_talker is not None:
                self.drafts[self.draft_talker] = self.reply.toPlainText()
            self.draft_talker = talker_id
            self.reply.setPlainText(self.drafts.get(talker_id, ""))
        if self.thread is None or self.thread.talker_id != talker_id:
            self.thread = None
            self.body.clear()
        self.refresh_thread.setEnabled(talker_id is not None)
        self.older.setEnabled(False)
        if talker_id is not None:
            self.title.setText("正在读取所选会话…")
            self.open_requested.emit(talker_id, False)
        else:
            self.title.setText(
                "群/系统会话暂不支持展开" if self.sessions.currentItem() else "选择会话以读取消息"
            )
        self.update_controls()

    def request_send(self):
        if self.send.isEnabled():
            self.send_requested.emit(self.selected(), self.reply.toPlainText())

    def set_busy(self, busy, logged_in):
        self.busy, self.logged_in = busy, logged_in
        self.update_controls()

    def update_controls(self):
        ready = self.logged_in and not self.busy
        editable = ready and not self.pending
        self.sessions.setEnabled(editable)
        self.participated.setEnabled(editable)
        self.refresh.setEnabled(editable)
        self.more.setEnabled(editable and self.view is not None and self.view.has_more)
        self.reconnect.setEnabled(ready and self.live_kind in {"idle", "stopped"})
        self.refresh_thread.setEnabled(editable and self.selected() is not None)
        self.older.setEnabled(editable and self.thread is not None and self.thread.has_more)
        self.reply.setReadOnly(not editable or self.thread is None)
        self.send.setEnabled(
            editable and self.thread is not None and bool(self.reply.toPlainText().strip())
        )
        self.check_send.setEnabled(ready and self.pending)

    def clear(self):
        self.participated.blockSignals(True)
        self.participated.setChecked(False)
        self.participated.blockSignals(False)
        self.sessions.blockSignals(True)
        self.sessions.clear()
        self.sessions.blockSignals(False)
        self.body.clear()
        self.thread, self.view, self.account_id = None, None, None
        self.pending = False
        self.drafts.clear()
        self.draft_talker = None
        self.reply.clear()
        self.send_notice.setText("只发给选中的单聊；不会自动重试发送。")
        self.title.setText("选择会话以读取消息")
        self.hint.setText("通知驱动更新，不标记已读、不定时轮询。文字回复仍需确认收件人。")
        self.live_kind = "idle"
        self.live_status.setText("登录后连接实时私信；网络断线有界恢复，鉴权拒绝或风控停止。")
        self.set_connection_indicator("idle", "未连接实时私信")
        self.set_busy(False, False)

    def set_connection_indicator(self, kind, detail):
        color = "green" if kind == "connected" else "yellow" if kind == "connecting" else "red"
        self.connection_dot.setStyleSheet(f"background: {COLORS[color]}; border-radius: 5px;")
        self.connection_dot.setToolTip(detail)
        self.connection_dot.setAccessibleName("实时私信连接状态：" + detail)

    def show_live_status(self, state):
        self.live_kind = state.kind
        self.set_connection_indicator(state.kind, state.detail)
        counts = (
            f" · 心跳 {state.heartbeats} 次 · 通知 {state.notifications} 次"
            if state.kind == "connected"
            else ""
        )
        self.live_status.setText(state.detail + counts)
        self.update_controls()

    def show_inbox(self, view, account_id):
        selected = self.selected()
        self.view, self.account_id = view, account_id
        self.sessions.blockSignals(True)
        self.sessions.clear()
        kept = False
        shown = 0
        for row in view.conversations:
            if self.participated.isChecked() and row.recent_participation is not True:
                continue
            session = row.session
            item = QTreeWidgetItem(
                [
                    row.name,
                    message_text(session.last_msg).replace("\n", " ")[:100],
                    str(session.unread_count) if session.unread_count else "",
                ]
            )
            participation = (
                "最近 20 条有我发言"
                if row.recent_participation is True
                else "最近 20 条未见我发言"
                if row.recent_participation is False
                else "最近页参与情况未判断"
            )
            item.setToolTip(
                0, f"UID {session.talker_id} · 会话类型 {session.session_type} · {participation}"
            )
            item.setData(0, Qt.UserRole + 1, row.avatar_url)
            item.setData(0, Qt.UserRole + 2, row.name)
            item.setIcon(0, self.avatars(row.avatar_url, row.name))
            if session.session_type == 1:
                item.setData(0, Qt.UserRole, session.talker_id)
            self.sessions.addTopLevelItem(item)
            shown += 1
            if session.session_type == 1 and session.talker_id == selected:
                self.sessions.setCurrentItem(item)
                kept = True
        self.sessions.blockSignals(False)
        if not kept:
            self.thread = None
            self.body.clear()
            if self.draft_talker is not None:
                self.drafts[self.draft_talker] = self.reply.toPlainText()
            self.draft_talker = None
            self.reply.clear()
            self.title.setText("选择会话以读取消息")
        self.hint.setText(
            ("本地缓存，尚待联网校验。 " if view.cached else "")
            + f"已读取 {len(view.conversations)} 个会话"
            + ("，可加载更多。" if view.has_more else "，已到列表末尾。")
            + " 查看不标记已读；未读数量为服务端值。"
            + (" 昵称暂不可用，部分会话显示 UID。" if not view.names_available else "")
        )
        if self.participated.isChecked():
            unknown = sum(row.recent_participation is None for row in view.conversations)
            self.hint.setText(
                f"已读取 {len(view.conversations)} 个会话 · 筛选显示 {shown} 个"
                f" · {unknown} 个未判断。\n"
                "只判断最近 20 条，不扫描完整历史；取消筛选可查看其他会话。"
            )
        self.update_controls()

    def filter_changed(self):
        if self.view is not None:
            self.show_inbox(self.view, self.account_id)

    def select_cached_thread(self, thread):
        self.sessions.blockSignals(True)
        for index in range(self.sessions.topLevelItemCount()):
            item = self.sessions.topLevelItem(index)
            if item.data(0, Qt.UserRole) == thread.talker_id:
                self.sessions.setCurrentItem(item)
                self.draft_talker = thread.talker_id
                break
        self.sessions.blockSignals(False)

    def show_messages(self, view):
        if view is None or view.talker_id != self.selected():
            return
        scrollbar = self.body.verticalScrollBar()
        position = scrollbar.value()
        at_bottom = position >= scrollbar.maximum() - 4
        older = bool(
            self.thread
            and self.thread.messages
            and view.messages
            and view.messages[0].msg_seqno < self.thread.messages[0].msg_seqno
        )
        first_load = self.thread is None
        self.thread = view
        name = self.sessions.currentItem().text(0)
        self.title.setText(
            f"{name} · {len(view.messages)} 条消息"
            + ("，可读取更早消息" if view.has_more else "")
            + (" · 本地缓存" if view.cached else "")
        )
        self.body.clear()
        peer_url = self.sessions.currentItem().data(0, Qt.UserRole + 1)
        for message in view.messages:
            try:
                stamp = (
                    datetime.fromtimestamp(message.timestamp).astimezone().strftime("%m-%d %H:%M")
                )
            except (ValueError, OverflowError, OSError):
                stamp = "时间未知"
            sender = "我" if message.sender_uid == self.account_id else name
            content = message_content(message)
            header = f"{stamp} · {sender}"
            item = QListWidgetItem(f"{header}\n{content.text}")
            item.setData(Qt.UserRole, message.sender_uid)
            item.setData(Qt.UserRole + 1, peer_url if message.sender_uid != self.account_id else "")
            item.setData(Qt.UserRole + 2, sender)
            item.setData(CONTENT_ROLE, content)
            item.setData(HEADER_ROLE, header)
            item.setIcon(
                self.self_icon
                if message.sender_uid == self.account_id
                else self.avatars(peer_url, sender)
            )
            self.body.addItem(item)
        if not view.messages:
            self.body.addItem("此会话暂无消息。")
        if first_load or (at_bottom and not older):
            self.body.scrollToBottom()
        else:
            scrollbar.setValue(position)

    def set_account(self, account_id, icon):
        self.account_id, self.self_icon = account_id, icon
        for index in range(self.body.count()):
            item = self.body.item(index)
            if item.data(Qt.UserRole) == account_id:
                item.setIcon(icon)

    def visible_artwork_urls(self):
        urls = []
        selected = self.sessions.currentItem()
        if selected and selected.data(0, Qt.UserRole + 1):
            urls.append(selected.data(0, Qt.UserRole + 1))
        for tree in (self.sessions, self.body):
            region = tree.viewport().visibleRegion()
            count = tree.topLevelItemCount() if tree is self.sessions else tree.count()
            for index in range(count):
                item = tree.topLevelItem(index) if tree is self.sessions else tree.item(index)
                url = (
                    item.data(0, Qt.UserRole + 1)
                    if tree is self.sessions
                    else item.data(Qt.UserRole + 1)
                )
                if url and region.intersects(tree.visualItemRect(item)) and url not in urls:
                    urls.append(url)
                if tree is self.body and region.intersects(tree.visualItemRect(item)):
                    content = item.data(CONTENT_ROLE)
                    for part in content.parts if content else ():
                        if isinstance(part, MessageImage) and part.url and part.url not in urls:
                            urls.append(part.url)
        return urls

    def update_artwork(self, urls):
        urls = {canonical_image_url(url) for url in urls}
        scrollbar = self.body.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 4
        resized = False
        for index in range(self.sessions.topLevelItemCount()):
            item = self.sessions.topLevelItem(index)
            url = item.data(0, Qt.UserRole + 1)
            if canonical_image_url(url) in urls:
                item.setIcon(0, self.avatars(url, item.text(0)))
        for index in range(self.body.count()):
            item = self.body.item(index)
            url = item.data(Qt.UserRole + 1)
            if canonical_image_url(url) in urls:
                item.setIcon(self.avatars(url, item.data(Qt.UserRole + 2)))
            content = item.data(CONTENT_ROLE)
            if content and any(
                isinstance(part, MessageImage) and canonical_image_url(part.url) in urls
                for part in content.parts
            ):
                self.message_delegate.sizeHintChanged.emit(self.body.indexFromItem(item))
                resized = True
        if resized:
            self.body.doItemsLayout()
            if at_bottom:
                self.body.scrollToBottom()
        self.body.viewport().update()

    def show_receipt(self, receipt):
        if receipt.request.talker_id != self.selected():
            return
        if receipt.history:
            self.show_messages(receipt.history)
            if receipt.history.messages:
                self.sessions.currentItem().setText(
                    1, message_text(receipt.history.messages[-1]).replace("\n", " ")[:100]
                )
        self.pending = not receipt.confirmed
        if receipt.confirmed and self.reply.toPlainText() == receipt.request.text:
            self.reply.clear()
            self.drafts.pop(receipt.request.talker_id, None)
        self.send_notice.setText(receipt.notice)
        self.update_controls()
