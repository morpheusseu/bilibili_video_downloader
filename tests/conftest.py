"""Synthetic provider responses; tests never load credentials or call Bilibili."""

import base64
import os
import subprocess
from pathlib import Path

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
        return Bilibili(gate=RequestGate(0, 0), transport=httpx.MockTransport(self))


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
