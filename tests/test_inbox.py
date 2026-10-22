import json

import pytest
from conftest import inbox_provider, private_message

from bili_downloader.inbox import message_text
from bili_downloader.sources import DownloadError


async def test_read_only_inbox_categories_pagination_and_selected_history():
    provider, _ = inbox_provider(21)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        view = await api.inbox.refresh(42)
        assert len(view.conversations) == 21 and view.has_more
        assert all(row.name.startswith("测试联系人") for row in view.conversations)
        assert not any(r.url.path.endswith("fetch_session_msgs") for r in provider.requests)
        complete = await api.inbox.more(42)
        assert len(complete.conversations) == 22 and not complete.has_more
        sessions = [r for r in provider.requests if r.url.path.endswith("get_sessions")]
        assert [r.url.params["session_type"] for r in sessions] == ["1", "2", "1"]
        assert sessions[-1].url.params["end_ts"] == "2999981"
        thread = await api.inbox.open(7, 42)
        assert [m.msg_seqno for m in thread.messages] == list(range(6, 26)) and thread.has_more
        calls = len(provider.requests)
        assert await api.inbox.open(7, 42) is thread and len(provider.requests) == calls
        older = await api.inbox.older(42)
        assert [m.msg_seqno for m in older.messages] == list(range(1, 26)) and not older.has_more
        assert provider.requests[-1].url.params["end_seqno"] == "6"
        assert {
            r.url.params["talker_id"]
            for r in provider.requests
            if r.url.path.endswith("fetch_session_msgs")
        } == {"7"}
        assert all(r.method == "GET" for r in provider.requests)
        assert api.inbox.view.conversations[0].session.unread_count == 2
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        assert not api.inbox.view.conversations and api.inbox.thread is None


async def test_nonadvancing_pages_keep_last_successful_private_state():
    provider, state = inbox_provider(21)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        view = await api.inbox.refresh(42)
        state["repeat_sessions"] = True
        with pytest.raises(DownloadError, match="会话分页未推进"):
            await api.inbox.more(42)
        assert api.inbox.view is view
        thread = await api.inbox.open(7, 42)
        state["repeat_messages"] = True
        with pytest.raises(DownloadError, match="消息分页未推进"):
            await api.inbox.older(42)
        assert api.inbox.thread is thread


async def test_names_are_optional_but_wrong_thread_and_risk_fail_closed():
    provider, state = inbox_provider()
    state["names_fail"] = True
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        view = await api.inbox.refresh(42)
        assert not view.names_available and view.conversations[0].name == "UID 7"
        thread = await api.inbox.open(7, 42)
        state["messages"][-1]["receiver_id"] = 123456
        with pytest.raises(DownloadError, match="身份"):
            await api.inbox.open(7, 42, refresh=True)
        assert api.inbox.thread is thread
        state["risk"] = True
        with pytest.raises(DownloadError, match="风控"):
            await api.inbox.refresh(42)
        count = len(provider.requests)
        with pytest.raises(DownloadError, match="风控"):
            await api.inbox.refresh(42)
        assert len(provider.requests) == count and api.inbox.view is view
        api.clear_session()
        assert not api.inbox.view.conversations and not api.inbox.profiles


async def test_private_write_allowlist_rejects_unrelated_mutations_and_ack():
    provider, _ = inbox_provider()
    async with provider.api() as api:
        for method, path in [
            ("POST", "/web_im/v1/web_im/delete_msg"),
            ("GET", "/session_svr/v1/session_svr/update_ack"),
        ]:
            with pytest.raises(DownloadError, match="拒绝"):
                await api.http.request(method, "https://api.vc.bilibili.com" + path)
        assert not provider.requests and api.gate.count == 0


def test_withdrawn_and_unrecognized_messages_never_expose_raw_payloads():
    from bpi.message.models import PrivateMessage

    for changes in (
        {"msg_status": 1},
        {"msg_type": 5},
        {"msg_type": 2},
        {"msg_type": 999},
        {"content": "private malformed marker"},
    ):
        payload = {"content": json.dumps({"content": "withdrawn marker"}), **changes}
        row = PrivateMessage.model_validate(private_message(1, **payload))
        assert "marker" not in message_text(row)
    literal = PrivateMessage.model_validate(
        private_message(1, content=json.dumps({"content": "<b>纯文本</b>\n第二行"}))
    )
    assert message_text(literal) == "<b>纯文本</b>\n第二行"
    assert message_text(None) == "暂无消息"


@pytest.mark.parametrize("mode", ["ok", "timeout", "missing_key"])
async def test_single_sdk_send_is_confirmed_by_receipt_or_new_message_readback(mode):
    provider, state = inbox_provider()
    state["send_mode"] = mode
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42; bili_jct=synthetic-csrf")
        await api.inbox.refresh(42)
        receipt = await api.send_message(7, "收到，谢谢！\n第二行 <plain>")
        assert receipt.confirmed and receipt.message_key == 1026
        assert receipt.history.messages[-1].sender_uid == 42
        assert message_text(receipt.history.messages[-1]) == "收到，谢谢！\n第二行 <plain>"
        assert api.inbox.pending is None and state["posts"] == 1
        assert "第二行" not in repr(receipt)


@pytest.mark.parametrize("mode", ["lost", "ok"])
async def test_uncertain_or_delayed_send_is_reconciled_without_another_post(mode):
    provider, state = inbox_provider()
    state.update(send_mode=mode, read_failures=1)
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42; bili_jct=synthetic-csrf")
        await api.inbox.refresh(42)
        receipt = await api.send_message(7, "核对测试")
        assert not receipt.confirmed and api.inbox.pending is not None
        calls = len(provider.requests)
        with pytest.raises(DownloadError, match="尚未核对"):
            await api.send_message(7, "核对测试")
        assert len(provider.requests) == calls
        if mode == "lost":
            state["sent"][7] = [
                private_message(
                    26, sender_uid=42, receiver_id=7, content=json.dumps({"content": "核对测试"})
                )
            ]
        recovered = await api.inbox.reconcile(42)
        assert recovered.confirmed and api.inbox.pending is None
        assert state["posts"] == 1


async def test_known_rejection_and_invalid_input_never_retry_or_misroute():
    from bpi.errors import ApiError

    provider, state = inbox_provider()
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42; bili_jct=synthetic-csrf")
        await api.inbox.refresh(42)
        calls = len(provider.requests)
        for text in (" ", "中" * 1000):
            with pytest.raises(DownloadError):
                await api.send_message(7, text)
        with pytest.raises(DownloadError, match="列表中的"):
            await api.send_message(12345, "不应发送")
        assert len(provider.requests) == calls and state["posts"] == 0
        state["send_mode"] = "reject"
        with pytest.raises(ApiError):
            await api.send_message(7, "明确拒绝")
        assert state["posts"] == 1 and api.inbox.pending is None and not state["sent"]


async def test_missing_send_identity_or_csrf_stays_read_only():
    provider, state = inbox_provider()
    async with provider.api() as api:
        await api.use_cookie("SESSDATA=synthetic; DedeUserID=42")
        await api.inbox.refresh(42)
        count = len(provider.requests)
        with pytest.raises(DownloadError, match="验证字段"):
            await api.send_message(7, "需要重新登录")
        assert len(provider.requests) == count and state["posts"] == 0
