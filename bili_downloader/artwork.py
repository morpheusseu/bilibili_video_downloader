"""Bounded Qt image decoding and reusable, native circular avatars."""

from PyQt5.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt
from PyQt5.QtGui import QColor, QIcon, QImageReader, QPainter, QPainterPath, QPixmap

from .theme import COLORS


def decode_image(data, size=None):
    size = size or QSize(96, 96)
    buffer = QBuffer()
    buffer.setData(QByteArray(data))
    buffer.open(QIODevice.ReadOnly)
    reader = QImageReader(buffer)
    source_size = reader.size()
    if not source_size.isValid() or max(source_size.width(), source_size.height()) > 4096:
        return QPixmap()
    reader.setScaledSize(source_size.scaled(size, Qt.KeepAspectRatio))
    return QPixmap.fromImage(reader.read())


def avatar_icon(pixmap, name, size=32):
    canvas = QPixmap(size, size)
    canvas.fill(Qt.transparent)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    clip = QPainterPath()
    clip.addEllipse(0, 0, size, size)
    painter.setClipPath(clip)
    painter.fillRect(canvas.rect(), QColor(COLORS["soft"]))
    if not pixmap.isNull():
        source = pixmap.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        painter.drawPixmap((size - source.width()) // 2, (size - source.height()) // 2, source)
    else:
        font = painter.font()
        font.setPixelSize(size // 2)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(COLORS["blue"]))
        painter.drawText(canvas.rect(), Qt.AlignCenter, name[:1] or "B")
    painter.end()
    return QIcon(canvas)
