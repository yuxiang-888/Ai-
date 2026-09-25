from fastapi.testclient import TestClient

from fulin_editor import accounts, db, web_api


def test_two_browsers_share_data_and_permission_changes(tmp_path, monkeypatch):
    database = tmp_path / 'shared.sqlite3'
    monkeypatch.setattr(db, 'SQLITE_PATH', database)
    monkeypatch.setattr(db, 'DATABASE_URL', '')
    monkeypatch.setattr(accounts, 'ALLOW_SIGNUP', True)
    monkeypatch.setattr(accounts, 'DEFAULT_ROLE', 'admin')
    owner = accounts.register_user('owner@example.com', 'owner-password')
    member = accounts.register_user('member@example.com', 'member-password')
    assert member['role'] == 'member'
    monkeypatch.setattr(web_api, '_ensure_schema', lambda: None)
    monkeypatch.setattr(web_api.app.router, 'on_startup', [])
    with TestClient(web_api.app, base_url='https://testserver') as first, TestClient(web_api.app, base_url='https://testserver') as second:
        assert first.post('/api/auth/login', json={'email': owner['email'], 'password': 'owner-password'}).status_code == 200
        assert second.post('/api/auth/login', json={'email': member['email'], 'password': 'member-password'}).status_code == 200
        assert second.get('/api/admin/users').status_code == 403
        assert first.patch(f"/api/admin/users/{member['id']}/access", json={'role': 'admin'}).status_code == 200
        # Already-open browser sees the grant immediately, without logging in again.
        assert second.get('/api/admin/users').json() == first.get('/api/admin/users').json()
        assert second.get('/api/admin/users').headers['cache-control'] == 'no-store'
        accounts.register_user('customer@example.com', 'customer-password')
        assert second.get('/api/admin/users').json()['total'] == 3
        assert first.patch(f"/api/admin/users/{member['id']}/access", json={'role': 'member'}).status_code == 200
        assert second.get('/api/admin/users').status_code == 403
        assert first.patch(f"/api/admin/users/{member['id']}/access", json={'enabled': False}).status_code == 200
        assert second.get('/api/admin/users').status_code == 401
