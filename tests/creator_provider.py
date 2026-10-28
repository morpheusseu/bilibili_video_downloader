"""Sanitized provider shapes at the real SDK/HTTP boundary, no credentials."""

import httpx
from account_content_provider import content_provider, dynamic
from conftest import response


def creator_provider(count=23):
    provider = content_provider()
    fallback = provider.handler
    for row in provider.content_state["followings"]:
        row["special"] = int(row["mid"] == 7)
        row["tag"] = [-10] if row["mid"] == 7 else None
    state = {
        "videos": [],
        "dynamics": [],
        "states": {},
        "repeat": False,
        "risk": None,
        "risk_flag": False,
    }
    for number in range(count, 0, -1):
        video = {
            "aid": number,
            "bvid": f"BV{number:010}",
            "mid": 7,
            "title": f"合成投稿 {number}",
            "author": "合成 UP 主",
            "description": "投稿说明",
            "created": 1700000000 + number,
            "pic": "",
            "length": "1:20",
        }
        state["videos"].append(video)
        row = dynamic(1000 + number)
        row["type"] = "DYNAMIC_TYPE_AV"
        row["modules"]["module_dynamic"]["major"] = {
            "archive": {
                "aid": str(number),
                "bvid": video["bvid"],
                "title": video["title"],
                "desc": video["description"],
                "cover": "",
                "duration_text": "1:20",
            }
        }
        row["modules"]["module_stat"] = {
            "like": {"count": 999, "status": number % 2 == 0},
            "forward": {"count": 500},  # A total is not a personal flag.
        }
        state["dynamics"].append(row)
        state["states"][number] = {"like": number % 3 == 0, "coin": number % 3}

    def handler(request):
        path, params = request.url.path, request.url.params
        scoped = path.endswith("/feed/all") and "host_mid" in params
        if not scoped and path not in {
            "/x/space/wbi/arc/search",
            "/x/web-interface/archive/relation",
        }:
            return fallback(request)
        assert request.method == "GET"
        assert "SESSDATA=" in request.headers.get("cookie", "")
        if state["risk"]:
            return httpx.Response(state["risk"])
        if scoped:
            rows = state["dynamics"] if params["host_mid"] == "7" else []
            start = 0
            if "offset" in params and not state["repeat"]:
                start = next(
                    i + 1 for i, row in enumerate(rows) if row["id_str"] == params["offset"]
                )
            # Recorded host_mid feed pages contain 12 items; submissions use 20.
            page = rows[start : start + 12]
            return response(
                {
                    "items": page,
                    "has_more": start + 12 < len(rows),
                    "offset": page[-1]["id_str"] if page else "",
                    "update_baseline": page[0]["id_str"] if page else "",
                    "update_num": 0,
                }
            )
        if path.endswith("/arc/search"):
            assert params["ps"] == "20"
            videos = state["videos"] if params["mid"] == "7" else []
            assert "w_rid" in params and "wts" in params
            page = int(params["pn"])
            start = (page - 1) * 20 if not state["repeat"] else 0
            return response(
                {
                    "list": {"vlist": videos[start : start + 20], "tlist": None},
                    "page": {"count": len(videos), "pn": page, "ps": 20},
                    "is_risk": state["risk_flag"],
                }
            )
        return response(state["states"][int(params["aid"])])

    provider.handler, provider.creator_state = handler, state
    return provider
