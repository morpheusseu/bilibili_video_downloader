"""Plain content and explicit image parts; provider HTML is never rendered."""

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MessageImage:
    url: str
    alt: str
    width: int
    height: int


@dataclass(frozen=True)
class MessageContent:
    text: str
    parts: tuple[str | MessageImage, ...]


def _text(value):
    return value if isinstance(value, str) else ""


def _plain(text):
    return MessageContent(text, (text,))


def message_content(message):
    if message is None:
        return _plain("暂无消息")
    # Withdrawn bodies must not render text, emoji, image URLs or thumbnails.
    if message.msg_status or message.msg_type == 5:
        return _plain("[消息已撤回]")
    kind = message.msg_type
    try:
        body = json.loads(message.content)
        if not isinstance(body, dict):
            raise ValueError
    except (ValueError, TypeError):
        return _plain("[无法解析的文本消息]" if kind == 1 else "[无法解析的消息]")
    if kind == 1:
        text = body.get("content")
        if not isinstance(text, str):
            return _plain("[无法解析的文本消息]")
        emojis = {e.text: e for e in getattr(message, "emojis", ()) if e.text and e.url}
        if not emojis:
            return _plain(text)
        parts, start = [], 0
        pattern = "|".join(re.escape(key) for key in sorted(emojis, key=len, reverse=True))
        for match in re.finditer(pattern, text):
            parts.append(text[start : match.start()])
            emoji = emojis[match.group()]
            size = 72 if emoji.size == 2 else 28
            parts.append(MessageImage(emoji.url, emoji.text, size, size))
            start = match.end()
        parts.append(text[start:])
        return MessageContent(text, tuple(parts))
    if kind == 10:
        fields = [_text(body.get("title")), _text(body.get("text"))]
        for module in body.get("modules", []) if isinstance(body.get("modules"), list) else ():
            if isinstance(module, dict):
                fields.extend((_text(module.get("title")), _text(module.get("detail"))))
        return _plain("\n".join(filter(None, fields)) or "[通知]")
    if kind == 18:
        try:
            fragments = json.loads(body.get("content", ""))
            if not isinstance(fragments, list):
                raise ValueError
            text = "".join(_text(item.get("text")) for item in fragments if isinstance(item, dict))
        except (ValueError, TypeError):
            text = ""
        return _plain(text or "[系统提示]")
    if kind == 11:
        # Provider video-push cards (not msg_source=11). Some contain only a
        # video ID and an optional creator note; never fetch missing details.
        fields = ["[视频推送]", _text(body.get("title")), _text(body.get("desc"))]
        seconds = body.get("times")
        if type(seconds) is int and seconds >= 0:
            fields.append(f"时长 {seconds // 60}:{seconds % 60:02d}")
        bvid, aid = body.get("bvid"), body.get("rid")
        if isinstance(bvid, str) and re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            fields.append(f"https://www.bilibili.com/video/{bvid}")
        elif type(aid) is int and aid > 0:
            fields.append(f"https://www.bilibili.com/video/av{aid}")
        note = body.get("attach_msg")
        if isinstance(note, dict) and _text(note.get("content")):
            fields.append("UP 主赠言：" + note["content"])
        text = "\n".join(filter(None, fields))
        url = _text(body.get("cover"))
        return (
            MessageContent(text, (text + "\n", MessageImage(url, "[视频封面]", 240, 180)))
            if url
            else _plain(text)
        )
    if kind in {2, 6, 7, 13, 14}:
        if kind in {2, 6}:
            text = "[图片]" if kind == 2 else "[表情包]"
            url = _text(body.get("url"))
        else:
            fields = (_text(body.get(key)) for key in ("headline", "title", "author", "desc"))
            text = "\n".join(filter(None, fields)) or "[分享内容]"
            url = _text(body.get({7: "thumb", 13: "pic_url", 14: "cover"}[kind]))
        if not url:
            return _plain(text)
        size = (112, 112) if kind == 6 else (240, 180)
        return MessageContent(text, (text + "\n", MessageImage(url, text, *size)))
    return _plain(f"[暂不支持的消息类型 {kind}]")
