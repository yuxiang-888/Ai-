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


def test_admin_can_grant_and_revoke_member_access(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(accounts, "ALLOW_SIGNUP", True)
    owner = accounts.register_user("owner@example.com", "owner-password", "负责人")
    member = accounts.register_user("member@example.com", "member-password", "运营")
    promoted = accounts.update_user_access(member["id"], role="admin", actor_user_id=owner["id"])
    assert promoted["role"] == "admin"
    accounts.update_user_access(member["id"], role="member", actor_user_id=owner["id"])
    accounts.update_user_access(member["id"], enabled=False, actor_user_id=owner["id"])
    assert accounts.authenticate("member@example.com", "member-password") is None


def test_last_admin_and_current_admin_cannot_lock_themselves_out(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "SQLITE_PATH", tmp_path / "accounts.sqlite3")
    monkeypatch.setattr(accounts, "ALLOW_SIGNUP", True)
    owner = accounts.register_user("owner@example.com", "owner-password", "负责人")
    import pytest
    with pytest.raises(ValueError, match="至少"):
        accounts.update_user_access(owner["id"], enabled=False, actor_user_id="some-other-admin")
    with pytest.raises(ValueError, match="不能"):
        accounts.update_user_access(owner["id"], role="member", actor_user_id=owner["id"])
