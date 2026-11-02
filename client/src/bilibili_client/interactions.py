"""Account-local observations, not an exhaustive history of personal actions.

Recent lists discover positive facts. Falling out of a finite list only makes
the last fact uncertain; exact negatives come from a target relationship read.
All observations and discovery checkpoints share the account cache transaction.
"""

import logging
import math
import time
from dataclasses import asdict, dataclass, replace

from .provider_views import (
    VideoRelationship,
    own_dynamic_page,
    recent_coined_videos,
    recent_liked_videos,
    video_relationship,
)
from .sources import DownloadError

log = logging.getLogger(__name__)
INTERACTION_AGE = 600


def valid_time(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


@dataclass(frozen=True)
class Observation:
    value: bool
    checked_at: float
    uncertain: bool = False

    @classmethod
    def from_record(cls, row):
        value = cls(**row)
        if (
            type(value.value) is not bool
            or type(value.uncertain) is not bool
            or not valid_time(value.checked_at)
        ):
            raise ValueError("Invalid personal observation")
        return value

    def current(self, now):
        return not self.uncertain and 0 <= now - self.checked_at < INTERACTION_AGE


class PersonalInteractions:
    def __init__(self, sdk, *, cache=None, clock=time.time):
        self.sdk, self.cache, self.clock = sdk, cache, clock
        self.videos, self.reposts, self.sources = {}, {}, {}

    def video(self, aid):
        if aid not in self.videos:
            observations = {}
            if self.cache is not None:
                try:
                    record = self.cache.get("video_interaction", str(aid), max_age=None)
                    if record is not None:
                        # One conversion of the previously saved paired snapshot.
                        if "state" in record:
                            state = VideoRelationship.model_validate(record["state"])
                            stamp = record["checked_at"]
                            if not valid_time(stamp):
                                raise ValueError("Invalid saved observation time")
                            record = {
                                field: asdict(Observation(value, stamp))
                                for field, value in (
                                    ("liked", state.like),
                                    ("coined", state.coin > 0 if state.coin is not None else None),
                                )
                                if value is not None
                            }
                            self.cache.put_many({("video_interaction", str(aid)): record})
                        if set(record) - {"liked", "coined"}:
                            raise ValueError("Invalid observation fields")
                        observations = {
                            field: Observation.from_record(row) for field, row in record.items()
                        }
                except (DownloadError, ValueError, TypeError, KeyError):
                    log.warning("Discarded invalid video interaction cache")
            self.videos[aid] = observations
        return self.videos[aid]

    def repost(self, dynamic_id):
        if dynamic_id not in self.reposts:
            value = None
            if self.cache is not None:
                try:
                    row = self.cache.get("dynamic_repost", dynamic_id, max_age=None)
                    value = Observation.from_record(row) if row is not None else None
                except (DownloadError, ValueError, TypeError, KeyError):
                    log.warning("Discarded invalid repost observation")
            self.reposts[dynamic_id] = value
        return self.reposts[dynamic_id]

    def needs_video(self, aid):
        values = self.video(aid)
        return any(
            field not in values or not values[field].current(self.clock())
            for field in ("liked", "coined")
        )

    async def verify_video(self, aid, *, force=False):
        if not force and not self.needs_video(aid):
            return
        state = await video_relationship(self.sdk, aid)
        if state.like is None and state.coin is None:
            raise DownloadError("互动接口未返回可确认状态，保留上次状态；没有自动重试")
        values = dict(self.video(aid))
        for field, value in (
            ("liked", state.like),
            ("coined", state.coin > 0 if state.coin is not None else None),
        ):
            if value is not None:
                values[field] = Observation(value, self.clock())
        if self.cache is not None:
            self.cache.put_many(
                {("video_interaction", str(aid)): {k: asdict(v) for k, v in values.items()}}
            )
        self.videos[aid] = values

    def source(self, kind):
        if kind not in self.sources:
            value = None
            if self.cache is not None:
                try:
                    value = self.cache.get("recent_actions", kind, max_age=None)
                    if value is not None and (
                        not valid_time(value["checked_at"])
                        or type(value["blocked"]) is not bool
                        or not isinstance(value["targets"], list)
                        or any(
                            not isinstance(key, str) or not key.isdecimal() or int(key) <= 0
                            for key in value["targets"]
                        )
                        or len(set(value["targets"])) != len(value["targets"])
                    ):
                        raise ValueError("Invalid recent list checkpoint")
                except (DownloadError, ValueError, TypeError, KeyError):
                    log.warning("Discarded invalid recent list checkpoint")
                    value = None
            self.sources[kind] = value
        return self.sources[kind]

    async def refresh_recent(self, account_id, *, force=False, check_current=None):
        if self.cache is not None and self.cache.account_id != account_id:
            raise DownloadError("近期操作账号不匹配，未读取缓存")
        refreshed = False
        for kind in ("liked", "coined", "reposted"):
            if check_current:
                check_current()
            previous = self.source(kind)
            if (
                not force
                and previous
                and (
                    previous["blocked"]
                    or 0 <= self.clock() - previous["checked_at"] < INTERACTION_AGE
                )
            ):
                continue
            try:
                if kind == "liked":
                    targets = {
                        str(row.aid) for row in await recent_liked_videos(self.sdk, account_id)
                    }
                elif kind == "coined":
                    targets = {
                        str(row.aid) for row in await recent_coined_videos(self.sdk, account_id)
                    }
                else:
                    page = await own_dynamic_page(self.sdk, account_id)
                    targets = set()
                    for row in page.items:
                        author = row.modules.get("module_author", {})
                        if row.visible and (
                            not isinstance(author, dict) or author.get("mid") != account_id
                        ):
                            raise DownloadError("本人动态身份不匹配，未更新转发索引")
                        if not row.visible or row.type_field != "DYNAMIC_TYPE_FORWARD":
                            continue
                        key = row.orig.get("id_str") if isinstance(row.orig, dict) else None
                        if not isinstance(key, str) or not key.isdecimal() or int(key) <= 0:
                            raise DownloadError("转发未包含有效原动态标识，保留原索引")
                        targets.add(key)
                if check_current:
                    check_current()
                self._commit_recent(kind, targets, previous)
                refreshed = True
            except Exception:
                if check_current:
                    check_current()
                # A refused/changed optional surface is not an automatic retry
                # loop. An explicit refresh can try it again.
                blocked = {**(previous or {"checked_at": 0, "targets": []}), "blocked": True}
                self.sources[kind] = blocked
                if self.cache is not None:
                    self.cache.put_many({("recent_actions", kind): blocked})
                raise
        return refreshed

    def _commit_recent(self, kind, targets, previous):
        stamp = self.clock()
        updated = {}
        vanished = set(previous["targets"]) - targets if previous else set()
        for key in targets | vanished:
            if kind == "reposted":
                old = self.repost(key)
                value = (
                    Observation(True, stamp)
                    if key in targets
                    else (replace(old, uncertain=True) if old else None)
                )
                if value is not None:
                    updated[key] = value
            else:
                aid = int(key)
                value = dict(self.video(aid))
                if key in targets:
                    value[kind] = Observation(True, stamp)
                elif kind in value and value[kind].value:
                    value[kind] = replace(value[kind], uncertain=True)
                updated[aid] = value
        checkpoint = {"checked_at": stamp, "targets": sorted(targets), "blocked": False}
        records = {("recent_actions", kind): checkpoint}
        records.update(
            {
                ("dynamic_repost" if kind == "reposted" else "video_interaction", str(key)): (
                    asdict(value)
                    if kind == "reposted"
                    else {field: asdict(item) for field, item in value.items()}
                )
                for key, value in updated.items()
            }
        )
        if self.cache is not None:
            self.cache.put_many(records)
        (self.reposts if kind == "reposted" else self.videos).update(updated)
        self.sources[kind] = checkpoint
        log.info(
            "Recent interaction index kind=%s returned=%s uncertain=%s",
            kind,
            len(targets),
            len(vanished),
        )

    def notice(self):
        blocked = [
            kind
            for kind in ("liked", "coined", "reposted")
            if (self.source(kind) or {}).get("blocked")
        ]
        if blocked:
            return "部分近期操作未能读取，已停止自动重试；可点“检查近期操作”重试。"
        return "近期列表仅发现已操作内容，不代表完整历史；列表缺失不算取消操作。"
