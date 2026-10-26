"""One in-memory favorite-folder view per login; no provider writes or disk cache."""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, replace

from bpi.fav.models import CreatedFolderItem, FavListMedia

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


class FavoriteLibrary:
    def __init__(self, fav, *, clock=time.monotonic):
        self.fav, self.clock = fav, clock
        self.clear()

    def clear(self):
        self.view = FavoriteView()
        self.next_page = 1
        self.body_at = 0.0
        self.mtime = None
        self.folders_at = None

    async def refresh(self, mid: int, *, force=False) -> FavoriteView:
        # The GUI checks the selected folder every minute. The folder directory
        # has its own ten-minute lifetime, including a successfully empty list.
        read_folders = force or self.folders_at is None or self.clock() - self.folders_at >= 600
        folders = (
            tuple((await self.fav.created_list(up_mid=mid)).list)
            if read_folders
            else self.view.folders
        )
        ids = {folder.id for folder in folders}
        selected = self.view.folder_id if self.view.folder_id in ids else None
        if selected is None and folders:
            selected = folders[0].id
        if selected is None:
            self.view = FavoriteView(folders=folders, refreshed=True)
            self.next_page, self.body_at, self.mtime = 1, 0.0, None
        else:
            await self.open(selected, force=force, folders=folders)
        # Do not make a new directory fresh if its selected-folder check failed
        # or was cancelled; the last successful view remains authoritative.
        if read_folders:
            self.folders_at = self.clock()
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

    async def open(self, folder_id: int, *, force=False, folders=None) -> FavoriteView:
        folders = self.view.folders if folders is None else folders
        if folder_id not in {folder.id for folder in folders}:
            raise DownloadError("此收藏夹已不在当前账号列表中，请刷新收藏夹")
        keys, digest = await self.identifiers(folder_id)
        if (
            folder_id == self.view.folder_id
            and self.view.digest == digest
            and not force
            and self.clock() - self.body_at < 600
        ):
            self.view = replace(self.view, folders=folders, refreshed=False)
            log.info("Favorites body=reused items=%s", len(self.view.items))
            return self.view
        return await self.first_page(folder_id, folders, keys, digest)

    async def first_page(self, folder_id, folders, keys, digest):
        data = await self.fav.list_detail(media_id=folder_id, page=1, page_size=20)
        if data.info.id != folder_id:
            raise DownloadError("收藏夹返回了不同的 ID，未替换已有缓存")
        items = self.unique_items((), data.medias, data.has_more)
        observed = {(item.type_name, item.id) for item in items}
        # IDs are an invalidation hint, not a transactional server snapshot.
        # If the folder changed between calls, render this page but do not reuse it as unchanged.
        if not observed <= keys or (not data.has_more and observed != keys):
            digest = ""
        self.view = FavoriteView(folders, folder_id, items, data.has_more, digest, True)
        self.next_page, self.body_at, self.mtime = 2, self.clock(), data.info.mtime
        log.info("Favorites body=fetched items=%s", len(items))
        return self.view

    async def more(self) -> FavoriteView:
        current = self.view
        if current.folder_id is None or not current.has_more:
            return replace(current, refreshed=False)
        keys, digest = await self.identifiers(current.folder_id)
        if current.digest and digest != current.digest:
            return await self.first_page(current.folder_id, current.folders, keys, digest)
        data = await self.fav.list_detail(
            media_id=current.folder_id, page=self.next_page, page_size=20
        )
        if data.info.id != current.folder_id:
            raise DownloadError("收藏夹返回了不同的 ID，未替换已有缓存")
        if data.info.mtime != self.mtime:
            return await self.first_page(current.folder_id, current.folders, keys, digest)
        items = self.unique_items(current.items, data.medias, data.has_more)
        observed = {(item.type_name, item.id) for item in items}
        if not observed <= keys or (not data.has_more and observed != keys):
            digest = ""
        self.view = replace(
            current, items=items, has_more=data.has_more, digest=digest, refreshed=True
        )
        self.next_page += 1
        return self.view

    @staticmethod
    def unique_items(existing, page, has_more):
        seen = {(item.type_name, item.id) for item in existing}
        result = list(existing)
        for item in page:
            key = (item.type_name, item.id)
            if key not in seen:
                seen.add(key)
                result.append(item)
        if has_more and len(result) == len(existing):
            raise DownloadError("收藏夹分页未推进，已保留原缓存；请手动刷新")
        return tuple(result)
