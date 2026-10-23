"""Synthetic HTTP boundary shared by account browsing and real Qt worker tests."""

import httpx
from conftest import inbox_provider, response


def dynamic(number):
    return {
        "basic": {
            "comment_id_str": str(number),
            "comment_type": 11,
            "like_icon": None,
            "rid_str": str(number),
        },
        "id_str": str(number),
        "type": "DYNAMIC_TYPE_DRAW",
        "visible": True,
        "modules": {
            "module_author": {"mid": 7, "name": "合成 UP 主", "pub_ts": 1700000000},
            "module_dynamic": {
                "desc": {"text": f"合成动态 {number} <plain>"},
                "major": {"draw": {"items": [{"src": "", "width": 800, "height": 600}]}},
            },
        },
    }


def history(number):
    return {
        "title": f"合成观看 {number}",
        "author_name": "合成 UP 主",
        "view_at": 1700000000 + number,
        "cover": "",
        "history": {"oid": number, "business": "archive", "bvid": "BV178411i7AD", "dt": 2},
        "progress": 12,
        "duration": 60,
        "is_fav": 0,
        "kid": number,
    }


def content_provider(count=21):
    provider, _ = inbox_provider()
    fallback = provider.handler
    state = {
        "dynamic": [dynamic(i) for i in range(count, 0, -1)],
        "followings": [
            {"mid": i, "uname": f"关注 {i}", "sign": "合成签名", "mtime": 1700000000}
            for i in range(1, count + 1)
        ],
        "history": [history(i) for i in range(count, 0, -1)],
        "new": 0,
        "repeat": False,
        "risk": None,
        "fail": False,
        "empty": False,
    }

    def handler(request):
        path, params = request.url.path, request.url.params
        if path not in {
            "/x/polymer/web-dynamic/v1/feed/all",
            "/x/polymer/web-dynamic/v1/feed/all/update",
            "/x/relation/followings",
            "/x/web-interface/history/cursor",
        }:
            return fallback(request)
        assert request.method == "GET" and "SESSDATA=" in request.headers.get("cookie", "")
        if state["risk"]:
            return (
                httpx.Response(state["risk"]) if state["risk"] > 0 else response({}, state["risk"])
            )
        if state["fail"]:
            return httpx.Response(500)
        if path.endswith("/update"):
            return response({"update_num": state["new"]})
        size = 20
        if path.endswith("/feed/all"):
            start = int(params.get("offset", 0)) if not state["repeat"] else 0
            rows = state["dynamic"]
            return response(
                {
                    "items": rows[start : start + size],
                    "has_more": start + size < len(rows),
                    "offset": str(start + size),
                    "update_baseline": "synthetic-baseline",
                    "update_num": 0,
                }
            )
        if path.endswith("/followings"):
            assert params["vmid"] == "42" and params["ps"] == "20"
            start = (int(params["pn"]) - 1) * size if not state["repeat"] else 0
            return response(
                {
                    "list": [] if state["empty"] else state["followings"][start : start + size],
                    "total": len(state["followings"]),
                }
            )
        rows = state["history"]
        if "max" in params and not state["repeat"]:
            rows = [row for row in rows if row["history"]["oid"] < int(params["max"])]
        page = rows[:size]
        cursor = {
            "max": page[-1]["history"]["oid"] if len(rows) > size else 0,
            "view_at": page[-1]["view_at"] if len(rows) > size else 0,
            "business": "archive",
            "ps": size,
        }
        return response({"list": page, "cursor": cursor, "tab": []})

    provider.handler, provider.content_state = handler, state
    return provider
