"""Resumable collection of every following's two latest content pages.

The work plan is only a checkpoint. AccountContent owns the directory,
CreatorContent owns pages/cursors. Personal interaction checks have their own
scope and do not delay this collection's completion.
No per-creator timers, historical crawls or second HTTP client are introduced.
"""

import logging
import time
from dataclasses import dataclass

from .creator_content import valid_time
from .sources import DownloadError

log = logging.getLogger(__name__)
ROUND_STEPS = 6
KINDS = ("dynamic", "videos")


def _decode_checkpoint(saved):
    """Convert retained interaction-era work plans into the content-only plan."""
    mids = saved["mids"]
    stage = saved["stage"]
    position = saved["content_position"]
    if (
        not valid_time(saved["started_at"])
        or type(saved["directory_started"]) is not bool
        or stage not in {"directory", "content", "interactions", "done"}
        or not isinstance(mids, list)
        or any(type(mid) is not int or mid <= 0 for mid in mids)
        or len(set(mids)) != len(mids)
        or type(position) is not int
        or not 0 <= position <= 2 * len(mids)
        or (stage in {"interactions", "done"} and position != 2 * len(mids))
    ):
        raise ValueError("Invalid collection checkpoint")
    # Legacy aids and interaction_position describe work this collection no
    # longer owns. Do not read or validate them, and never persist them again.
    return {
        "started_at": saved["started_at"],
        "stage": "done" if stage == "interactions" else stage,
        "directory_started": saved["directory_started"],
        "mids": mids,
        "content_position": position,
    }


@dataclass(frozen=True)
class FollowingProgress:
    stage: str
    people: int
    content_done: int
    content_total: int

    @property
    def done(self):
        return self.stage == "done"

    @property
    def notice(self):
        if self.stage == "directory":
            return f"正在分页收齐关注名单 · 已读取 {self.people} 人"
        return f"{self.people} 位关注者 · 最新内容页 {self.content_done}/{self.content_total}" + (
            " · 本轮采集完成" if self.done else " · 可暂停，已完成部分会保存"
        )


class FollowingCollection:
    def __init__(self, directory, creators, *, cache=None, clock=time.time):
        self.directory, self.creators, self.cache, self.clock = (
            directory,
            creators,
            cache,
            clock,
        )
        self.state = None
        if cache is not None:
            saved = cache.get("following_collection", "current", max_age=None)
            if saved is not None:
                try:
                    decoded = _decode_checkpoint(saved)
                    if decoded != saved:
                        self._save(decoded)
                    else:
                        self.state = decoded
                except (KeyError, TypeError, ValueError):
                    log.warning("Discarded invalid following collection checkpoint")
        if self.state is not None:
            self._repair_missing_pages()

    def _save(self, state):
        if self.cache is not None:
            self.cache.put_many({("following_collection", "current"): state})
        self.state = state

    def progress(self):
        state = self.state
        if state is None:
            return None
        view = self.directory.views.get("followings")
        return FollowingProgress(
            state["stage"],
            len(state["mids"]) if state["stage"] != "directory" else len(view.items) if view else 0,
            state["content_position"],
            2 * len(state["mids"]),
        )

    def _restart(self):
        self._save(
            {
                "started_at": self.clock(),
                "stage": "directory",
                "directory_started": False,
                "mids": [],
                "content_position": 0,
            }
        )

    def _repair_missing_pages(self):
        """A checkpoint never authorizes skipping missing or corrupt content."""
        state = self.state
        if state["stage"] == "directory":
            return
        if self.directory.views.get("followings") is None:
            self._restart()
            return
        for position in range(state["content_position"]):
            mid, kind = state["mids"][position // 2], KINDS[position % 2]
            if self.creators.cached_view(mid, kind) is None:
                self._save(
                    {
                        **state,
                        "stage": "content",
                        "content_position": position,
                    }
                )
                return

    async def run_round(self, account_id, *, restart=False, on_update=None):
        if self.cache is not None and account_id != self.cache.account_id:
            raise DownloadError("关注采集账号不匹配，未读取或覆盖缓存")
        if restart or self.state is None:
            self._restart()
        for _ in range(ROUND_STEPS):
            state = dict(self.state)
            if state["stage"] == "done":
                break
            if state["stage"] == "directory":
                current = self.directory.views.get("followings")
                more = state["directory_started"] and current is not None and current.has_more
                # If a completed directory was committed before its work-plan
                # checkpoint, consume it instead of restarting at page one.
                if not state["directory_started"] or more or current is None:
                    current = await self.directory.read("followings", account_id, more=more)
                state["directory_started"] = True
                if not current.has_more:
                    state.update(stage="content", mids=[int(row.key) for row in current.items])
            elif state["stage"] == "content":
                position = state["content_position"]
                if position < 2 * len(state["mids"]):
                    mid, kind = state["mids"][position // 2], KINDS[position % 2]
                    cached = self.creators.cached_view(mid, kind)
                    if cached is None or cached.checked_at < state["started_at"]:
                        await self.creators.read_page(mid, kind, refresh=True)
                    state["content_position"] += 1
                if state["content_position"] == 2 * len(state["mids"]):
                    state["stage"] = "done"
            self._save(state)
            if on_update:
                on_update(self.progress())
        progress = self.progress()
        log.info(
            "Following collection stage=%s content=%s/%s",
            progress.stage,
            progress.content_done,
            progress.content_total,
        )
        return progress
