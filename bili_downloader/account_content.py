"""Read-only browsing with disposable display projections and bounded cursors."""

import logging
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from .provider_views import feed_page
from .sources import DownloadError

KINDS = ("dynamic", "followings", "history")
TITLES = {"dynamic": "关注动态", "followings": "关注的人", "history": "观看历史"}
PAGE_SIZE = 20
FOLLOWING_PAGE_SIZE = 50
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContentItem:
    key: str
    title: str
    summary: str
    body: str
    image_role: str
    url: str = ""
    image: str = ""
    special: bool | None = None

    @classmethod
    def from_record(cls, record):
        # Earlier saved display rows have no special-follow field: unknown,
        # never silently reclassify them as ordinary follows.
        row = cls(**record)
        if (
            any(
                not isinstance(getattr(row, key), str)
                for key in (
                    "key",
                    "title",
                    "summary",
                    "body",
                    "image_role",
                    "url",
                    "image",
                )
            )
            or row.image_role not in {"avatar", "cover"}
            or (row.special is not None and type(row.special) is not bool)
            or (
                row.url
                and not row.url.startswith(
                    (
                        "https://t.bilibili.com/",
                        "https://space.bilibili.com/",
                        "https://www.bilibili.com/",
                        "https://live.bilibili.com/",
                    )
                )
            )
        ):
            raise ValueError("Invalid saved display row")
        return row


@dataclass(frozen=True)
class ContentView:
    kind: str
    items: tuple[ContentItem, ...]
    has_more: bool
    notice: str
    cached: bool = False


def local_time(value):
    try:
        return (
            datetime.fromtimestamp(int(value)).astimezone().strftime("%Y-%m-%d %H:%M")
            if value
            else "时间未知"
        )
    except (ValueError, TypeError, OverflowError, OSError):
        return "时间未知"


def _map(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value if isinstance(value, str) else ""


def _description(value):
    value = _map(value)
    if isinstance(value.get("text"), str):
        return value["text"]
    nodes = value.get("rich_text_nodes")
    return (
        "".join(_text(_map(node).get("text")) for node in nodes) if isinstance(nodes, list) else ""
    )


def _forward_excerpt(original):
    original = _map(original)
    if not original:
        return "接口未提供转发原文，可通过“打开原文”查看。"
    if original.get("visible") is False:
        return "转发原文当前不可见或已删除。"
    modules = _map(original.get("modules"))
    author = _map(modules.get("module_author"))
    content = _map(modules.get("module_dynamic"))
    major = _map(content.get("major"))
    archive = _map(major.get("archive"))
    opus = _map(major.get("opus"))
    article = _map(major.get("article")) or _map(major.get("common"))
    pictures = _map(major.get("draw")).get("items") or opus.get("pics") or []
    name = _text(author.get("name"))
    sender = "@" + name if name else "作者信息暂不可用"
    if author.get("pub_ts"):
        sender += " · " + local_time(author["pub_ts"])
    detail = [
        "转发原文（仅第一层）",
        sender,
        _description(content.get("desc")),
        _text(archive.get("title")) or _text(opus.get("title")) or _text(article.get("title")),
        _text(archive.get("desc"))
        or _description(opus.get("summary"))
        or _text(article.get("desc")),
    ]
    if isinstance(pictures, list) and pictures:
        detail.append(f"图片 {len(pictures)} 张（仅计数，不加载原文图片）")
    if archive:
        detail.append("视频 1 个 · " + _text(archive.get("bvid")))
    if original.get("type") == "DYNAMIC_TYPE_FORWARD":
        detail.append("更深层的转发不展开，可打开原文查看。")
    return "\n".join(filter(None, detail))


def dynamic_item(item):
    if not item.id_str.isdecimal():
        raise DownloadError("动态标识无效，已保留原列表")
    url = f"https://t.bilibili.com/{item.id_str}"
    if not item.visible:
        return ContentItem(
            item.id_str,
            "当前不可见的动态",
            "不可见",
            f"动态 {item.id_str} 当前不可见。",
            image_role="avatar",
            url=url,
        )
    if not isinstance(item.modules, dict):
        raise DownloadError("动态内容结构不兼容，未把未知格式当作空动态")
    author = _map(item.modules.get("module_author"))
    content = _map(item.modules.get("module_dynamic"))
    major = _map(content.get("major"))
    desc = _description(content.get("desc"))
    title, detail, image = "", "", ""
    if archive := _map(major.get("archive")):
        title, detail, image = (_text(archive.get(key)) for key in ("title", "desc", "cover"))
        detail = "\n".join(
            filter(None, (detail, _text(archive.get("bvid")), _text(archive.get("duration_text"))))
        )
    elif _map(major.get("draw")) or _map(major.get("opus")):
        draw = _map(major.get("draw"))
        opus = _map(major.get("opus"))
        title = _text(opus.get("title"))
        detail = _description(opus.get("summary"))
        pictures = draw.get("items") or opus.get("pics") or []
        if isinstance(pictures, list) and pictures:
            detail += f"\n图片 {len(pictures)} 张（不自动下载原图）"
    elif card := _map(major.get("article")) or _map(major.get("common")):
        title, detail = (_text(card.get(key)) for key in ("title", "desc"))
    kind = {
        "DYNAMIC_TYPE_AV": "视频",
        "DYNAMIC_TYPE_WORD": "文字",
        "DYNAMIC_TYPE_DRAW": "图文",
        "DYNAMIC_TYPE_ARTICLE": "专栏",
        "DYNAMIC_TYPE_FORWARD": "转发",
        "DYNAMIC_TYPE_LIVE_RCMD": "直播动态",
    }.get(item.type_field, item.type_field)
    name = _text(author.get("name")) or f"UID {author.get('mid', '未知')}"
    stamp = local_time(author.get("pub_ts"))
    body = "\n\n".join(
        filter(
            None,
            (
                f"{name} · {kind} · {stamp}",
                desc,
                title,
                detail.strip(),
                _forward_excerpt(item.orig) if item.type_field == "DYNAMIC_TYPE_FORWARD" else "",
                f"动态 ID {item.id_str}",
                url,
            ),
        )
    )
    title = title or (desc.splitlines()[0][:120] if desc else f"{name}的{kind}")
    return ContentItem(
        item.id_str,
        title,
        f"{name} · {kind} · {stamp}",
        body,
        image_role="cover" if image else "avatar",
        url=url,
        image=image or _text(author.get("face")),
    )


def following_item(user):
    if user.mid <= 0:
        raise DownloadError("关注列表返回无效 UID，已保留原列表")
    name = user.name or f"UID {user.mid}"
    special = (
        user.special == 1 or -10 in (user.tag or ())
        if {"special", "tag"} & user.model_fields_set
        else None
    )
    relation = "特别关注" if special else "普通关注" if special is False else "特别关注状态未核验"
    body = "\n\n".join(
        filter(
            None,
            (
                f"{name} · UID {user.mid}",
                user.sign,
                user.official_verify.desc if user.official_verify else "",
                "关注关系修改时间：" + local_time(user.mtime),
                relation,
                "仅展示平台返回的关注关系；不据此推断私信权限或全局联系人。",
            ),
        )
    )
    return ContentItem(
        str(user.mid),
        name,
        f"{relation} · UID {user.mid}",
        body,
        image_role="avatar",
        url=f"https://space.bilibili.com/{user.mid}",
        image=user.face,
        special=special,
    )


def history_item(item):
    detail = item.history
    if detail.oid <= 0 or item.view_at <= 0:
        raise DownloadError("观看历史缺少有效标识或观看时间，已保留原列表")
    url = ""
    if detail.business == "archive":
        url = f"https://www.bilibili.com/video/av{detail.oid}"
    elif detail.business == "pgc" and detail.epid:
        url = f"https://www.bilibili.com/bangumi/play/ep{detail.epid}"
    elif detail.business == "live":
        url = f"https://live.bilibili.com/{detail.oid}"
    progress = "已看完" if item.progress == -1 else f"播放进度 {max(0, item.progress or 0)} 秒"
    if item.duration:
        progress += f" / 时长 {item.duration} 秒"
    stamp = local_time(item.view_at)
    body = "\n\n".join(
        filter(
            None,
            (
                item.title,
                item.long_title,
                item.author_name,
                f"观看时间：{stamp}",
                progress,
                detail.part,
                f"类型：{detail.business} · 内容 ID {detail.oid}",
                url,
                "这是 B 站保存的观看历史，不是浏览器历史或完整的逐次播放日志。",
            ),
        )
    )
    return ContentItem(
        f"{detail.business}:{detail.oid}:{item.view_at}",
        item.title,
        f"{item.author_name or detail.business} · {stamp}",
        body,
        image_role="cover",
        url=url,
        image=item.cover or "",
    )


def merged(existing, page, has_more):
    rows = {row.key: row for row in existing}
    before = len(rows)
    for row in page:
        rows[row.key] = row
    if has_more and len(rows) == before:
        raise DownloadError("分页没有新增条目，已停止并保留原列表；可手动刷新")
    return tuple(rows.values())


class AccountContent:
    def __init__(self, sdk, *, cache=None):
        self.sdk = sdk
        self.cache = cache
        self.views = {}
        self.frontiers = {}
        if cache is not None:
            for kind in KINDS:
                saved = cache.get("content", kind)
                if saved is not None:
                    try:
                        rows = tuple(ContentItem.from_record(item) for item in saved["items"])
                        if (kind != "followings" and len(rows) > 200) or (
                            kind == "followings" and any(not row.key.isdecimal() for row in rows)
                        ):
                            raise ValueError("Invalid content cache")
                        view = ContentView(
                            kind, rows, bool(saved["has_more"]), saved["notice"], True
                        )
                        frontier = dict(saved["frontier"])
                        if kind == "followings":
                            # Existing saved pages were fetched with ps=20. Keep
                            # that size until this traversal ends; changing it
                            # midway would skip people at the next pn boundary.
                            frontier.setdefault("page_size", 20)
                            if (
                                frontier["page_size"] not in (20, FOLLOWING_PAGE_SIZE)
                                or type(frontier.get("next_page")) is not int
                                or frontier["next_page"] < 2
                                or len({row.key for row in rows}) != len(rows)
                            ):
                                raise ValueError("Invalid following frontier")
                        self.views[kind], self.frontiers[kind] = view, frontier
                    except (KeyError, TypeError, ValueError):
                        # Display strings are not provider truth. A stale projection
                        # must be rebuilt on demand, never disable valid DM/image caches
                        # or retain a pagination cursor without its usable pages.
                        log.info("Content display cache needs refresh kind=%s", kind)

    async def read(self, kind, account_id, *, more=False, check=False):
        if kind not in KINDS:
            raise DownloadError("未知的账号内容类型")
        current = self.views.get(kind)
        if more and (current is None or not current.has_more):
            raise DownloadError("没有可继续读取的页面")
        frontier = self.frontiers.get(kind, {})
        if kind == "dynamic":
            view, frontier = await self._dynamic(current, more, check, frontier)
        elif kind == "followings":
            view, frontier = await self._followings(current, account_id, more, frontier)
        else:
            view, frontier = await self._history(current, more, frontier)
        # Never store a farther pagination cursor without all pages preceding it.
        # Larger explicitly loaded views stay in RAM; restart resumes the last
        # complete <=200-item cache instead of skipping uncached pages.
        if self.cache is not None and (kind == "followings" or len(view.items) <= 200):
            self.cache.put_many(
                {
                    ("content", kind): {
                        "items": [asdict(row) for row in view.items],
                        "has_more": view.has_more,
                        "notice": view.notice,
                        "frontier": frontier,
                    }
                }
            )
        self.views[kind], self.frontiers[kind] = view, frontier
        return view

    async def _dynamic(self, current, more, check, frontier):
        baseline = frontier.get("baseline", "")
        if check and current is not None and baseline:
            update = await self.sdk.dynamic.check_new(update_baseline=baseline)
            if update.update_num == 0:
                return replace(
                    current,
                    notice="暂无新动态，复用已加载内容；新增检查不代表已核验旧动态的编辑或删除。",
                ), frontier
            if update.update_num < 0:
                raise DownloadError("动态更新数量无效，已保留原列表")
        data = await feed_page(self.sdk, offset=frontier["offset"] if more else None)
        visited = set(frontier["seen"]) if more else set()
        if data.has_more and (not data.offset or data.offset in visited):
            raise DownloadError("动态分页游标未推进，已保留原列表")
        items = merged(
            current.items if more else (),
            (dynamic_item(item) for item in data.items),
            data.has_more,
        )
        return ContentView(
            "dynamic",
            items,
            data.has_more,
            ("已更新最新一页；更早内容可重新分页。 " if check else "")
            + "统一关注流，不逐个扫描 UP 主，不等于对方的全部操作。",
            current.cached if more else False,
        ), {
            "offset": data.offset,
            "seen": sorted(visited | {data.offset}),
            "baseline": baseline if more else data.update_baseline,
        }

    async def _followings(self, current, account_id, more, frontier):
        page = frontier["next_page"] if more else 1
        size = frontier["page_size"] if more else FOLLOWING_PAGE_SIZE
        data = await self.sdk.user.followings(mid=account_id, page=page, page_size=size)
        items = merged(
            current.items if more else (),
            (following_item(user) for user in data.list),
            False,
        )
        has_more = len(items) < data.total
        if data.total < 0 or (has_more and len(items) == (len(current.items) if more else 0)):
            raise DownloadError("关注分页不完整或未推进，已保留原列表；请手动刷新")
        if has_more and len(data.list) != size:
            raise DownloadError("关注分页数量与请求不符，已停止；未跳过未读取的关注者")
        return ContentView(
            "followings",
            items,
            has_more,
            f"平台报告关注 {data.total} 人；分页期间可能变化，不据此自动判定取关。",
            current.cached if more else False,
        ), {"next_page": page + 1, "page_size": size}

    async def _history(self, current, more, frontier):
        params = {"page_size": PAGE_SIZE}
        if more:
            max_id, view_at, business = frontier["cursor"]
            params.update(max_id=max_id, view_at=view_at, business=business)
        data = await self.sdk.historytoview.history_list(**params)
        cursor = (data.cursor.max, data.cursor.view_at, data.cursor.business)
        visited = {tuple(value) for value in frontier["seen"]} if more else set()
        # The endpoint has no has_more: an empty page or its zero cursor is terminal.
        has_more = bool(data.list and data.cursor.max and data.cursor.view_at)
        if has_more and cursor in visited:
            raise DownloadError("观看历史游标未推进，已保留原列表")
        items = merged(
            current.items if more else (), (history_item(item) for item in data.list), has_more
        )
        return ContentView(
            "history",
            items,
            has_more,
            "仅平台保存的观看记录；不记录浏览器访问，不上传播放进度，不删除历史。",
            current.cached if more else False,
        ), {"cursor": cursor, "seen": sorted(visited | {cursor})}
