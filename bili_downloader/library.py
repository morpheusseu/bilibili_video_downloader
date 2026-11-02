"""Account-scoped, on-demand favorite pages; the provider owns current contents."""

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, replace
from functools import cached_property

from bpi.fav.models import CreatedFolderItem, FavListMedia, ResourceInfoItem

from .sources import DownloadError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FavoriteView:
    folders: tuple[CreatedFolderItem, ...] = ()
    folder_id: int | None = None
    items: tuple[FavListMedia, ...] = ()
    has_more: bool = False
    digest: str = ""
    refreshed: bool = False
    cached: bool = False


def unique_items(existing, page, has_more):
    rows = {(item.type_name, item.id): item for item in existing}
    old_size = len(rows)
    for item in page:
        rows[(item.type_name, item.id)] = item
    if has_more and len(rows) == old_size:
        raise DownloadError("收藏夹分页未推进，已保留原缓存；请手动刷新")
    return tuple(rows.values())


@dataclass(frozen=True)
class FolderPages:
    pages: tuple[tuple[FavListMedia, ...], ...]
    has_more: bool
    digest: str
    body_at: float
    mtime: int

    @cached_property
    def items(self):
        return unique_items((), (item for page in self.pages for item in page), False)


class FavoriteLibrary:
    def __init__(self, fav, *, cache=None, clock=time.time):
        self.fav, self.cache, self.clock = fav, cache, clock
        self.view = FavoriteView()
        self.states = {}
        self.metadata = {}
        self.folders_at = None
        if cache is not None:
            self._restore_directory()

    def _restore_directory(self):
        try:
            record = self.cache.get("favorites", "directory", max_age=None)
            if record is None:
                return
            folders = tuple(CreatedFolderItem.model_validate(row) for row in record["folders"])
            if (
                len({row.id for row in folders}) != len(folders)
                or any(row.mid != self.cache.account_id or row.id <= 0 for row in folders)
                or not self._valid_time(record["checked_at"])
            ):
                raise ValueError("Invalid favorite directory")
            self.view = FavoriteView(folders=folders)
            self.folders_at = record["checked_at"]
            self.cached_view(record["selected"])
        except (DownloadError, ValueError, TypeError, KeyError):
            log.warning("Discarded invalid favorite directory cache")
            self.view, self.folders_at = FavoriteView(), None

    @staticmethod
    def _valid_time(value):
        return (
            isinstance(value, (float, int))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value >= 0
        )

    def _state(self, folder_id):
        if folder_id in self.states or self.cache is None:
            return self.states.get(folder_id)
        try:
            record = self.cache.get("favorite_folder", str(folder_id), max_age=None)
            if record is None:
                return None
            count, digest = record["page_count"], record["digest"]
            if (
                type(count) is not int
                or count < 1
                or type(record["has_more"]) is not bool
                or not self._valid_time(record["body_at"])
                or type(record["mtime"]) is not int
                or not isinstance(digest, str)
                or (
                    digest
                    and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest))
                )
            ):
                raise ValueError("Invalid favorite frontier")
            pages, observed = [], ()
            for page in range(1, count + 1):
                rows = self.cache.get("favorite_page", f"{folder_id}:{page}", max_age=None)
                if not isinstance(rows, list) or len(rows) > 20:
                    raise ValueError("Missing favorite page")
                parsed = tuple(FavListMedia.model_validate(row) for row in rows)
                observed = unique_items(observed, parsed, page < count or record["has_more"])
                pages.append(parsed)
            state = FolderPages(
                tuple(pages), record["has_more"], digest, record["body_at"], record["mtime"]
            )
            self.states[folder_id] = state
            return state
        except (DownloadError, ValueError, TypeError, KeyError):
            log.warning("Discarded incomplete favorite pages; cursor not restored")
            return None

    def cached_view(self, folder_id=None):
        """Select cached pages synchronously, before any network wait or validation."""
        ids = {folder.id for folder in self.view.folders}
        folder_id = folder_id if folder_id in ids else self.view.folder_id
        if folder_id not in ids:
            folder_id = self.view.folders[0].id if self.view.folders else None
        state = self._state(folder_id) if folder_id is not None else None
        self.view = self._view(self.view.folders, folder_id, state)
        return replace(self.view, cached=True)

    @staticmethod
    def _view(folders, folder_id, state, *, refreshed=False):
        return FavoriteView(
            folders,
            folder_id,
            state.items if state else (),
            state.has_more if state else False,
            state.digest if state else "",
            refreshed,
        )

    def _commit(self, folders, folder_id, state, *, folders_at, changed=()):
        previous = self._state(folder_id) if folder_id is not None else None
        if self.cache is not None:
            values = {
                ("favorites", "directory"): {
                    "folders": [row.model_dump(mode="json") for row in folders],
                    "selected": folder_id,
                    "checked_at": folders_at,
                }
            }
            remove = []
            if state is not None:
                values[("favorite_folder", str(folder_id))] = {
                    "page_count": len(state.pages),
                    "has_more": state.has_more,
                    "digest": state.digest,
                    "body_at": state.body_at,
                    "mtime": state.mtime,
                }
                for page in changed:
                    values[("favorite_page", f"{folder_id}:{page}")] = [
                        row.model_dump(mode="json") for row in state.pages[page - 1]
                    ]
                if previous is not None:
                    remove = [
                        ("favorite_page", f"{folder_id}:{page}")
                        for page in range(len(state.pages) + 1, len(previous.pages) + 1)
                    ]
            self.cache.put_many(values, remove=remove)
        if state is not None:
            self.states[folder_id] = state
        self.folders_at = folders_at
        self.view = self._view(
            folders, folder_id, state, refreshed=bool(changed) or folder_id is None
        )
        return self.view

    async def refresh(self, mid: int, *, force=False) -> FavoriteView:
        if self.cache is not None and mid != self.cache.account_id:
            raise DownloadError("收藏夹账号不匹配，未读取或覆盖缓存")
        now = self.clock()
        read_folders = force or self.folders_at is None or not 0 <= now - self.folders_at < 600
        folders = (
            tuple((await self.fav.created_list(up_mid=mid)).list)
            if read_folders
            else self.view.folders
        )
        if any(folder.mid != mid for folder in folders) or len({row.id for row in folders}) != len(
            folders
        ):
            raise DownloadError("收藏夹列表账号或 ID 不匹配，保留原缓存")
        ids = {folder.id for folder in folders}
        selected = (
            self.view.folder_id
            if self.view.folder_id in ids
            else folders[0].id
            if folders
            else None
        )
        checked_at = now if read_folders else self.folders_at
        if selected is None:
            self._commit(folders, None, None, folders_at=checked_at)
        else:
            await self.open(selected, force=force, folders=folders, folders_at=checked_at)
        log.info(
            "Favorites directory=%s folders=%s force=%s",
            "fetched" if read_folders else "reused",
            len(folders),
            bool(force),
        )
        return self.view

    async def identifiers(self, folder_id):
        data = await self.fav.resource_ids(media_id=folder_id)
        keys = {(item.type_name, item.id) for item in data}
        if len(keys) != len(data):
            raise DownloadError("收藏夹标识列表含重复项，未替换缓存；请稍后手动刷新")
        digest = hashlib.sha256(
            json.dumps(sorted(keys), separators=(",", ":")).encode()
        ).hexdigest()
        return keys, digest

    async def complete_metadata(self, items):
        """Batch only genuinely missing public fields; retain folder-specific facts."""
        missing = {(row.type_name, row.id) for row in items if not row.title or not row.cover}
        now = self.clock()
        infos = {}
        for key in missing:
            saved = self.metadata.get(key)
            if saved is None and self.cache is not None:
                record = self.cache.get("favorite_metadata", f"{key[0]}:{key[1]}", max_age=None)
                if record is not None:
                    try:
                        saved = (
                            record["checked_at"],
                            ResourceInfoItem.model_validate(record["info"]),
                        )
                        if (saved[1].type_name, saved[1].id) != key:
                            raise ValueError("Mismatched resource metadata")
                    except (ValueError, KeyError, TypeError):
                        saved = None
            if saved is not None and self._valid_time(saved[0]) and 0 <= now - saved[0] < 600:
                infos[key] = saved[1]
        pending = sorted(missing - infos.keys())
        for start in range(0, len(pending), 20):
            batch = pending[start : start + 20]
            rows = await self.fav.resource_infos(
                resources=",".join(f"{rid}:{kind}" for kind, rid in batch)
            )
            keys = [(row.type_name, row.id) for row in rows]
            if len(set(keys)) != len(keys) or not set(keys) <= set(batch):
                raise DownloadError("批量资源信息包含重复或不匹配的标识，已保留原缓存")
            values = {}
            for row in rows:
                key = (row.type_name, row.id)
                infos[key] = row
                values[("favorite_metadata", f"{key[0]}:{key[1]}")] = {
                    "checked_at": now,
                    "info": row.model_dump(mode="json"),
                }
            if self.cache is not None:
                self.cache.put_many(values)
            self.metadata.update({key: (now, infos[key]) for key in keys})
        # Missing results can be removed/private videos. Keep their placeholders;
        # do not infer membership, ordering or fav_time from this aggregate API.
        return tuple(
            row.model_copy(
                update={
                    "title": row.title or infos[key].title,
                    "cover": row.cover or infos[key].cover,
                }
            )
            if (key := (row.type_name, row.id)) in infos
            else row
            for row in items
        )

    async def open(
        self, folder_id: int, *, force=False, folders=None, folders_at=None
    ) -> FavoriteView:
        folders = self.view.folders if folders is None else folders
        folders_at = self.folders_at if folders_at is None else folders_at
        if folder_id not in {folder.id for folder in folders}:
            raise DownloadError("此收藏夹已不在当前账号列表中，请刷新收藏夹")
        state = self._state(folder_id)
        keys, digest = await self.identifiers(folder_id)
        if (
            state is not None
            and state.digest == digest
            and not force
            and 0 <= self.clock() - state.body_at < 600
        ):
            self._commit(folders, folder_id, state, folders_at=folders_at)
            log.info("Favorites body=reused items=%s", len(state.items))
            return self.view
        return await self.first_page(
            folder_id,
            folders,
            keys,
            digest,
            folders_at=folders_at,
            retain=not force,
        )

    async def first_page(self, folder_id, folders, keys, digest, *, folders_at, retain=False):
        data = await self.fav.list_detail(media_id=folder_id, page=1, page_size=20)
        if data.info.id != folder_id:
            raise DownloadError("收藏夹返回了不同的 ID，未替换已有缓存")
        page = await self.complete_metadata(data.medias)
        unique_items((), page, data.has_more)
        previous = self._state(folder_id)
        keep = (
            retain
            and previous is not None
            and previous.digest == digest
            and previous.mtime == data.info.mtime
            and [(r.type_name, r.id) for r in previous.pages[0]]
            == [(r.type_name, r.id) for r in page]
        )
        pages = (page, *previous.pages[1:]) if keep else (page,)
        has_more = previous.has_more if keep and len(pages) > 1 else data.has_more
        state = FolderPages(pages, has_more, digest, self.clock(), data.info.mtime)
        if not self._consistent(state.items, keys, has_more):
            state = replace(state, digest="")
        self._commit(folders, folder_id, state, folders_at=folders_at, changed=(1,))
        log.info("Favorites body=fetched items=%s", len(state.items))
        return self.view

    @staticmethod
    def _consistent(items, keys, has_more):
        observed = {(item.type_name, item.id) for item in items}
        # IDs are an invalidation hint, not a transactional provider snapshot.
        return observed <= keys and (has_more or observed == keys)

    async def more(self) -> FavoriteView:
        current = self.view
        if current.folder_id is None or not current.has_more:
            return replace(current, refreshed=False)
        state = self._state(current.folder_id)
        keys, digest = await self.identifiers(current.folder_id)
        if not state.digest or digest != state.digest:
            return await self.first_page(
                current.folder_id,
                current.folders,
                keys,
                digest,
                folders_at=self.folders_at,
            )
        data = await self.fav.list_detail(
            media_id=current.folder_id,
            page=len(state.pages) + 1,
            page_size=20,
        )
        if data.info.id != current.folder_id:
            raise DownloadError("收藏夹返回了不同的 ID，未替换已有缓存")
        if data.info.mtime != state.mtime:
            return await self.first_page(
                current.folder_id,
                current.folders,
                keys,
                digest,
                folders_at=self.folders_at,
            )
        unique_items(state.items, data.medias, data.has_more)
        page = await self.complete_metadata(data.medias)
        state = replace(
            state,
            pages=(*state.pages, page),
            has_more=data.has_more,
            digest=digest,
        )
        if not self._consistent(state.items, keys, data.has_more):
            state = replace(state, digest="")
        return self._commit(
            current.folders,
            current.folder_id,
            state,
            folders_at=self.folders_at,
            changed=(len(state.pages),),
        )
