"""Synthetic provider responses; tests never load credentials or call Bilibili."""

import base64
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from bili_downloader.api import Bilibili, RequestGate
from bili_downloader.download import ffmpeg_executable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
BV = "BV178411i7AD"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAACXBIWXMAAA9hAAAPYQGoP6dp"
    "AAAAFElEQVQImWNkSDvzn4GBgYGJAQoAIHwCNeos0L0AAAAASUVORK5CYII="
)


def video(bvid=BV, pages=1):
    return {
        "aid": 233802576,
        "bvid": bvid,
        "videos": pages,
        "title": "测试 / video",
        "pic": "",
        "owner": {"mid": 42, "name": "Synthetic", "face": ""},
        "stat": dict.fromkeys(["aid", "view", "danmaku", "reply", "coin", "share", "like"], 0),
        "cid": 100,
        "pages": [
            {"cid": 100 + i, "page": i + 1, "part": f"part {i + 1}", "duration": 1}
            for i in range(pages)
        ],
    }


def folder_info(count=1):
    return {
        "id": 10,
        "fid": 10,
        "mid": 42,
        "attr": 0,
        "title": "Piano",
        "cover": "",
        "upper": {"mid": 42, "name": "Synthetic", "face": ""},
        "cover_type": 0,
        "cnt_info": {"collect": 0, "play": 0},
        "type": 11,
        "intro": "",
        "ctime": 0,
        "mtime": 0,
        "state": 0,
        "fav_state": 0,
        "like_state": 0,
        "media_count": count,
    }


def favorite_item(bvid=BV, id=1):
    return {
        "id": id,
        "type": 2,
        "title": "Synthetic video",
        "cover": "",
        "intro": "",
        "duration": 1,
        "upper": {"mid": 42, "name": "Synthetic", "face": ""},
        "attr": 0,
        "cnt_info": {"collect": 0, "play": 0},
        "link": "",
        "ctime": 0,
        "pubtime": 0,
        "fav_time": 0,
        "bvid": bvid,
    }


def dash_stream(kind):
    return {
        "id": 64 if kind == "video" else 30280,
        "baseUrl": f"https://media.bilivideo.com/{kind}",
        "bandwidth": 1000,
        "mimeType": f"{kind}/mp4",
        "codecs": "avc1.64001f" if kind == "video" else "mp4a.40.2",
    }


def playback():
    return {
        "from": "local",
        "result": "suee",
        "message": "",
        "quality": 64,
        "format": "dash",
        "timelength": 1000,
        "video_codecid": 7,
        "seek_param": "start",
        "seek_type": "offset",
        "support_formats": [],
        "last_play_time": 0,
        "last_play_cid": 0,
        "dash": {"duration": 1, "video": [dash_stream("video")], "audio": [dash_stream("audio")]},
    }


def response(data, code=0):
    return httpx.Response(200, json={"code": code, "message": "", "data": data})


class Provider:
    def __init__(self, handler=None, pages=1):
        self.requests = []
        self.handler = handler
        self.pages = pages
        self.images = []
        self.image_handler = None

    def __call__(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/x/web-interface/nav":
            return response(
                {
                    "isLogin": "SESSDATA=" in request.headers.get("cookie", ""),
                    "mid": 42,
                    "uname": "Synthetic",
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                    },
                }
            )
        if path == "/x/web-interface/view":
            return response(video(request.url.params.get("bvid", BV), self.pages))
        if path == "/x/player/wbi/playurl":
            return response(playback())
        if self.handler:
            return self.handler(request)
        raise AssertionError(f"Unexpected synthetic request: {path}")

    def api(self):
        def image(request):
            self.images.append(request)
            assert self.image_handler is not None, "Unexpected synthetic image request"
            return self.image_handler(request)

        return Bilibili(
            gate=RequestGate(0, 0),
            transport=httpx.MockTransport(self),
            image_transport=httpx.MockTransport(image),
        )


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    work = tmp_path_factory.mktemp("synthetic-media")
    executable = ffmpeg_executable()
    paths = {"video": work / "video.mp4", "audio": work / "audio.m4a"}
    for name, source, codec in (
        ("video", "color=c=blue:s=16x16:r=5:d=0.4", "libx264"),
        ("audio", "sine=frequency=440:duration=0.4", "aac"),
    ):
        subprocess.run(
            [
                executable,
                "-nostdin",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                source,
                "-c:v" if name == "video" else "-c:a",
                codec,
                str(paths[name]),
            ],
            check=True,
            capture_output=True,
        )
    combined = work / "combined.mp4"
    subprocess.run(
        [
            executable,
            "-nostdin",
            "-loglevel",
            "error",
            "-i",
            str(paths["video"]),
            "-i",
            str(paths["audio"]),
            "-c",
            "copy",
            str(combined),
        ],
        check=True,
        capture_output=True,
    )
    paths["combined"] = combined
    return {key: Path(path).read_bytes() for key, path in paths.items()}


def library_provider(count=21):
    state = {
        "items": [favorite_item(f"BV{i:010}", i) for i in range(1, count + 1)],
        "mtime": 1,
        "repeat": False,
        "deny": False,
    }

    def handler(request):
        if state["deny"]:
            return httpx.Response(429)
        path = request.url.path
        if path.endswith("list-all"):
            return response(
                {
                    "count": 1,
                    "list": [
                        {
                            "id": 10,
                            "fid": 10,
                            "mid": 42,
                            "attr": 0,
                            "title": "我的收藏",
                            "fav_state": 0,
                            "media_count": 0,  # Count is a hint, not a pagination frontier.
                        }
                    ],
                }
            )
        if path.endswith("resource/ids"):
            items = state.get("ids_override", state["items"])
            return response([{"id": item["id"], "type": item["type"]} for item in items])
        if path.endswith("resource/list"):
            page = int(request.url.params["pn"])
            start = (page - 1) * 20
            if state["repeat"] and page > 1:
                start = 0
            info = folder_info(len(state["items"]))
            info["mtime"] = state["mtime"]
            return response(
                {
                    "info": info,
                    "medias": state["items"][start : start + 20],
                    "has_more": state["repeat"] or start + 20 < len(state["items"]),
                    "ttl": 0,
                }
            )
        raise AssertionError(path)

    return Provider(handler), state


def private_message(seq, talker=7, **changes):
    row = {
        "sender_uid": talker,
        "receiver_id": 42,
        "receiver_type": 1,
        "msg_type": 1,
        "content": json.dumps({"content": f"合成私信 {seq}"}),
        "msg_seqno": seq,
        "msg_key": 1000 + seq,
        "timestamp": 1700000000 + seq,
        "msg_status": 0,
        "msg_source": 0,
        "notify_code": "",
    }
    row.update(changes)
    return row


def private_session(talker, timestamp):
    return {
        "talker_id": talker,
        "session_type": 1,
        "session_ts": timestamp,
        "top_ts": 0,
        "ack_seqno": 0,
        "ack_ts": 0,
        "max_seqno": 25,
        "unread_count": 2,
        "system_msg_type": 0,
        "is_follow": 0,
        "is_dnd": 0,
        "last_msg": private_message(25, talker),
    }


def inbox_provider(count=2):
    provider, library_state = library_provider(1)
    library_handler = provider.handler
    state = {
        "lanes": {
            1: [private_session(7 + i, 3000000 - i) for i in range(count)],
            2: [private_session(99, 2000000)],
        },
        "messages": [private_message(i) for i in range(1, 26)],
        "repeat_sessions": False,
        "repeat_messages": False,
        "risk": False,
        "names_fail": False,
        "faces": {},
        "sent": {},
        "send_mode": "ok",
        "posts": 0,
        "read_failures": 0,
    }

    def handler(request):
        if request.url.host != "api.vc.bilibili.com":
            return library_handler(request)
        if state["risk"]:
            return httpx.Response(429)
        path, params = request.url.path, request.url.params
        assert "SESSDATA=" in request.headers.get("cookie", "")
        if path.endswith("send_msg"):
            assert request.method == "POST"
            form = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
            assert form["msg[sender_uid]"] == "42"
            assert form["msg[receiver_type]"] == form["msg[msg_type]"] == "1"
            assert form["csrf"] == "synthetic-csrf"
            state["posts"] += 1
            if state["send_mode"] == "reject":
                return response({}, code=-403)
            talker = int(form["msg[receiver_id]"])
            seq = 26 + len(state["sent"].get(talker, []))
            if state["send_mode"] != "lost":
                state["sent"].setdefault(talker, []).append(
                    private_message(
                        seq, sender_uid=42, receiver_id=talker, content=form["msg[content]"]
                    )
                )
            if state["send_mode"] in {"timeout", "lost"}:
                raise httpx.ReadTimeout("synthetic uncertain submission")
            return response({} if state["send_mode"] == "missing_key" else {"msg_key": 1000 + seq})
        assert request.method == "GET"
        if path.endswith("get_sessions"):
            rows = state["lanes"][int(params["session_type"])]
            if "end_ts" in params and not state["repeat_sessions"]:
                rows = [row for row in rows if row["session_ts"] < int(params["end_ts"])]
            return response(
                {
                    "session_list": rows[:20] or None,
                    "has_more": int(len(rows) > 20),
                    "is_address_list_empty": 0,
                    "anti_disturb_cleaning": False,
                    "show_level": False,
                }
            )
        if path.endswith("user/cards"):
            if state["names_fail"]:
                return httpx.Response(500)
            return response(
                [
                    {
                        "mid": int(mid),
                        "name": f"测试联系人 {mid}",
                        "face": state["faces"].get(int(mid), ""),
                    }
                    for mid in params["uids"].split(",")
                ]
            )
        if path.endswith("fetch_session_msgs"):
            if state["posts"] and state["read_failures"]:
                state["read_failures"] -= 1
                return httpx.Response(500)
            talker = int(params["talker_id"])
            rows = [dict(row, sender_uid=talker) for row in state["messages"]]
            rows.extend(state["sent"].get(talker, []))
            if "end_seqno" in params and not state["repeat_messages"]:
                rows = [row for row in rows if row["msg_seqno"] < int(params["end_seqno"])]
            page = sorted(rows, key=lambda row: row["msg_seqno"], reverse=True)[:20]
            return response(
                {
                    "messages": page or None,
                    "has_more": int(len(rows) > 20),
                    "min_seqno": min((row["msg_seqno"] for row in page), default=0),
                    "max_seqno": max((row["msg_seqno"] for row in page), default=0),
                }
            )
        raise AssertionError(f"Unexpected private endpoint: {path}")

    provider.handler = handler
    state["library"] = library_state
    provider.inbox_state = state
    return provider, state
