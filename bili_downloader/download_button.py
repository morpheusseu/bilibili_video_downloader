"""One accessible download-tray button: disclosure, current-file progress, and unread results."""

from PyQt5.QtCore import QRectF, Qt
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QPushButton

from .theme import COLORS


class DownloadButton(QPushButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("downloadDetails")
        self.setFixedSize(56, 56)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.active = False
        self.percent = None
        self.new_count = 0
        self.toggled.connect(self.disclosed)
        self.refresh()

    def disclosed(self, expanded):
        if expanded:
            self.new_count = 0
        self.refresh()

    def set_progress(self, active, percent=None):
        self.active = active
        self.percent = percent if active else None
        self.refresh()

    def completed(self):
        self.new_count += 1
        self.refresh()

    def refresh(self):
        parts = ["收起下载详情" if self.isChecked() else "展开下载详情"]
        if self.active:
            parts.append(
                f"当前文件约 {self.percent}%（按媒体流分阶段估算）"
                if self.percent is not None
                else "正在处理，当前进度未知"
            )
        if self.new_count:
            parts.append(f"新完成 {self.new_count} 个文件")
        text = " · ".join(parts)
        self.setAccessibleName(text)
        self.setToolTip(text)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(COLORS["focus" if self.hasFocus() else "hairline"]), 1))
        painter.setBrush(QColor(COLORS["soft" if self.isChecked() or self.isDown() else "white"]))
        painter.drawEllipse(QRectF(7, 7, 42, 42))
        if self.active:
            ring = QRectF(3, 3, 50, 50)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(COLORS["hairline"]), 2.5))
            painter.drawEllipse(ring)
            pen = QPen(QColor(COLORS["blue"]), 2.5, Qt.SolidLine, Qt.RoundCap)
            if self.percent is None:
                pen.setStyle(Qt.DotLine)
            painter.setPen(pen)
            painter.drawArc(
                ring,
                90 * 16,
                -int(360 * 16 * (self.percent or 0) / 100)
                if self.percent is not None
                else -90 * 16,
            )
        painter.setPen(QPen(QColor(COLORS["blue"]), 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        arrow = QPainterPath()
        arrow.moveTo(28, 17)
        arrow.lineTo(28, 32)
        arrow.moveTo(22, 26)
        arrow.lineTo(28, 32)
        arrow.lineTo(34, 26)
        arrow.moveTo(21, 37)
        arrow.lineTo(35, 37)
        painter.drawPath(arrow)
        if self.new_count:
            badge = QRectF(
                35 if self.new_count < 100 else 29, 0, 20 if self.new_count < 100 else 27, 20
            )
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLORS["red"]))
            painter.drawRoundedRect(badge, 10, 10)
            font = painter.font()
            font.setPixelSize(11)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor(COLORS["white"]))
            painter.drawText(
                badge, Qt.AlignCenter, str(self.new_count) if self.new_count < 100 else "99+"
            )
