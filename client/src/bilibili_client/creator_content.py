"""Selected-following pages and account-specific interaction observations.

Pages own content/cursors; one account-wide video observation owns like/coin
facts shared by dynamic cards and submissions. Percentages are derived from
these saved facts, never persisted as an independent counter.
"""

import logging
import time
from dataclasses import asdict, dataclass, replace

from .account_content import ContentItem, dynamic_item, local_time
from .interactions import INTERACTION_AGE, PersonalInteractions, valid_time
from .provider_views import feed_page, uploaded_videos
from .sources import DownloadError

log = logging.getLogger(__name__)
PAGE_SIZE = 20


@dataclass(frozen=True)
class CreatorEntry:
    content: ContentItem
    aid: int | None = None
    liked: bool | None = None
    reposted: bool | None = None
    coined: bool | None = None
    liked_at: float | None = None
    coined_at: float | None = None
    reposted_at: float | None = None
    uncertain: tuple[str, ...] = ()

    def is_current(self, field, now):
        stamp = getattr(self, field + "_at")
        return (
            getattr(self, field) is not None
            and stamp is not None
            and field not in self.uncertain
            and 0 <= now - stamp < INTERACTION_AGE
        )

    @classmethod
    def from_record(cls, record):
        row = cls(**{**record, "content": ContentItem.from_record(record["content"])})
        if (row.aid is not None and (type(row.aid) is not int or row.aid <= 0)) or any(
            value is not None and type(value) is not bool
            for value in (
                row.liked,
                row.reposted,
                row.coined,
            )
        ):
            raise ValueError("Invalid interaction observation")
        if any(
            stamp is not None and not valid_time(stamp)
            for stamp in (row.liked_at, row.coined_at, row.reposted_at)
        ) or any(field not in {"liked", "coined", "reposted"} for field in row.uncertain):
            raise ValueError("Invalid interaction observation time")
        return row


@dataclass(frozen=True)
class CreatorPage:
    entries: tuple[CreatorEntry, ...]
    cursor: str | int | None
    has_more: bool
    fetched_at: float


@dataclass(frozen=True)
class CreatorView:
    mid: int
    kind: str
    page: int
    entries: tuple[CreatorEntry, ...]
    has_more: bool
    checked_at: float
    cached: bool = False
    notice: str = ""
    observed_at: float = 0

    def statistics(self):
        result = []
        total = len(self.entries)
        if not total:
            return "本页暂无内容，没有可统计的个人互动。"
        for name, field in (("点赞", "liked"), ("投币", "coined"), ("转发", "reposted")):
            known = [
                getattr(row, field)
                for row in self.entries
                if row.is_current(field, self.observed_at)
            ]
            positive = sum(known)
            if not known:
                result.append(f"{name}：未知（已核验 0/{total}）")
            else:
                result.append(
                    f"{name}：{positive / len(known):.0%}"
                    f"（{positive}/{len(known)}，已核验 {len(known)}/{total}，"
                    f"待核验 {total - len(known)}）"
                )
        return "   ·   ".join(result)


def _flag(value):
    # Counts and missing status must never turn into a personal interaction.
    if type(value) is bool:
        return value
    return bool(value) if type(value) is int and value in (0, 1) else None


def dynamic_entry(item, mid):
    modules = item.modules if isinstance(item.modules, dict) else {}
    author = modules.get("module_author") or {}
    if item.visible and (not isinstance(author, dict) or author.get("mid") != mid):
        raise DownloadError("动态作者与所选关注者不一致，保留原缓存")
    stat = modules.get("module_stat") or {}
    stat = stat if isinstance(stat, dict) else {}
    states = {}
    for field, remote in (("liked", "like"), ("reposted", "forward"), ("coined", "coin")):
        value = stat.get(remote)
        states[field] = _flag(value.get("status")) if isinstance(value, dict) else None
    content = modules.get("module_dynamic") or {}
    major = content.get("major") or {} if isinstance(content, dict) else {}
    archive = major.get("archive") or {} if isinstance(major, dict) else {}
    aid = archive.get("aid") if isinstance(archive, dict) else None
    if isinstance(aid, str) and aid.isdecimal():
        aid = int(aid)
    aid = aid if type(aid) is int and aid > 0 else None
    return CreatorEntry(dynamic_item(item), aid=aid, **states)


def video_entry(video, mid):
    if video.aid <= 0 or video.mid <= 0 or (video.mid != mid and not video.is_union_video):
        raise DownloadError("投稿作者或视频标识不匹配，保留原缓存")
    stamp = local_time(video.created)
    content = ContentItem(
        str(video.aid),
        video.title,
        ("合作投稿 · " if video.is_union_video else "")
        + f"{video.author} · {stamp} · {video.length}",
        "\n\n".join(
            filter(
                None,
                (
                    video.title,
                    video.description,
                    video.author,
                    stamp,
                    video.bvid,
                    "合作投稿；按平台原字段展示实际作者。" if video.is_union_video else "",
                ),
            )
        ),
        "cover",
        f"https://www.bilibili.com/video/av{video.aid}",
        video.pic,
    )
    return CreatorEntry(content, aid=video.aid)


class CreatorContent:
    def __init__(self, sdk, *, cache=None, clock=time.time):
        self.sdk, self.cache, self.clock = sdk, cache, clock
        self.pages = {}
        self.interactions = PersonalInteractions(sdk, cache=cache, clock=lambda: self.clock())

    def _pages(self, mid, kind):
        key = f"{mid}:{kind}"
        if key in self.pages:
            return self.pages[key]
        pages = []
        if self.cache is not None:
            try:
                index = self.cache.get("creator_index", key, max_age=None)
                if index is not None:
                    count = index["count"]
                    if type(count) is not int or count < 1:
                        raise ValueError("Invalid page count")
                    for number in range(1, count + 1):
                        value = self.cache.get("creator_page", f"{key}:{number}", max_age=None)
                        entries = tuple(CreatorEntry.from_record(row) for row in value["entries"])
                        cursor, has_more = value["cursor"], value["has_more"]
                        if (
                            type(has_more) is not bool
                            or not valid_time(value["fetched_at"])
                            or len({row.content.key for row in entries}) != len(entries)
                            or (
                                kind == "dynamic"
                                and cursor is not None
                                and not isinstance(cursor, str)
                            )
                            or (kind == "videos" and cursor is not None and type(cursor) is not int)
                            or (has_more and (not cursor or not entries))
                            or (number < count and not has_more)
                        ):
                            raise ValueError("Invalid saved page")
                        pages.append(CreatorPage(entries, cursor, has_more, value["fetched_at"]))
            except (DownloadError, ValueError, TypeError, KeyError):
                log.warning("Creator page cache incomplete; cursor not restored")
                pages = []
        self.pages[key] = pages
        return pages

    def cached_view(self, mid, kind, page=1):
        pages = self._pages(mid, kind)
        if not 1 <= page <= len(pages):
            return None
        return self._view(mid, kind, page, pages[page - 1], cached=True)

    def _view(self, mid, kind, number, page, *, cached=False, notice=""):
        entries = []
        for entry in page.entries:
            values = {
                field + "_at": page.fetched_at if getattr(entry, field) is not None else None
                for field in ("liked", "coined", "reposted")
            }
            uncertain = []
            if entry.aid:
                # A dynamic's own like and a video like are different targets.
                for field, observation in self.interactions.video(entry.aid).items():
                    if field == "liked" and kind != "videos":
                        continue
                    if (
                        getattr(entry, field) is not None
                        and page.fetched_at > observation.checked_at
                    ):
                        continue
                    values.update({field: observation.value, field + "_at": observation.checked_at})
                    if observation.uncertain:
                        uncertain.append(field)
            if kind == "dynamic":
                repost = self.interactions.repost(entry.content.key)
                if repost and (entry.reposted is None or repost.checked_at > page.fetched_at):
                    values.update(reposted=repost.value, reposted_at=repost.checked_at)
                    if repost.uncertain:
                        uncertain.append("reposted")
            entry = replace(entry, **values, uncertain=tuple(uncertain))
            entries.append(entry)
        return CreatorView(
            mid,
            kind,
            number,
            tuple(entries),
            page.has_more,
            page.fetched_at,
            cached,
            notice,
            self.clock(),
        )

    def _save_page(self, mid, kind, number, page):
        key = f"{mid}:{kind}"
        previous = self._pages(mid, kind)
        old = previous[number - 1] if number <= len(previous) else None
        keep_tail = old is not None and (
            tuple(row.content.key for row in old.entries)
            == tuple(row.content.key for row in page.entries)
            and old.cursor == page.cursor
            and old.has_more == page.has_more
        )
        pages = [*previous[: number - 1], page, *(previous[number:] if keep_tail else ())]
        if self.cache is not None:
            self.cache.put_many(
                {
                    ("creator_page", f"{key}:{number}"): {
                        "entries": [asdict(row) for row in page.entries],
                        "cursor": page.cursor,
                        "has_more": page.has_more,
                        "fetched_at": page.fetched_at,
                    },
                    ("creator_index", key): {"count": len(pages)},
                },
                remove=[
                    ("creator_page", f"{key}:{i}") for i in range(len(pages) + 1, len(previous) + 1)
                ],
            )
        self.pages[key] = pages
        return old is not None and not keep_tail

    async def read_page(self, mid, kind, page=1, *, refresh=False, on_update=None):
        """Acquire a complete provider page without delaying it for video checks."""
        if (
            type(mid) is not int
            or mid <= 0
            or kind not in {"dynamic", "videos"}
            or type(page) is not int
            or page < 1
        ):
            raise DownloadError("请选择有效的关注者和页面")
        pages = self._pages(mid, kind)
        cached = self.cached_view(mid, kind, page)
        if cached and on_update:
            on_update(cached)
        if cached and not refresh and (page > 1 or 0 <= self.clock() - cached.checked_at < 60):
            return cached
        reset = False
        if page > 1 and (page - 1 > len(pages) or not pages[page - 2].has_more):
            raise DownloadError("上一页尚未完整读取，未跳过分页位置")
        cursor = pages[page - 2].cursor if page > 1 else None
        if kind == "dynamic":
            data = await feed_page(self.sdk, host_mid=mid, offset=cursor)
            entries = tuple(dynamic_entry(item, mid) for item in data.items)
            next_cursor, has_more = data.offset, bool(data.has_more)
            if has_more and (not next_cursor or next_cursor == cursor):
                raise DownloadError("关注者动态分页未推进，保留原缓存")
        else:
            data = await uploaded_videos(self.sdk, mid=mid, page=page, page_size=PAGE_SIZE)
            if data.is_risk or data.page.pn != page or data.page.count < 0:
                raise DownloadError("投稿列表未通过页面或风控检查，已停止")
            entries = tuple(video_entry(item, mid) for item in data.list.videos)
            has_more = page * PAGE_SIZE < data.page.count
            next_cursor = page + 1
        keys = {row.content.key for row in entries}
        earlier = {row.content.key for p in pages[: page - 1] for row in p.entries}
        if len(keys) != len(entries) or (not keys - earlier and (has_more or entries)):
            raise DownloadError("关注者页面重复或未推进，保留原缓存")
        value = CreatorPage(entries, next_cursor, has_more, self.clock())
        reset = self._save_page(mid, kind, page, value)
        return self._view(
            mid,
            kind,
            page,
            value,
            notice="最新页已变化，后续分页已重置，避免跳过内容。" if reset else "",
        )

    async def read(
        self, mid, kind, page=1, *, refresh=False, verify=False, on_update=None, discover=None
    ):
        cached = self.cached_view(mid, kind, page)
        if verify and cached and kind == "videos":
            view = cached
        else:
            view = await self.read_page(
                mid, kind, page, refresh=refresh or verify, on_update=on_update
            )
        value = self._pages(mid, kind)[page - 1]
        if discover:
            if on_update:
                on_update(self._view(mid, kind, page, value, cached=view.cached))
            await discover()
        targets = list(dict.fromkeys(row.aid for row in value.entries if row.aid))
        pending = [aid for aid in targets if verify or self.interactions.needs_video(aid)]
        for index, aid in enumerate(pending):
            if on_update:
                on_update(
                    self._view(
                        mid,
                        kind,
                        page,
                        value,
                        cached=view.cached,
                        notice=f"后台核验互动 {index + 1}/{len(pending)}；"
                        "可继续切换 UP 主，已读内容保留。",
                    )
                )
            await self.interactions.verify_video(aid, force=verify)
        return self._view(
            mid,
            kind,
            page,
            value,
            cached=view.cached,
            notice=view.notice + "比例仅统计本页；未知不算未互动。"
            "转发状态以明确的个人状态为准，不用全站转发总数推断。" + self.interactions.notice(),
        )
