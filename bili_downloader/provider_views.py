"""Preserve display fields omitted by bpi-py 0.3.1, without extra requests.

The pinned SDK owns parameters, authentication, transport and API errors. These
two response adapters retain message emoji maps and the first forwarded item.
"""

import json
import logging

from bpi.dynamic.models import DynamicAllData, DynamicAllItem
from bpi.dynamic.params import DEFAULT_ALL_FEATURES, DEFAULT_ALL_WEB_LOCATION, all_query
from bpi.message.models import PrivateMessage, SessionMessagesData
from bpi.message.params import session_messages_query
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

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


_MESSAGES = TypeAdapter(MessagePage)
_FEED = TypeAdapter(FeedPage)


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


async def feed_page(sdk, *, offset=None):
    return await sdk._get_payload(
        "/x/polymer/web-dynamic/v1/feed/all",
        all_query(
            features=DEFAULT_ALL_FEATURES,
            web_location=DEFAULT_ALL_WEB_LOCATION,
            host_mid=None,
            offset=offset,
            update_baseline=None,
        ),
        _FEED,
    )
