from __future__ import annotations

from fulin_editor import accounts, db


def test_admin_account_list_never_exposes_password_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(accounts, "ALLOW_SIGNUP", True)
    accounts.register_user("owner@example.com", "old-password", "负责人")
    accounts.register_user("member@example.com", "member-password", "运营")

    payload = accounts.list_users(query="example", page=1, page_size=20)

    assert payload["total"] == 2
    assert all("password_hash" not in item for item in payload["items"])
    assert all(item["password_state"] == "已加密保存，不可查看" for item in payload["items"])


def test_admin_can_reset_password_without_reading_old_password(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(accounts, "ALLOW_SIGNUP", True)
    user = accounts.register_user("user@example.com", "old-password", "成员")

    accounts.reset_user_password(user["id"], "new-password")

    assert accounts.authenticate("user@example.com", "old-password") is None
    assert accounts.authenticate("user@example.com", "new-password") is not None
