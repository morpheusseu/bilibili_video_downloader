"""Resumable latest-page collection, using the existing page and interaction owners.

The work plan is only a checkpoint. AccountContent owns the directory,
CreatorContent owns pages/cursors and the account-wide video observations.
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


@dataclass(frozen=True)
class FollowingProgress:
    stage: str
    people: int
    content_done: int
    content_total: int
    interactions_done: int
    interactions_total: int

    @property
    def done(self):
        return self.stage == "done"

    @property
    def notice(self):
        if self.stage == "directory":
            return f"正在分页收齐关注名单 · 已读取 {self.people} 人"
        return (
            f"{self.people} 位关注者 · 最新内容页 {self.content_done}/{self.content_total}"
            + (
                " · 内容收齐后核验个人视频互动"
                if self.stage == "content"
                else f" · 个人视频互动 {self.interactions_done}/{self.interactions_total}"
            )
            + (" · 本轮采集完成" if self.done else " · 可暂停，已完成部分会保存")
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
                    mids, aids = saved["mids"], saved["aids"]
                    if (
                        not valid_time(saved["started_at"])
                        or type(saved["directory_started"]) is not bool
                        or saved["stage"] not in {"directory", "content", "interactions", "done"}
                        or any(
                            not isinstance(ids, list)
                            or any(type(item) is not int or item <= 0 for item in ids)
                            or len(set(ids)) != len(ids)
                            for ids in (mids, aids)
                        )
                        or type(saved["content_position"]) is not int
                        or not 0 <= saved["content_position"] <= 2 * len(mids)
                        or type(saved["interaction_position"]) is not int
                        or not 0 <= saved["interaction_position"] <= len(aids)
                        or (
                            saved["stage"] in {"interactions", "done"}
                            and saved["content_position"] != 2 * len(mids)
                        )
                        or (saved["stage"] == "done" and saved["interaction_position"] != len(aids))
                    ):
                        raise ValueError("Invalid collection checkpoint")
                    self.state = saved
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
            state["interaction_position"],
            len(state["aids"]),
        )

    def _restart(self):
        self._save(
            {
                "started_at": self.clock(),
                "stage": "directory",
                "directory_started": False,
                "mids": [],
                "content_position": 0,
                "aids": [],
                "interaction_position": 0,
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
                        "aids": [],
                        "interaction_position": 0,
                    }
                )
                return
        for position, aid in enumerate(state["aids"][: state["interaction_position"]]):
            if self.creators.video_observation(aid) is None:
                self._save({**state, "stage": "interactions", "interaction_position": position})
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
                    state.update(
                        stage="interactions",
                        aids=list(
                            dict.fromkeys(
                                aid
                                for mid in state["mids"]
                                for kind in KINDS
                                for aid in self.creators.video_ids(mid, kind)
                            )
                        ),
                    )
            else:
                position = state["interaction_position"]
                if position < len(state["aids"]):
                    await self.creators.verify_video(state["aids"][position])
                    state["interaction_position"] += 1
                if state["interaction_position"] == len(state["aids"]):
                    state["stage"] = "done"
            self._save(state)
            if on_update:
                on_update(self.progress())
        progress = self.progress()
        log.info(
            "Following collection stage=%s content=%s/%s interactions=%s/%s",
            progress.stage,
            progress.content_done,
            progress.content_total,
            progress.interactions_done,
            progress.interactions_total,
        )
        return progress
