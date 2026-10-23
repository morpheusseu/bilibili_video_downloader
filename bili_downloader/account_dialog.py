"""Floating account controls; credentials still belong to the single API worker."""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class AccountDialog(QDialog):
    login_requested = pyqtSignal(bool)
    cookie_requested = pyqtSignal(str, bool)
    restore_requested = pyqtSignal()
    forget_requested = pyqtSignal()
    cancel_requested = pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("accountDialog")
        self.setWindowTitle("账号与登录")
        self.setWindowModality(Qt.WindowModal)
        self.setMinimumWidth(500)
        self.resize(560, 330)
        self.authorizing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(14)
        self.identity = QLabel("需要账号权限时，在 Chrome 中登录。")
        self.identity.setTextFormat(Qt.PlainText)
        self.identity.setWordWrap(True)
        self.identity.setObjectName("sectionTitle")
        layout.addWidget(self.identity)
        self.login = QPushButton("在 Chrome 中登录")
        self.login.setObjectName("primary")
        layout.addWidget(self.login)
        actions = QHBoxLayout()
        self.restore = QPushButton("使用已保存登录")
        self.forget = QPushButton("退出并忘记")
        actions.addWidget(self.restore)
        actions.addWidget(self.forget)
        layout.addLayout(actions)
        self.remember = QCheckBox("记住登录（本机保存，无需钥匙串，可取消）")
        self.remember.setChecked(True)
        layout.addWidget(self.remember)
        explanation = QLabel(
            "登录凭证与内容缓存按账号保存在当前用户的本机文件中，不使用系统钥匙串、"
            "不要求输入 Mac 密码。文件未加密，同一系统用户下的其他程序可能读取。"
            "使用独立 Chrome 临时会话；“退出并忘记”清除当前账号的凭证与缓存。"
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("hint")
        layout.addWidget(explanation)
        self.advanced = QCheckBox("手动使用 Cookie")
        layout.addWidget(self.advanced)
        self.cookie_panel = QWidget()
        cookie_row = QHBoxLayout(self.cookie_panel)
        cookie_row.setContentsMargins(0, 0, 0, 0)
        self.cookie = QLineEdit()
        self.cookie.setPlaceholderText("粘贴 Cookie · 隐藏显示；勾选记住登录时保存到本机")
        self.cookie.setAccessibleName("已有 Cookie")
        self.cookie.setEchoMode(QLineEdit.Password)
        self.import_cookie = QPushButton("使用 Cookie")
        cookie_row.addWidget(self.cookie)
        cookie_row.addWidget(self.import_cookie)
        layout.addWidget(self.cookie_panel)
        self.cookie_panel.hide()
        self.advanced.toggled.connect(self.cookie_panel.setVisible)
        self.status = QLabel("首次在 Chrome 中登录；之后可直接使用本机保存的登录。")
        self.status.setObjectName("hint")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.close_button = QPushButton("关闭")
        layout.addWidget(self.close_button)
        self.login.clicked.connect(lambda: self.login_requested.emit(self.remember.isChecked()))
        self.import_cookie.clicked.connect(self.submit_cookie)
        self.restore.clicked.connect(self.restore_requested)
        self.forget.clicked.connect(self.forget_requested)
        self.close_button.clicked.connect(self.reject)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)

    def submit_cookie(self):
        value = self.cookie.text()
        self.cookie.clear()
        self.cookie_requested.emit(value, self.remember.isChecked())

    def set_busy(self, busy, *, authorizing=False):
        self.authorizing = authorizing
        for widget in (
            self.login,
            self.restore,
            self.forget,
            self.remember,
            self.cookie,
            self.import_cookie,
            self.advanced,
        ):
            widget.setEnabled(not busy)
        self.close_button.setText("取消登录并关闭" if authorizing else "关闭")

    def reject(self):
        self.cookie.clear()
        if self.authorizing:
            self.cancel_requested.emit()
        super().reject()
