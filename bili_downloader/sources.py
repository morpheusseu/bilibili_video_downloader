"""Parse supported user inputs without fetching arbitrary URLs."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


class DownloadError(Exception):
    """A safe, user-facing error; never contains cookies or signed media URLs."""


@dataclass(frozen=True)
class Source:
    kind: str
    key: str
    page: int | None = None
    pattern: str = ""


def positive(value: str, label: str) -> int:
    if not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
        raise DownloadError(f"{label} 必须是正整数")
    return int(value)


def parse_source(value: str, mode: str = "auto", pattern: str = "") -> Source:
    value = value.strip()
    if mode == "user":
        positive(value, "UID")
        try:
            re.compile(pattern)
        except re.error as error:
            raise DownloadError("收藏夹名称的正则表达式无效") from error
        return Source("user", value, pattern=pattern)
    if mode == "favorite" and value.isdecimal():
        return Source("favorite", str(positive(value, "收藏夹 ID")))
    if re.fullmatch(r"BV[0-9A-Za-z]{10}", value):
        return Source("video", value)
    if re.fullmatch(r"av[0-9]+", value, flags=re.IGNORECASE):
        positive(value[2:], "AV ID")
        return Source("video", value.lower())
    if re.fullmatch(r"ep[0-9]+", value):
        positive(value[2:], "剧集 ID")
        return Source("episode", value[2:])
    url = urlparse(value)
    if (
        url.scheme not in {"https", "http"}
        or url.hostname
        not in {
            "bilibili.com",
            "www.bilibili.com",
            "m.bilibili.com",
            "space.bilibili.com",
        }
        or url.username
        or url.password
        or url.netloc != url.hostname
    ):
        raise DownloadError("请输入 BV/AV、ep ID 或完整的 bilibili.com 链接（不自动展开短链接）")
    query = parse_qs(url.query)
    if "fid" in query:
        return Source("favorite", str(positive(query["fid"][0], "收藏夹 ID")))
    if match := re.fullmatch(r"/medialist/play/ml([0-9]+)/?", url.path):
        return Source("favorite", str(positive(match[1], "收藏夹 ID")))
    if match := re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[0-9]+)/?", url.path):
        page = positive(query["p"][0], "分 P") if "p" in query else None
        return Source("video", match[1], page=page)
    if match := re.fullmatch(r"/bangumi/play/ep([0-9]+)/?", url.path):
        return Source("episode", str(positive(match[1], "剧集 ID")))
    raise DownloadError("链接不是受支持的视频、番剧单集或收藏夹链接")


@dataclass(frozen=True)
class VideoPart:
    bvid: str
    aid: int
    cid: int
    title: str
    part_title: str
    number: int
    episode_id: int | None = None

    @property
    def stem(self) -> str:
        title = self.title
        if self.part_title.strip() != self.title.strip():
            title += f" - P{self.number} {self.part_title}"
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
        title = title.encode("utf-8")[:150].decode("utf-8", errors="ignore") or "video"
        # IDs prevent collisions between equal titles or different parts.
        return f"{title} [{self.bvid}-cid{self.cid}]"
