"""On-demand private messages, with one explicitly approved send pending at a time."""

import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass, field, replace

from bpi.errors import (
    ApiError,
    HttpStatusError,
    InvalidParameterError,
    ResponseDecodeError,
    TransportError,
)
from bpi.message.models import PrivateMessage, Session

from .cache import MAX_MESSAGES, MAX_THREADS
from .message_content import message_content
from .provider_views import ReceivedMessage, message_page
from .sources import DownloadError


def message_text(message: PrivateMessage | None) -> str:
    return message_content(message).text


@dataclass(frozen=True)
class Conversation:
    session: Session
    name: str
    avatar_url: str = ""
    recent_participation: bool | None = None


@dataclass(frozen=True)
class InboxView:
    conversations: tuple[Conversation, ...] = ()
    has_more: bool = False
    names_available: bool = True
    cached: bool = False


@dataclass(frozen=True)
class MessageView:
    talker_id: int
    messages: tuple[PrivateMessage, ...] = ()
    has_more: bool = False
    cached: bool = False


@dataclass(frozen=True)
class Profile:
    name: str
    face: str = ""


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
    def __init__(self, sdk, *, cache=None):
        self.sdk, self.messages, self.users = sdk, sdk.message, sdk.user
        self.cache = cache
        self._lock = asyncio.Lock()
        self.clear()
        if cache is not None:
            self._restore()

    def clear(self):
        self.account_id = self.cache.account_id if self.cache else None
        self.view = InboxView()
        self.thread = None
        self.profiles = {}
        self.frontiers = {}
        self.pending = None
        self.high_water = {}
        self.threads = OrderedDict()

    def _restore(self):
        try:
            header = self.cache.get("inbox", "head")
            conversations = []
            if header is not None:
                for item in header:
                    session = Session.model_validate(item["session"])
                    if session.session_type == 1 and session.last_msg:
                        self._validate_message(
                            session.last_msg, self.cache.account_id, session.talker_id
                        )
                    profile = Profile(item["name"], item["avatar_url"])
                    if not isinstance(profile.name, str) or not isinstance(profile.face, str):
                        raise ValueError("Invalid cached profile")
                    self.profiles[session.talker_id] = profile
                    conversations.append(Conversation(session, profile.name, profile.face))
                self.view = InboxView(tuple(conversations), True, True, True)
                self.frontiers = {1: (None, True), 2: (None, True)}
            for peer, record in self.cache.threads():
                rows = tuple(ReceivedMessage.model_validate(row) for row in record["messages"])
                for row in rows:
                    self._validate_message(row, self.cache.account_id, peer)
                if (
                    not rows
                    or len(rows) > MAX_MESSAGES
                    or record["high_water"] != max(row.msg_seqno for row in rows)
                ):
                    raise ValueError("Cursor without cached messages")
                self.threads[peer] = MessageView(peer, rows, bool(record["has_more"]), True)
                self.high_water[peer] = record["high_water"]
            selected = self.cache.get("selected", "last")
            if selected in {
                row.session.talker_id for row in conversations if row.session.session_type == 1
            }:
                self.thread = self.threads.get(selected)
            self._update_participation()
        except DownloadError:
            raise
        except Exception:
            raise DownloadError("消息缓存结构无效，未恢复其正文或游标") from None

    @staticmethod
    def _message_record(message):
        value = message.model_dump(mode="json")
        if message.msg_status or message.msg_type == 5:
            value["content"] = ""
            value.pop("emojis", None)
        return value

    def _save_header(self, view):
        if self.cache is not None:
            header = []
            for row in view.conversations[:100]:
                session = row.session.model_dump(mode="json")
                if row.session.last_msg is not None:
                    session["last_msg"] = self._message_record(row.session.last_msg)
                header.append({"session": session, "name": row.name, "avatar_url": row.avatar_url})
            self.cache.put_many({("inbox", "head"): header})

    def _commit_thread(self, view, *, select=False):
        watermark = max((row.msg_seqno for row in view.messages), default=0)
        if self.cache is not None:
            values = {}
            if watermark:
                values[("thread", str(view.talker_id))] = {
                    "messages": [
                        self._message_record(row) for row in view.messages[-MAX_MESSAGES:]
                    ],
                    "high_water": watermark,
                    "has_more": view.has_more or len(view.messages) > MAX_MESSAGES,
                }
            if select:
                values[("selected", "last")] = view.talker_id
            self.cache.put_many(values)
        self.threads[view.talker_id] = view
        self.threads.move_to_end(view.talker_id)
        if watermark:
            self.high_water[view.talker_id] = watermark
        while len(self.threads) > MAX_THREADS:
            peer, _ = self.threads.popitem(last=False)
            self.high_water.pop(peer, None)
        if select or (self.thread and self.thread.talker_id == view.talker_id):
            self.thread = view
        self._update_participation()

    def _update_participation(self):
        account_id = self.account_id
        if account_id is None:
            return
        rows = tuple(
            replace(row, recent_participation=self._participated(row, account_id))
            for row in self.view.conversations
        )
        if rows != self.view.conversations:
            self.view = replace(self.view, conversations=rows)

    @staticmethod
    def _authored(message, account_id):
        return (
            message is not None
            and message.sender_uid == account_id
            and not message.msg_status
            and message.msg_type not in {5, 10, 11, 18, 52}
        )

    def _participated(self, row, account_id):
        session = row.session
        if session.session_type != 1:
            return None
        if self._authored(session.last_msg, account_id):
            return True
        thread = self.threads.get(session.talker_id)
        if thread is None or self.high_water.get(session.talker_id, 0) < session.max_seqno:
            return None
        sample = thread.messages[-20:]
        if any(self._authored(message, account_id) for message in sample):
            return True
        return False if len(sample) == 20 or not thread.has_more else None

    async def refresh(self, account_id):
        async with self._lock:
            return await self._sessions(account_id, {1: (None, True), 2: (None, True)}, previous=())

    async def more(self, account_id):
        async with self._lock:
            if not self.view.has_more:
                return self.view
            return await self._sessions(
                account_id, self.frontiers, previous=self.view.conversations
            )

    async def refresh_heads(self, account_id):
        """Notification-driven list refresh without discarding already loaded pages."""
        async with self._lock:
            frontiers = self.frontiers.copy()
            await self._sessions(
                account_id,
                {1: (None, True), 2: (None, True)},
                previous=self.view.conversations,
            )
            self.frontiers.update(frontiers)
            self.view = replace(
                self.view, has_more=any(more for _, more in self.frontiers.values())
            )
            return self.view

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
                # Unread/ack state can change without a newer message timestamp.
                if key not in rows or row.session_ts >= rows[key].session_ts:
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
                    profiles.update(
                        {
                            card.mid: Profile(card.name, card.face)
                            for card in cards
                            if card.mid in missing
                        }
                    )
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
        view = InboxView(
            conversations, any(more for _, more in next_frontiers.values()), names_available
        )
        self._save_header(view)
        self.account_id = account_id
        self.profiles, self.frontiers, self.view = profiles, next_frontiers, view
        self._update_participation()
        return self.view

    async def open(self, talker_id, account_id, *, refresh=False):
        async with self._lock:
            self.ensure_target(talker_id)
            if not refresh and talker_id in self.threads:
                if self.cache is not None:
                    self.cache.put_many({("selected", "last"): talker_id})
                self.thread = self.threads[talker_id]
                return self.thread
            return await self._history(talker_id, account_id)

    def ensure_target(self, talker_id):
        if not any(
            row.session.talker_id == talker_id and row.session.session_type == 1
            for row in self.view.conversations
        ):
            raise DownloadError("请选择列表中的普通用户私信；群/系统会话暂不支持展开")

    async def send(self, talker_id, account_id, text):
        async with self._lock:
            return await self._send(talker_id, account_id, text)

    async def _send(self, talker_id, account_id, text):
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
        return await self._reconcile(account_id)

    async def reconcile(self, account_id):
        async with self._lock:
            return await self._reconcile(account_id)

    async def _reconcile(self, account_id):
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
        async with self._lock:
            if self.thread is None or not self.thread.has_more:
                return self.thread
            return await self._history(
                self.thread.talker_id, account_id, previous=self.thread.messages
            )

    async def sync_talker(self, talker_id, account_id):
        """Read only the notified user thread; commit the cursor after all delta pages.

        First contact bootstraps one recent page, not the entire private history.
        Subsequent notifications drain messages above the last successful sequence.
        """
        async with self._lock:
            return await self._sync_talker(talker_id, account_id)

    async def _sync_talker(self, talker_id, account_id):
        self.ensure_target(talker_id)
        baseline, end, collected = self.high_water.get(talker_id), None, {}
        data = None
        for _ in range(25):  # Drain at most 500 new messages; never advance across an unfilled gap.
            data = await message_page(
                self.sdk, talker_id=talker_id, size=20, begin_seqno=baseline, end_seqno=end
            )
            rows = data.messages or []
            for row in rows:
                self._validate_message(row, account_id, talker_id)
                if baseline is not None and row.msg_seqno < baseline:
                    raise DownloadError("私信增量接口未遵守序号边界，游标未推进")
            lowest = min((row.msg_seqno for row in rows), default=None)
            if data.has_more and (lowest is None or (end is not None and lowest >= end)):
                raise DownloadError("私信增量分页未推进，原有游标保留")
            collected.update(
                {row.msg_seqno: row for row in rows if baseline is None or row.msg_seqno > baseline}
            )
            if baseline is None or not data.has_more or (lowest is not None and lowest <= baseline):
                break
            end = lowest
        else:
            raise DownloadError("本次私信增量超过 500 条，已暂停；原有游标保留，请手动检查")
        if collected:
            previous = self.threads.get(talker_id)
            merged = {row.msg_seqno: row for row in previous.messages} if previous else {}
            merged.update(collected)
            view = MessageView(
                talker_id,
                tuple(merged[key] for key in sorted(merged)),
                previous.has_more if previous else bool(data.has_more),
            )
            self._commit_thread(view)
        return len(collected)

    async def _history(self, talker_id, account_id, *, previous=()):
        # A refresh/send preflight must not jump over a restart gap by replacing
        # a saved cursor with the latest page's maximum. Drain forward first.
        if not previous and talker_id in self.high_water:
            await self._sync_talker(talker_id, account_id)
        before = min((row.msg_seqno for row in previous), default=None)
        data = await message_page(self.sdk, talker_id=talker_id, size=20, end_seqno=before)
        rows = data.messages or []
        for row in rows:
            self._validate_message(row, account_id, talker_id)
        older = [row for row in rows if before is None or row.msg_seqno < before]
        if (data.has_more and not older) or (before is not None and rows and not older):
            raise DownloadError("私信消息分页未推进，保留已读取的内容")
        cached = self.threads.get(talker_id)
        existing = previous or (cached.messages if cached else ())
        merged = {row.msg_seqno: row for row in (*existing, *older)}
        view = MessageView(
            talker_id, tuple(merged[key] for key in sorted(merged)), bool(data.has_more)
        )
        self._commit_thread(view, select=True)
        return self.thread

    @staticmethod
    def _validate_message(message, account_id, talker_id):
        if (
            message.receiver_type != 1
            or message.msg_seqno <= 0
            or {message.sender_uid, message.receiver_id} != {account_id, talker_id}
        ):
            raise DownloadError("私信响应身份或序列号不匹配，未展示该页")
