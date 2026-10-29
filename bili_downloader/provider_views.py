"""Narrow read adapters for fields/endpoints omitted by bpi-py 0.3.1.

The pinned SDK owns parameters, authentication, transport and API errors. These
response adapters retain emoji maps, forwarded items and cooperative video markers.
Video relationship reads use the same SDK transport and one typed response.
"""

import json
import logging

from bpi._generated.user_models import UserUploadedVideo, UserUploadedVideoList, UserUploadedVideos
from bpi.dynamic.models import DynamicAllData, DynamicAllItem
from bpi.dynamic.params import DEFAULT_ALL_FEATURES, DEFAULT_ALL_WEB_LOCATION, all_query
from bpi.message.models import PrivateMessage, SessionMessagesData
from bpi.message.params import session_messages_query
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictBool,
    StrictInt,
    TypeAdapter,
    ValidationError,
    field_validator,
)

log = logging.getLogger(__name__)


class MessageEmoji(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    text: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=4096)
    size: int = 1


class ReceivedMessage(PrivateMessage):
    emojis: list[MessageEmoji] = Field(default_factory=list)


class MessagePage(SessionMessagesData):
    messages: list[ReceivedMessage] | None = None
    e_infos: JsonValue = None


class FeedItem(DynamicAllItem):
    orig: dict[str, JsonValue] | None = None


class FeedPage(DynamicAllData):
    items: list[FeedItem]


class UploadedVideo(UserUploadedVideo):
    # Recorded /arc/search responses include coauthored videos whose actual
    # uploader differs from the requested space owner. The SDK drops this flag.
    is_union_video: StrictInt = Field(default=0, ge=0, le=1)


class UploadedVideoList(UserUploadedVideoList):
    videos: list[UploadedVideo] = Field(default_factory=list, alias="vlist")


class UploadedVideos(UserUploadedVideos):
    list: UploadedVideoList


class VideoRelationship(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    like: StrictBool | None = None
    coin: StrictInt | None = Field(default=None, ge=0, le=2)

    @field_validator("like", mode="before")
    @classmethod
    def flag(cls, value):
        if type(value) is int and value in (0, 1):
            return bool(value)
        return value


_MESSAGES = TypeAdapter(MessagePage)
_FEED = TypeAdapter(FeedPage)
_VIDEO_RELATION = TypeAdapter(VideoRelationship)
_UPLOADED_VIDEOS = TypeAdapter(UploadedVideos)


async def message_page(sdk, *, talker_id, size=20, begin_seqno=None, end_seqno=None):
    page = await sdk._get_payload(
        "/svr_sync/v1/svr_sync/fetch_session_msgs",
        session_messages_query(talker_id, size, begin_seqno, end_seqno),
        _MESSAGES,
        host="api.vc.bilibili.com",
    )
    emojis = []
    for value in page.e_infos if isinstance(page.e_infos, list) else ():
        try:
            emojis.append(MessageEmoji.model_validate(value))
        except ValidationError:
            # Optional decoration must not reject valid message bodies/cursors.
            log.warning("Ignored invalid optional message emoji metadata")
    for message in page.messages or ():
        if message.msg_type != 1 or message.msg_status:
            continue
        try:
            body = json.loads(message.content)
            text = body.get("content", "") if isinstance(body, dict) else ""
        except (ValueError, TypeError):
            continue
        if isinstance(text, str):
            # Persist only this body's image dependencies, atomically with its cursor.
            message.emojis = [e for e in emojis if e.text in text][:64]
    return page


async def feed_page(sdk, *, offset=None, host_mid=None):
    return await sdk._get_payload(
        "/x/polymer/web-dynamic/v1/feed/all",
        all_query(
            features=DEFAULT_ALL_FEATURES,
            web_location=DEFAULT_ALL_WEB_LOCATION,
            host_mid=host_mid,
            offset=offset,
            update_baseline=None,
        ),
        _FEED,
    )


async def video_relationship(sdk, aid):
    # bpi-py has a separate coin_status call, but this provider read returns
    # both account-specific flags in one request. It never performs an action.
    return await sdk._get_payload(
        "/x/web-interface/archive/relation", {"aid": str(aid)}, _VIDEO_RELATION
    )


async def uploaded_videos(sdk, *, mid, page, page_size):
    # Same pinned SDK endpoint/signing/transport; retain its omitted cooperation
    # marker instead of weakening identity validation for all mismatching rows.
    params = await sdk._sign(
        {
            "mid": str(mid),
            "pn": str(page),
            "ps": str(page_size),
            "order": "pubdate",
            "tid": "0",
        }
    )
    return await sdk._get_payload("/x/space/wbi/arc/search", params, _UPLOADED_VIDEOS)
