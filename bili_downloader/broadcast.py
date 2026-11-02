"""Official web Broadcast transport: authenticated routing hints, never message truth.

The current message-notify.js uses protobuf over WSS (not MQTT). Only Auth,
Subscribe, Heartbeat and transport MessageAck are sent. Message reads stay in
bpi-py; transport ACK is not a private-message read acknowledgement.
"""

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .sources import DownloadError

log = logging.getLogger(__name__)
URL = "wss://broadcast.chat.bilibili.com:7826/sub"
BASE = "/bilibili.broadcast.v1.Broadcast"
WATCH = "/bilibili.broadcast.message.im.Notify/WatchNotify"
HEARTBEAT_SECONDS = 20
CHECK_TIMEOUT_SECONDS = 10
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_BASE_SECONDS = 2
RECONNECT_MAX_SECONDS = 30
STABLE_CONNECTION_SECONDS = 120
RECOVERY_COOLDOWN_SECONDS = 30


class _ReceiveDeadline(Exception):
    pass


class _CheckRequested(Exception):
    pass


class _Reconnectable(DownloadError):
    """Only transport loss or missing liveness can authorize automatic recovery."""


@dataclass(frozen=True)
class Wakeup:
    talkers: frozenset[int] = frozenset()
    refresh_sessions: bool = False


@dataclass(frozen=True)
class BroadcastState:
    kind: str
    detail: str
    heartbeats: int = 0
    notifications: int = 0


def _varint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _number(field, value):
    return _varint(field << 3) + _varint(value)


def _blob(field, value):
    value = value.encode() if isinstance(value, str) else value
    return _varint(field << 3 | 2) + _varint(len(value)) + value


def _fields(data):
    """Bounded protobuf wire reader for the small, fixed official Broadcast schema."""
    if not isinstance(data, bytes) or len(data) > 1024 * 1024:
        raise ValueError("Invalid protobuf size")
    pos, result, count = 0, {}, 0

    def integer():
        nonlocal pos
        value = 0
        for shift in range(0, 70, 7):
            if pos >= len(data):
                raise ValueError("Truncated protobuf integer")
            part = data[pos]
            pos += 1
            value |= (part & 127) << shift
            if part < 128:
                if value >= 1 << 64:
                    raise ValueError("Oversized protobuf integer")
                return value
        raise ValueError("Oversized protobuf integer")

    while pos < len(data):
        key = integer()
        field, wire = key >> 3, key & 7
        if not 0 < field < 1 << 29:
            raise ValueError("Invalid protobuf field")
        if wire == 0:
            value = integer()
        elif wire in (1, 2, 5):
            length = integer() if wire == 2 else (8 if wire == 1 else 4)
            if length > len(data) - pos:
                raise ValueError("Truncated protobuf field")
            value = data[pos : pos + length]
            pos += length
        else:
            raise ValueError("Unsupported protobuf wire type")
        result.setdefault(field, []).append(value)
        count += 1
        if count > 2048:
            raise ValueError("Too many protobuf fields")
    return result


def _one(data, field, default=None):
    values = data.get(field, [])
    if len(values) > 1:
        raise ValueError("Duplicate singular protobuf field")
    return values[0] if values else default


def _wakeup(body, account_id):
    response = _fields(body)
    if _one(response, 1, 0) != account_id:
        raise DownloadError("Broadcast 通知账号不匹配，已停止实时收信")
    command = _one(response, 2, 0)
    if command == 2:
        raise DownloadError("服务端结束了当前 Broadcast 登录，已停止；请到官方客户端检查账号")
    if command != 1 or _one(response, 4, 0) not in (0, 1):
        raise ValueError("Unknown notification command or payload type")
    # Official schema: payload is nested ReqServerNotify even with payloadType=1.
    # Do not Base64-decode these wire bytes or log the optional instantMsg body.
    payload = _fields(_one(response, 3, b""))
    info = _fields(_one(payload, 3, b""))
    talkers, refresh = set(), False
    if _one(info, 3, 0) == 1 and _one(info, 2, 0) > 0:
        talkers.add(_one(info, 2))
    message = _fields(_one(payload, 2, b""))
    if _one(message, 2, 0) == 1:
        sender, receiver = _one(message, 1, 0), _one(message, 3, 0)
        if sender == account_id and receiver > 0:
            talkers.add(receiver)
        elif receiver == account_id and sender > 0:
            talkers.add(sender)
    for raw in payload.get(4, []):
        item = _fields(raw)
        for key in (1, 2, 3, 4):
            if key not in item:
                continue
            refresh |= key in (1, 2, 3)
            if key in (2, 4):
                wrapper = _fields(_one(item, key))
                session = _fields(_one(wrapper, 1, b""))
                private = _fields(_one(session, 1, b""))
                peer = _one(private, 1, 0)
                if peer > 0:
                    talkers.add(peer)
                else:
                    refresh = True  # A folder/system update cannot be read as a user thread.
    talkers.discard(account_id)
    return Wakeup(frozenset(talkers), refresh)


class _SingleConnect(connect):
    def process_redirect(self, exc):
        return exc  # Never follow redirects, rotate hosts or use reconnect iteration.


class BroadcastClient:
    def __init__(self, session, gate, *, connect_factory=_SingleConnect):
        self.session, self.gate, self.connect_factory = session, gate, connect_factory
        self._check_requested = asyncio.Event()
        self._failures = self._heartbeats = self._notifications = 0

    def request_check(self, reason):
        # Called on the owning asyncio loop. It never opens another socket or
        # interrupts backoff; concurrent foreground/resume hints coalesce.
        if not self.gate.stopped:
            log.info("Broadcast health check requested reason=%s", reason)
            self._check_requested.set()

    def _state(self, callback, kind, detail):
        callback(BroadcastState(kind, detail, self._heartbeats, self._notifications))

    async def run(self, on_wakeup, on_state):
        """One owner for liveness, bounded recovery and foreground/resume checks."""
        while True:
            attempt_started = time.monotonic()
            try:
                await self._connection(on_wakeup, on_state)
            except _Reconnectable as error:
                if self.gate.stopped:
                    raise DownloadError("请求已因风控停止，未自动重连") from None
                self._failures += 1
                if self._failures > MAX_RECONNECT_ATTEMPTS:
                    self._state(
                        on_state,
                        "stopped",
                        f"实时私信已断开；{MAX_RECONNECT_ATTEMPTS} 次自动重连未成功，已暂停。"
                        "切回前台、唤醒或点击恢复可再次检查。",
                    )
                    log.warning("Broadcast reconnect budget exhausted")
                    # Only a *new* external hint starts another bounded episode.
                    # Old checks during a failing attempt cannot create a retry loop.
                    self._check_requested.clear()
                    await self._check_requested.wait()
                    self._check_requested.clear()
                    delay = max(0, RECOVERY_COOLDOWN_SECONDS - (time.monotonic() - attempt_started))
                    self._failures = 0
                    detail = f"收到恢复检查，{delay:.1f} 秒后尝试重新连接实时私信。"
                else:
                    ceiling = min(
                        RECONNECT_MAX_SECONDS,
                        RECONNECT_BASE_SECONDS * 2 ** (self._failures - 1),
                    )
                    delay = random.uniform(ceiling / 2, ceiling)
                    detail = (
                        f"实时私信已断开，{delay:.1f} 秒后重连"
                        f"（第 {self._failures}/{MAX_RECONNECT_ATTEMPTS} 次）：{error}"
                    )
                self._state(on_state, "retry_wait", detail)
                log.info("Broadcast retry attempt=%s delay_s=%.3f", self._failures, delay)
                await asyncio.sleep(delay)

    async def _connection(self, on_wakeup, on_state):
        ws, sequence, subscribed = None, 1, False
        started, pending = time.monotonic(), {}

        def state(kind, detail):
            self._state(on_state, kind, detail)

        async def send(suffix, body, type_name):
            nonlocal sequence
            if self.gate.stopped:
                raise DownloadError("请求已因风控停止，未继续 Broadcast 操作")
            sequence += 1
            any_body = _blob(1, "type.googleapis.com/bilibili.broadcast.v1." + type_name) + _blob(
                2, body
            )
            packet = _blob(1, _number(2, sequence)) + _blob(2, BASE + suffix) + _blob(3, any_body)
            # Protocol keepalive must not queue behind a long media-transfer lock.
            # Data acquisition still uses the single shared, paced HTTP gate.
            await asyncio.wait_for(ws.send(packet), 5)
            return sequence

        async def receive(wait_seconds, *, check=False):
            if not check:
                try:
                    raw = await asyncio.wait_for(ws.recv(), wait_seconds)
                except TimeoutError:
                    raise _ReceiveDeadline from None
            else:
                reader = asyncio.create_task(ws.recv())
                checker = asyncio.create_task(self._check_requested.wait())
                try:
                    done, _ = await asyncio.wait(
                        (reader, checker), timeout=wait_seconds, return_when=asyncio.FIRST_COMPLETED
                    )
                    # Prioritize a received frame over a simultaneous check, so
                    # foreground activation can never drop a message notification.
                    if reader in done:
                        raw = reader.result()
                    elif checker in done:
                        self._check_requested.clear()
                        raise _CheckRequested
                    else:
                        raise _ReceiveDeadline
                finally:
                    for task in (reader, checker):
                        task.cancel()
                    await asyncio.gather(reader, checker, return_exceptions=True)
            data = _fields(raw)
            options = _fields(_one(data, 1, b""))
            status = _fields(_one(options, 4, b""))
            code = _one(status, 1, 0)
            if code >= 1 << 63:
                code -= 1 << 64
            elif code >= 1 << 31:
                code -= 1 << 32
            if code:
                self.gate.check(200, code)
                raise DownloadError(f"Broadcast 服务端拒绝（API {code}），未自动重试")
            path = _one(data, 2, b"").decode("ascii")
            if len(path) > 200 or not path.startswith("/bilibili."):
                raise ValueError("Unknown Broadcast target")
            if path == WATCH:
                envelope = _fields(_one(data, 3, b""))
                hint = _wakeup(_one(envelope, 2, b""), self.session.mid)
                self._notifications += 1
                log.info(
                    "Broadcast notification targets=%s refresh=%s",
                    len(hint.talkers),
                    hint.refresh_sessions,
                )
                on_wakeup(hint)
                if subscribed:
                    state("connected", "已收到私信更新通知")
            if _one(options, 3, 0):
                await send(
                    "/MessageAck",
                    _number(1, _one(options, 1, 0))
                    + _blob(2, _one(options, 5, b""))
                    + _blob(3, path)
                    + _number(4, _one(options, 7, 0)),
                    "MessageAckReq",
                )
            return path, _one(options, 2, 0)

        async def confirm(suffix, expected):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                path, received = await receive(max(0.01, deadline - time.monotonic()))
                if path == BASE + suffix and received == expected:
                    return
            raise TimeoutError

        try:
            state("connecting", "正在连接实时私信…")
            async with self.gate.request():
                ws = await self.connect_factory(
                    URL,
                    origin="https://message.bilibili.com",
                    subprotocols=["proto"],
                    additional_headers={
                        "Cookie": self.session.cookie,
                        "Referer": "https://message.bilibili.com/",
                    },
                    user_agent_header=self.session.user_agent,
                    proxy=None,
                    compression=None,
                    open_timeout=15,
                    close_timeout=3,
                    ping_interval=None,
                    max_size=1024 * 1024,
                    max_queue=16,
                )
            if ws.subprotocol != "proto":
                raise ValueError("Protobuf subprotocol not negotiated")
            log.info("Broadcast transport connected HTTP=101")
            await confirm("/Auth", await send("/Auth", _blob(1, str(uuid.uuid4())), "AuthReq"))
            await confirm("/Subscribe", await send("/Subscribe", _blob(1, WATCH), "TargetPath"))
            log.info("Broadcast subscribed")
            subscribed = True
            state("connected", "实时私信已连接")
            connected_at = last_received = time.monotonic()
            silent_ticks, probe = 0, None
            while True:
                deadline = last_received + (silent_ticks + 1) * HEARTBEAT_SECONDS
                if probe is not None:
                    deadline = min(deadline, probe[1])
                timeout = max(0.01, deadline - time.monotonic())
                try:
                    path, received = await receive(timeout, check=True)
                except _CheckRequested:
                    if probe is None:
                        sent = await send("/Heartbeat", b"", "HeartbeatResp")
                        pending[sent] = time.monotonic()
                        probe = (sent, pending[sent] + CHECK_TIMEOUT_SECONDS)
                        log.info("Broadcast health probe sent")
                    continue
                except _ReceiveDeadline:
                    if probe is not None and time.monotonic() >= probe[1]:
                        raise _Reconnectable("连接检查未收到心跳响应") from None
                    sent = await send("/Heartbeat", b"", "HeartbeatResp")
                    pending[sent] = time.monotonic()
                    while len(pending) > 64:
                        pending.pop(next(iter(pending)))
                    silent_ticks += 1
                    if silent_ticks >= 4:
                        raise _Reconnectable("Broadcast 连续四个心跳周期无响应") from None
                    continue
                last_received, silent_ticks = time.monotonic(), 0
                if path == BASE + "/Heartbeat":
                    sent_at = pending.pop(received, None)
                    if sent_at is not None:
                        self._heartbeats += 1
                        log.info(
                            "Broadcast heartbeat=%s latency_ms=%s",
                            self._heartbeats,
                            round((last_received - sent_at) * 1000),
                        )
                        if probe is not None and received == probe[0]:
                            probe = None
                            log.info("Broadcast health probe confirmed")
                        if (
                            self._failures
                            and last_received - connected_at >= STABLE_CONNECTION_SECONDS
                        ):
                            self._failures = 0
                            log.info("Broadcast stable connection reset retry budget")
                        state("connected", "实时私信心跳正常")
        except InvalidStatus as error:
            code = error.response.status_code
            self.gate.check(code)
            raise DownloadError(f"Broadcast 连接被拒绝（HTTP {code}），未自动重试") from None
        except ConnectionClosed as error:
            code = error.rcvd.code if error.rcvd else 1006
            cause = type(error.__cause__).__name__ if error.__cause__ else "unknown"
            log.warning(
                "Broadcast closed code=%s received_close=%s cause=%s uptime=%s",
                code,
                error.rcvd is not None,
                cause,
                round(time.monotonic() - started),
            )
            error_type = (
                _Reconnectable if code in {1000, 1001, 1006, 1011, 1012, 1014} else DownloadError
            )
            raise error_type(f"实时私信连接已断开（{code}）") from None
        except (TimeoutError, _ReceiveDeadline, OSError):
            raise _Reconnectable("Broadcast 网络连接或响应超时") from None
        except (ValueError, TypeError, UnicodeError):
            raise DownloadError("Broadcast 响应格式不匹配，已停止实时收信；未自动重试") from None
        finally:
            if ws is not None:
                await ws.close()
            log.info(
                "Broadcast connection ended heartbeats=%s notifications=%s uptime=%s",
                self._heartbeats,
                self._notifications,
                round(time.monotonic() - started),
            )
