"""On-demand private messages, with one explicitly approved send pending at a time."""

import asyncio
import json
from dataclasses import dataclass, field, replace

from bpi.errors import (
    ApiError,
    HttpStatusError,
    InvalidParameterError,
    ResponseDecodeError,
    TransportError,
)
from bpi.message.models import PrivateMessage, Session

from .sources import DownloadError


def message_text(message: PrivateMessage | None) -> str:
    if message is None:
        return "暂无消息"
    # The provider can return withdrawn bodies. Never surface them, even in previews.
    if message.msg_status == 1 or message.msg_type == 5:
        return "[消息已撤回]"
    if message.msg_type == 2:
        return "[图片]"
    if message.msg_type != 1:
        return f"[暂不支持的消息类型 {message.msg_type}]"
    try:
        content = json.loads(message.content)
    except (ValueError, TypeError):
        return "[无法解析的文本消息]"
    text = content.get("content") if isinstance(content, dict) else None
    return text if isinstance(text, str) else "[无法解析的文本消息]"


@dataclass(frozen=True)
class Conversation:
    session: Session
    name: str
    avatar_url: str = ""


@dataclass(frozen=True)
class InboxView:
    conversations: tuple[Conversation, ...] = ()
    has_more: bool = False
    names_available: bool = True


@dataclass(frozen=True)
class MessageView:
    talker_id: int
    messages: tuple[PrivateMessage, ...] = ()
    has_more: bool = False


def validate_reply(text):
    if not isinstance(text, str) or not text.strip():
        raise DownloadError("回复内容不能为空")
    try:
        size = len(
            json.dumps({"content": text}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except UnicodeError:
        raise DownloadError("回复包含无效字符") from None
    if size > 2000:
        raise DownloadError("回复过长：编码后的消息须不超过 2000 字节")


@dataclass(frozen=True)
class PendingSend:
    account_id: int
    talker_id: int
    text: str = field(repr=False)
    after_seqno: int
    message_key: int = 0


@dataclass(frozen=True)
class SendReceipt:
    request: PendingSend = field(repr=False)
    message_key: int
    confirmed: bool
    history: MessageView | None = field(default=None, repr=False)

    @property
    def notice(self):
        if self.confirmed:
            return f"已找到对应新消息并回读确认（消息编号 {self.message_key}）。"
        if self.request.message_key:
            return f"服务器已受理（消息编号 {self.message_key}），暂未回读确认；请核对，勿重发。"
        return "发送结果未知；没有自动重试。请点击“核对发送结果”或到官方客户端确认，勿直接重发。"


class Inbox:
    def __init__(self, messages, users):
        self.messages, self.users = messages, users
        self.clear()

    def clear(self):
        self.view = InboxView()
        self.thread = None
        self.profiles = {}
        self.frontiers = {}
        self.pending = None

    async def refresh(self, account_id):
        return await self._sessions(account_id, {1: (None, True), 2: (None, True)}, previous=())

    async def more(self, account_id):
        if not self.view.has_more:
            return self.view
        return await self._sessions(account_id, self.frontiers, previous=self.view.conversations)

    async def _sessions(self, account_id, frontiers, *, previous):
        rows = {(row.session.session_type, row.session.talker_id): row.session for row in previous}
        next_frontiers = dict(frontiers)
        for category, (before, has_more) in frontiers.items():
            if not has_more:
                continue
            # ALL=4 ignores end_ts on the live provider. Use its two real categories.
            data = await self.messages.sessions(session_type=category, size=20, end_ts=before)
            page = data.session_list or []
            oldest = min((row.session_ts for row in page), default=None)
            if (data.has_more and not page) or (
                oldest is not None and (oldest <= 0 or (before is not None and oldest >= before))
            ):
                raise DownloadError("私信会话分页未推进，保留原列表；未将部分结果视为完整")
            for row in page:
                if row.session_type == 1 and row.last_msg:
                    self._validate_message(row.last_msg, account_id, row.talker_id)
                key = (row.session_type, row.talker_id)
                if key not in rows or row.session_ts > rows[key].session_ts:
                    rows[key] = row
            next_frontiers[category] = (oldest, bool(data.has_more))
        missing = sorted(
            {row.talker_id for row in rows.values() if row.session_type == 1 and row.talker_id > 0}
            - self.profiles.keys()
        )
        profiles = dict(self.profiles)
        names_available = True
        if missing:
            try:
                for start in range(0, len(missing), 100):
                    cards = await self.users.cards(mids=missing[start : start + 100])
                    profiles.update({card.mid: card for card in cards if card.mid in missing})
            except (ApiError, HttpStatusError, ResponseDecodeError, TransportError):
                names_available = (
                    False  # Names are labels, not a reason to hide valid conversations.
                )
        conversations = tuple(
            Conversation(
                row,
                profiles[row.talker_id].name
                if row.talker_id in profiles
                else f"UID {row.talker_id}",
                profiles[row.talker_id].face if row.talker_id in profiles else "",
            )
            for row in sorted(rows.values(), key=lambda row: row.session_ts, reverse=True)
        )
        self.profiles, self.frontiers = profiles, next_frontiers
        self.view = InboxView(
            conversations, any(more for _, more in next_frontiers.values()), names_available
        )
        return self.view

    async def open(self, talker_id, account_id, *, refresh=False):
        self.ensure_target(talker_id)
        if not refresh and self.thread and self.thread.talker_id == talker_id:
            return self.thread
        return await self._history(talker_id, account_id)

    def ensure_target(self, talker_id):
        if not any(
            row.session.talker_id == talker_id and row.session.session_type == 1
            for row in self.view.conversations
        ):
            raise DownloadError("请选择列表中的普通用户私信；群/系统会话暂不支持展开")

    async def send(self, talker_id, account_id, text):
        validate_reply(text)
        self.ensure_target(talker_id)
        if self.pending is not None:
            raise DownloadError("上次发送结果尚未核对，未再次发送")
        latest = await self._history(talker_id, account_id)
        self.pending = PendingSend(
            account_id, talker_id, text, max((row.msg_seqno for row in latest.messages), default=0)
        )
        try:
            result = await self.messages.send(receiver_id=talker_id, message=text, receiver_type=1)
            if result.msg_key and result.msg_key > 0:
                self.pending = replace(self.pending, message_key=result.msg_key)
        except (ApiError, InvalidParameterError):
            self.pending = None  # Explicit provider rejection, not an uncertain transport result.
            raise
        except asyncio.CancelledError:
            raise  # Retain the uncertainty fence even if the caller is interrupted.
        except Exception:
            pass  # No retry; only a read-back can resolve uncertain submission.
        return await self.reconcile(account_id)

    async def reconcile(self, account_id):
        pending = self.pending
        if pending is None or pending.account_id != account_id:
            raise DownloadError("当前账号没有待核对的发送")
        history = self.thread
        try:
            history = await self._history(pending.talker_id, account_id)
        except Exception:
            return SendReceipt(pending, pending.message_key, False, history)
        matches = [
            row
            for row in history.messages
            if row.sender_uid == account_id
            and row.msg_seqno > pending.after_seqno
            and row.msg_status == 0
            and row.msg_type == 1
            and (
                row.msg_key == pending.message_key
                if pending.message_key
                else message_text(row) == pending.text
            )
        ]
        if len(matches) == 1 and matches[0].msg_key > 0:
            self.pending = None
            return SendReceipt(pending, matches[0].msg_key, True, history)
        return SendReceipt(pending, pending.message_key, False, history)

    async def older(self, account_id):
        if self.thread is None or not self.thread.has_more:
            return self.thread
        return await self._history(self.thread.talker_id, account_id, previous=self.thread.messages)

    async def _history(self, talker_id, account_id, *, previous=()):
        before = min((row.msg_seqno for row in previous), default=None)
        data = await self.messages.session_messages(talker_id=talker_id, size=20, end_seqno=before)
        rows = data.messages or []
        for row in rows:
            self._validate_message(row, account_id, talker_id)
        older = [row for row in rows if before is None or row.msg_seqno < before]
        if (data.has_more and not older) or (before is not None and rows and not older):
            raise DownloadError("私信消息分页未推进，保留已读取的内容")
        merged = {row.msg_seqno: row for row in (*older, *previous)}
        self.thread = MessageView(
            talker_id, tuple(merged[key] for key in sorted(merged)), bool(data.has_more)
        )
        return self.thread

    @staticmethod
    def _validate_message(message, account_id, talker_id):
        if (
            message.receiver_type != 1
            or message.msg_seqno <= 0
            or {message.sender_uid, message.receiver_id} != {account_id, talker_id}
        ):
            raise DownloadError("私信响应身份或序列号不匹配，未展示该页")
