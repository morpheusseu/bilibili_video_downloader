import os
import stat

import pytest
from bilibili_client import local_store
from bilibili_client.api import safe_error
from bilibili_client.sources import DownloadError

from bili_downloader.auth import Session, forget_session, load_session, save_session


def session(account_id=42):
    return Session(
        account_id,
        "合成账号",
        f"SESSDATA=synthetic; DedeUserID={account_id}",
        "Synthetic Chrome",
    )


def test_local_login_roundtrip_permissions_and_precise_account_forget():
    assert load_session() is None
    one, two = session(), session(43)
    save_session(one)
    assert load_session() == one
    save_session(two)
    assert load_session() == two
    root = local_store.data_root()
    first = local_store.account_folder(root / "accounts", 42)
    second = local_store.account_folder(root / "accounts", 43)
    assert "SESSDATA" in (first / "session.json").read_text()
    assert "SESSDATA" not in (root / "active-account.json").read_text()
    if os.name == "posix":
        for path in (root, root / "accounts", first, second):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        for path in (root / "active-account.json", first / "session.json", second / "session.json"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Forgetting a different in-memory account cannot erase the saved active login.
    forget_session(42)
    assert not first.exists() and load_session() == two
    forget_session(43)
    assert not second.exists() and load_session() is None
    forget_session()  # Repeating forget is harmless.


def test_failed_atomic_save_retains_previous_login_and_hides_error_details(monkeypatch):
    one = session()
    save_session(one)

    def unavailable(*args):
        raise OSError("sensitive=never-display")

    monkeypatch.setattr(local_store.os, "replace", unavailable)
    with pytest.raises(DownloadError) as error:
        save_session(Session(42, "changed", "SESSDATA=new", "Synthetic Chrome"))
    assert "sensitive" not in safe_error(error.value)
    assert load_session() == one
    assert not list(local_store.data_root().rglob(".session.json-*"))


@pytest.mark.parametrize("damage", ["corrupt", "identity", "symlink"])
def test_invalid_login_is_rejected_without_following_symlinks(tmp_path, damage):
    save_session(session())
    path = local_store.account_folder(local_store.data_root() / "accounts", 42) / "session.json"
    if damage == "corrupt":
        path.write_bytes(b"not valid json")
    elif damage == "identity":
        local_store.write_json(
            path, {"mid": 43, "name": "other", "cookie": "SESSDATA=x", "user_agent": ""}
        )
    else:
        outside = tmp_path / "unrelated.json"
        outside.write_text("unrelated data")
        path.unlink()
        path.symlink_to(outside)
    with pytest.raises(DownloadError, match="无法读取"):
        load_session()
    if damage == "symlink":
        with pytest.raises(DownloadError, match="无法保存"):
            save_session(session())
        with pytest.raises(DownloadError, match="无法完整删除"):
            forget_session(42)
        assert outside.read_text() == "unrelated data"
