"""Synthetic external socket peer; real Broadcast code still encodes/decodes frames."""

import asyncio

from bilibili_client.broadcast import BASE, URL, WATCH, _blob, _fields, _number, _one


def server_frame(path, sequence=0, *, body=b"", status=0, ack=False):
    options = _number(2, sequence)
    if status:
        options += _blob(4, _number(1, status if status >= 0 else status + (1 << 64)))
    if ack:
        options += _number(1, 88) + _number(3, 1) + _blob(5, "synthetic") + _number(7, 1)
    return (
        _blob(1, options)
        + _blob(2, path)
        + _blob(
            3,
            _blob(1, "type.googleapis.com/bilibili.broadcast.message.im.NotifyRsp")
            + _blob(2, body),
        )
    )


def notice(talker=7, *, metadata=False, account=42, command=1, payload_type=1, ack=False):
    if metadata:
        payload = _blob(4, _blob(2, b"")) + _blob(4, _blob(1, b""))
    else:
        session_id = _blob(1, _number(1, talker))
        payload = _blob(3, _number(1, 1) + _number(2, talker) + _number(3, 1)) + _blob(
            4, _blob(4, _blob(1, session_id))
        )
    body = _number(1, account) + _number(2, command) + _blob(3, payload) + _number(4, payload_type)
    return server_frame(WATCH, body=body, ack=ack)


class BroadcastPeer:
    def __init__(self, *, auth_status=0, drop_heartbeats=()):
        self.auth_status = auth_status
        self.connections = 0
        self.sent = []
        self.closed = False
        self.subprotocol = "proto"
        self.incoming = None
        self.drop_heartbeats = set(drop_heartbeats)
        self.heartbeat_count = 0

    async def connect(self, url, **kwargs):
        assert url == URL and kwargs["subprotocols"] == ["proto"]
        assert kwargs["origin"] == "https://message.bilibili.com"
        assert kwargs["ping_interval"] is None
        self.connections += 1
        self.closed = False
        self.incoming = asyncio.Queue()
        return self

    async def send(self, raw):
        data = _fields(raw)
        options = _fields(_one(data, 1, b""))
        path = _one(data, 2).decode()
        sequence = _one(options, 2)
        self.sent.append((path, sequence, data))
        assert path in {
            BASE + suffix for suffix in ("/Auth", "/Subscribe", "/Heartbeat", "/MessageAck")
        }
        if path.endswith("/Heartbeat"):
            self.heartbeat_count += 1
            if self.heartbeat_count in self.drop_heartbeats:
                return
        if not path.endswith("/MessageAck"):
            await self.incoming.put(
                server_frame(
                    path, sequence, status=self.auth_status if path.endswith("/Auth") else 0
                )
            )

    async def recv(self):
        item = await self.incoming.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self):
        self.closed = True
