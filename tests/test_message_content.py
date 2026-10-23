import json

import pytest
from conftest import private_message

from bili_downloader.inbox import message_text
from bili_downloader.message_content import MessageImage, message_content
from bili_downloader.provider_views import ReceivedMessage


@pytest.mark.parametrize(
    ("kind", "body", "expected"),
    [
        (
            10,
            {"title": "通知标题", "text": "正文", "modules": [{"title": "项目", "detail": "详情"}]},
            "通知标题\n正文\n项目\n详情",
        ),
        (18, {"content": json.dumps([{"text": "请注意"}, {"text": "安全"}])}, "请注意安全"),
        (
            7,
            {"title": "分享视频", "author": "作者", "thumb": "https://i0.hdslb.com/cover.png"},
            "分享视频\n作者",
        ),
        (13, {"title": "图片卡片", "pic_url": "https://i0.hdslb.com/card.png"}, "图片卡片"),
        (
            14,
            {"title": "直播", "desc": "直播说明", "cover": "https://i0.hdslb.com/live.png"},
            "直播\n直播说明",
        ),
        (2, {"url": "https://i0.hdslb.com/photo.png"}, "[图片]"),
        (6, {"url": "https://i0.hdslb.com/sticker.png"}, "[表情包]"),
    ],
)
def test_common_message_types_keep_readable_text_and_explicit_image_parts(kind, body, expected):
    message = ReceivedMessage.model_validate(
        private_message(1, msg_type=kind, content=json.dumps(body))
    )
    content = message_content(message)
    assert message_text(message) == content.text == expected
    pictures = [part for part in content.parts if isinstance(part, MessageImage)]
    assert len(pictures) == (0 if kind in {10, 18} else 1)
    assert all(part.width <= 240 and part.height <= 180 for part in pictures)


@pytest.mark.parametrize("status", [1, 2])
def test_withdrawn_message_never_exposes_an_image_or_emoji(status):
    message = ReceivedMessage.model_validate(
        private_message(
            1,
            msg_type=6,
            msg_status=status,
            content=json.dumps({"url": "https://i0.hdslb.com/secret.png"}),
        )
    )
    content = message_content(message)
    assert content.text == "[消息已撤回]" and content.parts == (content.text,)


@pytest.mark.parametrize("body", ["invalid json", "[]", '{"content":null}'])
def test_malformed_messages_remain_readable_fallbacks(body):
    message = ReceivedMessage.model_validate(private_message(1, content=body))
    assert "无法解析" in message_text(message)


def test_inline_emoji_preserves_plain_text_and_does_not_guess_missing_images():
    text = "你好[热词系列测试]<b>不是 HTML</b>[未知表情]"
    message = ReceivedMessage.model_validate(
        private_message(
            1,
            content=json.dumps({"content": text}),
            emojis=[
                {"text": "[热词系列测试]", "url": "https://i0.hdslb.com/emoji.png", "size": 2},
            ],
        )
    )
    content = message_content(message)
    assert content.text == text
    assert len([part for part in content.parts if isinstance(part, MessageImage)]) == 1
    assert content.parts[-1] == "<b>不是 HTML</b>[未知表情]"
