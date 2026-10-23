import asyncio
from functools import partial

import pytest
from broadcast_peer import BroadcastPeer, notice
from conftest import inbox_provider, private_message
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.http11 import Response

from bili_downloader.api import RequestGate
from bili_downloader.auth import Session
from bili_downloader.broadcast import (
    WATCH,
    BroadcastClient,
    _blob,
    _fields,
    _one,
    _wakeup,
)
from bili_downloader.sources import DownloadError


async def until(condition):
    async with asyncio.timeout(3):
        while not condition():  # noqa: ASYNC110 - bounded assertion wait on external-peer callbacks
            await asyncio.sleep(0.01)


def body_of(raw):
    return _one(_fields(_one(_fields(raw), 3)), 2, b"")


def test_current_official_nested_payload_and_private_routing():
    # Golden protobuf: uid=42, cmd=1, payload={notifyInfo:{talkerId:7,sessionType:1}},
    # payloadType=1. It is not Base64 despite that enum's historical label.
    golden = bytes.fromhex("082a10011a061a04100718012001")
    assert _wakeup(golden, 42).talkers == {7}
    for payload_type in (0, 1):
        hint = _wakeup(body_of(notice(payload_type=payload_type)), 42)
        assert hint.talkers == {7} and not hint.refresh_sessions
    assert _wakeup(body_of(notice(metadata=True)), 42).refresh_sessions
    for raw in (notice(account=99), notice(command=2), notice(payload_type=9)):
        with pytest.raises((ValueError, DownloadError)):
            _wakeup(body_of(raw), 42)
    for raw in (b"\x00", b"\x0a\x05bad", b"\x08" + b"\xff" * 11):
        with pytest.raises(ValueError):
            _fields(raw)


async def test_actual_transport_auth_subscribe_ack_and_heartbeats(monkeypatch):
    import bili_downloader.broadcast as module

    monkeypatch.setattr(module, "HEARTBEAT_SECONDS", 0.02)
    peer, states, hints = BroadcastPeer(), [], []
    client = BroadcastClient(
        Session(42, "Synthetic", "synthetic", "browser"),
        RequestGate(0, 0),
        connect_factory=peer.connect,
    )
    task = asyncio.create_task(client.run(hints.append, states.append))
    try:
        await until(lambda: states and states[-1].kind == "connected")
        await peer.incoming.put(notice(ack=True))
        await peer.incoming.put(notice(metadata=True))
        await until(lambda: len(hints) == 2 and states[-1].heartbeats >= 2)
        assert hints[0].talkers == {7} and hints[1].refresh_sessions
        assert peer.connections == 1
        acks = [item for item in peer.sent if item[0].endswith("/MessageAck")]
        assert len(acks) == 1
        ack = _fields(body_of(_blob(3, _one(acks[0][2], 3))))
        assert ack[1] == [88] and ack[3] == [WATCH.encode()]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert peer.closed


@pytest.mark.parametrize("failure", ["auth", "kickout", "malformed", "disconnect"])
async def test_transport_failure_closes_once_without_reconnecting(failure):
    peer, gate, states = (
        BroadcastPeer(auth_status=-412 if failure == "auth" else 0),
        RequestGate(0, 0),
        [],
    )
    task = asyncio.create_task(
        BroadcastClient(
            Session(42, "Synthetic", "synthetic", "browser"), gate, connect_factory=peer.connect
        ).run(lambda _: None, states.append)
    )
    if failure != "auth":
        await until(lambda: states and states[-1].kind == "connected")
        raw = {
            "kickout": notice(command=2),
            "malformed": b"\x00",
            "disconnect": ConnectionClosedError(None, None),
        }[failure]
        await peer.incoming.put(raw)
    with pytest.raises(DownloadError):
        await task
    assert peer.connections == 1 and peer.closed
    if failure == "auth":
        assert gate.stopped and len(peer.sent) == 1


@pytest.mark.parametrize("silent", [False, True])
async def test_missed_heartbeat_is_not_an_immediate_failure_but_full_silence_stops(
    monkeypatch, silent
):
    import bili_downloader.broadcast as module

    monkeypatch.setattr(module, "HEARTBEAT_SECONDS", 0.02)
    peer = BroadcastPeer(drop_heartbeats=range(1, 10) if silent else (2,))
    states = []
    task = asyncio.create_task(
        BroadcastClient(
            Session(42, "Synthetic", "synthetic", "browser"),
            RequestGate(0, 0),
            connect_factory=peer.connect,
        ).run(lambda _: None, states.append)
    )
    try:
        if silent:
            with pytest.raises(DownloadError, match="四个心跳周期"):
                await task
            assert peer.heartbeat_count == 4 and peer.closed
        else:
            await until(lambda: states and states[-1].heartbeats >= 3)
            assert peer.heartbeat_count >= 4 and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert peer.connections == 1


async def test_websocket_http_rejection_latches_gate_without_another_attempt():
    attempts, gate = [], RequestGate(0, 0)

    async def rejected(*args, **kwargs):
        attempts.append(1)
        raise InvalidStatus(Response(403, "Forbidden", Headers(), b"synthetic"))

    client = BroadcastClient(
        Session(42, "Synthetic", "synthetic", "browser"), gate, connect_factory=rejected
    )
    with pytest.raises(DownloadError, match="风控"):
        await client.run(lambda _: None, lambda _: None)
    assert attempts == [1] and gate.stopped


async def test_notification_coalescing_sdk_increment_and_risk_stop():
    provider, state = inbox_provider()
    peer, updates, statuses = BroadcastPeer(), [], []
    provider.broadcast_factory = partial(BroadcastClient, connect_factory=peer.connect)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42", user_agent="Synthetic Browser")
        api.start_inbox_live(
            lambda *args: updates.append(args), lambda *args: statuses.append(args)
        )
        await until(lambda: api.inbox.view.conversations)
        await api.inbox.open(7, 42)
        state["messages"].append(private_message(26))
        first = len(provider.requests)
        await peer.incoming.put(notice())
        await peer.incoming.put(notice(metadata=True))
        await until(lambda: api.inbox.high_water[7] == 26 and len(provider.requests) >= first + 3)
        reads = [r for r in provider.requests[first:] if r.url.path.endswith("fetch_session_msgs")]
        assert len(reads) == 1 and reads[0].url.params["begin_seqno"] == "25"
        assert api.inbox.thread.messages[-1].msg_seqno == 26
        assert all(r.method == "GET" for r in provider.requests)
        # A repeat hint returns no duplicate message and never moves the cursor backwards.
        await peer.incoming.put(notice())
        await until(
            lambda: sum(r.url.path.endswith("fetch_session_msgs") for r in provider.requests)
            == sum(r.url.path.endswith("fetch_session_msgs") for r in provider.requests[:first]) + 2
        )
        assert [row.msg_seqno for row in api.inbox.thread.messages].count(26) == 1
        state["risk"] = True
        await peer.incoming.put(notice())
        await until(lambda: api._live_task.done())
        count = len(provider.requests)
        api.start_inbox_live(lambda *args: None, lambda *args: None)
        await asyncio.sleep(0.05)
        assert len(provider.requests) == count and peer.connections == 1 and peer.closed
        assert statuses[-1][1].kind == "stopped" and api.gate.stopped


async def test_incremental_backlog_drains_before_committing_cursor_and_preserves_older_pages():
    provider, state = inbox_provider(21)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.inbox.refresh(42)
        await api.inbox.more(42)
        frontiers = api.inbox.frontiers.copy()
        await api.inbox.refresh_heads(42)
        assert len(api.inbox.view.conversations) == 22 and api.inbox.frontiers == frontiers
        await api.inbox.open(7, 42)
        await api.inbox.older(42)
        state["messages"].extend(private_message(n) for n in range(26, 71))
        first = len(provider.requests)
        assert await api.inbox.sync_talker(7, 42) == 45
        reads = provider.requests[first:]
        assert len(reads) == 3
        assert all(r.url.params["begin_seqno"] == "25" for r in reads)
        assert [row.msg_seqno for row in api.inbox.thread.messages] == list(range(1, 71))
        assert api.inbox.high_water[7] == 70 and not api.inbox.thread.has_more
        state["messages"].append(private_message(71, receiver_id=999))
        with pytest.raises(DownloadError, match="身份"):
            await api.inbox.sync_talker(7, 42)
        assert api.inbox.high_water[7] == 70


async def test_manual_reconnect_reconciles_gap_and_old_login_cannot_publish():
    provider, state = inbox_provider()
    peers, updates, statuses = [], [], []

    def factory(session, gate):
        peer = BroadcastPeer()
        peers.append(peer)
        return BroadcastClient(session, gate, connect_factory=peer.connect)

    provider.broadcast_factory = factory
    async with provider.api() as api:
        cookie = "SESSDATA=synthetic; DedeUserID=42"
        await api.use_cookie(cookie, user_agent="Synthetic Browser")
        api.start_inbox_live(
            lambda *args: updates.append(args), lambda *args: statuses.append(args)
        )
        await until(lambda: api.inbox.view.conversations)
        await api.inbox.open(7, 42)
        await peers[0].incoming.put(ConnectionClosedError(None, None))
        await until(lambda: api._live_task.done())
        assert len(peers) == 1 and peers[0].closed
        state["messages"].append(private_message(26))
        state["lanes"][1][0].update(max_seqno=26, last_msg=private_message(26))
        await api.stop_inbox_live()  # Explicit user action, not an automatic retry.
        api.start_inbox_live(
            lambda *args: updates.append(args), lambda *args: statuses.append(args)
        )
        await until(lambda: api.inbox.high_water[7] == 26)
        assert len(peers) == 2 and api.inbox.thread.messages[-1].msg_seqno == 26
        old_owner = api.session
        await api.use_cookie(cookie, user_agent="Synthetic Browser")
        count = len(updates)
        assert peers[-1].closed and not api.inbox.high_water
        await asyncio.sleep(0.05)
        assert len(updates) == count and api.session is not old_owner
