"""Native message rows: escaped text and explicitly supplied in-memory images."""

import hashlib
from html import escape

from bilibili_client.message_content import MessageImage
from PyQt5.QtCore import QRect, QSize, Qt, QUrl
from PyQt5.QtGui import QIcon, QTextDocument
from PyQt5.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from .theme import COLORS

CONTENT_ROLE = Qt.UserRole + 3
HEADER_ROLE = Qt.UserRole + 4


class MessageDelegate(QStyledItemDelegate):
    def __init__(self, parent, images):
        super().__init__(parent)
        self.images = images

    def document(self, index, width, *, selected=False):
        document = QTextDocument()
        document.setDefaultFont(self.parent().font())
        document.setDocumentMargin(2)
        content = index.data(CONTENT_ROLE)
        if content is None:
            parts = (index.data(Qt.DisplayRole) or "",)
            header = ""
        else:
            parts, header = content.parts, index.data(HEADER_ROLE) or ""
        fragments = []
        for part in parts:
            if isinstance(part, str):
                fragments.append(escape(part).replace("\n", "<br>"))
                continue
            pixmap = self.images(part.url)
            if not isinstance(part, MessageImage) or pixmap.isNull():
                fragments.append(escape(part.alt))
                continue
            # Never put a provider URL into HTML; only registered memory resources.
            source = "bili-image:" + hashlib.sha256(part.url.encode()).hexdigest()
            document.addResource(QTextDocument.ImageResource, QUrl(source), pixmap)
            size = pixmap.size().scaled(QSize(part.width, part.height), Qt.KeepAspectRatio)
            fragments.append(
                f'<img src="{source}" width="{size.width()}" height="{size.height()}" '
                f'alt="{escape(part.alt, quote=True)}">'
            )
        color = COLORS["white"] if selected else COLORS["ink"]
        muted = COLORS["white"] if selected else COLORS["muted"]
        document.setHtml(
            f'<div style="color:{muted};font-size:11px">{escape(header)}</div>'
            + f'<div style="color:{color};white-space:pre-wrap">'
            + "".join(fragments)
            + "</div>"
        )
        document.setTextWidth(max(80, width))
        return document

    def sizeHint(self, option, index):
        width = max(80, self.parent().viewport().width() - 54)
        document = self.document(index, width)
        return QSize(width + 54, max(44, int(document.size().height()) + 10))

    def paint(self, painter, option, index):
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        style = self.parent().style()
        rect = style.subElementRect(QStyle.SE_ItemViewItemText, styled, self.parent())
        # QStyle centers the old plain-text block vertically. Rich content needs
        # the complete row height, otherwise a tall inline image gets clipped.
        rect.setTop(styled.rect.top())
        rect.setBottom(styled.rect.bottom())
        icon = QIcon(styled.icon)
        styled.icon = QIcon()
        styled.text = ""
        painter.save()
        style.drawControl(QStyle.CE_ItemViewItem, styled, painter, self.parent())
        painter.restore()
        icon.paint(
            painter,
            QRect(styled.rect.left() + 6, styled.rect.top() + 6, 32, 32),
            Qt.AlignTop | Qt.AlignHCenter,
        )
        document = self.document(
            index, rect.width(), selected=bool(styled.state & QStyle.State_Selected)
        )
        painter.save()
        painter.setClipRect(rect)
        painter.translate(rect.left(), rect.top() + 4)
        document.drawContents(painter)
        painter.restore()
