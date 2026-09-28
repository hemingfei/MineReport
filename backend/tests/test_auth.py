"""邀请制认证与三角色权限（#12 主缝合口测试）：覆盖 AC 全部路径含拒绝分支。"""

from __future__ import annotations

import datetime as dt

from conftest import new_test_password
from fastapi.testclient import TestClient

from app.auth import hash_password, verify_password
from app.models import Invitation, Role


# ---------- 密码原语 ----------

def test_password_hash_roundtrip() -> None:
    pw = new_test_password()
    stored = hash_password(pw)
    assert pw not in stored
    assert verify_password(pw, stored)
    assert not verify_password(new_test_password(), stored)
    assert not verify_password(pw, "garbage")


# ---------- 邀请 → 注册 → 登录 主流程 ----------

def test_invite_register_login_logout_flow(api: TestClient, make_user, login) -> None:
    admin = make_user(Role.ADMIN)
    admin_cookies = login(admin)

    r = api.post("/api/admin/invitations", json={"role": Role.ANALYST}, cookies=admin_cookies)
    assert r.status_code == 201, r.text
    invitation = r.json()
    assert invitation["role"] == "analyst"
    assert invitation["token"]

    email = "newcomer@test.local"
    newcomer_pw = new_test_password()
    r = api.post(
        "/api/auth/register",
        json={"token": invitation["token"], "email": email, "password": newcomer_pw},
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "analyst"
    newcomer_cookies = {"mr_session": r.cookies["mr_session"]}

    # 注册即获得会话
    r = api.get("/api/auth/me", cookies=newcomer_cookies)
    assert r.status_code == 200
    assert r.json()["email"] == email

    # 登出后会话失效
    r = api.post("/api/auth/logout", cookies=newcomer_cookies)
    assert r.status_code == 204
    assert api.get("/api/auth/me", cookies=newcomer_cookies).status_code == 401

    # 独立登录重新获得会话
    r = api.post("/api/auth/login", json={"email": email, "password": newcomer_pw})
    assert r.status_code == 200
    assert r.json()["role"] == "analyst"
    assert api.get("/api/auth/me", cookies={"mr_session": r.cookies["mr_session"]}).status_code == 200

    # 邀请已被使用（列表可见 used_at）
    r = api.get("/api/admin/invitations", cookies=admin_cookies)
    assert r.status_code == 200
    assert [i["used_at"] for i in r.json() if i["token"] == invitation["token"]][0] is not None


# ---------- 拒绝路径：401 / 403 ----------

def test_invitation_endpoints_role_gates(api: TestClient, make_user, login) -> None:
    """未登录→401；analyst/reader→403；admin→201（三角色就位，依赖可复用）。"""
    assert api.post("/api/admin/invitations", json={"role": Role.READER}).status_code == 401
    assert api.get("/api/admin/invitations").status_code == 401

    for role in (Role.ANALYST, Role.READER):
        cookies = login(make_user(role))
        r = api.post("/api/admin/invitations", json={"role": Role.READER}, cookies=cookies)
        assert r.status_code == 403, f"{role} should be forbidden, got {r.status_code}"
        assert api.get("/api/admin/invitations", cookies=cookies).status_code == 403


def test_tasks_endpoint_requires_auth(api: TestClient, make_user, login) -> None:
    """资源端点复用权限依赖的模式样板：tasks 轮询须登录（401 拒绝路径）。"""
    assert api.get("/api/tasks/1").status_code == 401

    cookies = login(make_user(Role.READER))
    assert api.get("/api/tasks/1", cookies=cookies).status_code == 404  # 已认证，走到 404


def test_me_requires_auth(api: TestClient) -> None:
    assert api.get("/api/auth/me").status_code == 401


def test_invalid_session_cookie_rejected(api: TestClient) -> None:
    r = api.get("/api/auth/me", cookies={"mr_session": "forged-token"})
    assert r.status_code == 401


# ---------- 注册的校验分支 ----------

def _invite(api: TestClient, admin_cookies: dict, role: str = Role.READER, **extra) -> str:
    r = api.post("/api/admin/invitations", json={"role": role, **extra}, cookies=admin_cookies)
    assert r.status_code == 201, r.text
    return r.json()["token"]


def _register(api: TestClient, token: str, email: str) -> object:
    return api.post(
        "/api/auth/register", json={"token": token, "email": email, "password": new_test_password()}
    )


def test_register_unknown_token(api: TestClient) -> None:
    assert _register(api, "no-such-token", "a@b.co").status_code == 400


def test_register_expired_invitation(api: TestClient, make_user, login) -> None:
    from app.db import session_scope

    admin_cookies = login(make_user(Role.ADMIN))
    token = _invite(api, admin_cookies)

    with session_scope() as s:
        inv = s.query(Invitation).filter(Invitation.token == token).one()
        inv.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
        s.commit()

    r = _register(api, token, "a@b.co")
    assert r.status_code == 400
    assert "expired" in r.json()["detail"]


def test_register_used_invitation(api: TestClient, make_user, login) -> None:
    admin_cookies = login(make_user(Role.ADMIN))
    token = _invite(api, admin_cookies, role=Role.ANALYST)

    assert _register(api, token, "first@b.co").status_code == 201
    assert _register(api, token, "second@b.co").status_code == 400


def test_register_email_bound_invitation(api: TestClient, make_user, login) -> None:
    admin_cookies = login(make_user(Role.ADMIN))
    token = _invite(api, admin_cookies, email="bound@b.co")

    assert _register(api, token, "other@b.co").status_code == 400
    assert _register(api, token, "bound@b.co").status_code == 201


def test_register_duplicate_email(api: TestClient, make_user, login) -> None:
    admin_cookies = login(make_user(Role.ADMIN))
    existing = make_user(Role.READER, email="dup@b.co")

    r = _register(api, _invite(api, admin_cookies), existing.email)
    assert r.status_code == 409


def test_register_bad_email_and_short_password(api: TestClient, make_user, login) -> None:
    admin_cookies = login(make_user(Role.ADMIN))
    token = _invite(api, admin_cookies)

    r = api.post(
        "/api/auth/register",
        json={"token": token, "email": "not-an-email", "password": new_test_password()},
    )
    assert r.status_code == 422

    r = api.post(
        "/api/auth/register", json={"token": token, "email": "ok@b.co", "password": "short"}
    )
    assert r.status_code == 422


def test_invitation_create_validates_role(api: TestClient, make_user, login) -> None:
    admin_cookies = login(make_user(Role.ADMIN))
    r = api.post("/api/admin/invitations", json={"role": "root"}, cookies=admin_cookies)
    assert r.status_code == 422


# ---------- 登录失败 ----------

def test_login_failures(api: TestClient, make_user) -> None:
    user = make_user(Role.READER)

    # 未知邮箱与错误密码同样 401（不泄漏哪个错）
    r = api.post(
        "/api/auth/login", json={"email": "ghost@b.co", "password": new_test_password()}
    )
    assert r.status_code == 401
    r = api.post(
        "/api/auth/login", json={"email": user.email, "password": new_test_password()}
    )
    assert r.status_code == 401

    # 禁用用户拒绝登录
    disabled = make_user(Role.READER, active=False)
    r = api.post(
        "/api/auth/login", json={"email": disabled.email, "password": disabled.password}
    )
    assert r.status_code == 401


# ---------- 会话过期 ----------

def test_expired_session_rejected(api: TestClient, make_user, login) -> None:
    from app.db import session_scope
    from app.models import UserSession

    user = make_user(Role.READER)
    cookies = login(user)

    with session_scope() as s:
        row = s.query(UserSession).one()
        row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        s.commit()

    assert api.get("/api/auth/me", cookies=cookies).status_code == 401

    # 幂等登出：会话已失效也 204 + 清 cookie，不残留死 cookie
    assert api.post("/api/auth/logout", cookies=cookies).status_code == 204


# ---------- bootstrap admin ----------

def test_bootstrap_admin_idempotent(db_engine, monkeypatch) -> None:
    from app import bootstrap
    from app.db import session_scope
    from app.models import User

    initial_pw = new_test_password()
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "root@minereport.local")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", initial_pw)

    assert bootstrap.ensure_bootstrap_admin() is True
    with session_scope() as s:
        admin = s.query(User).filter(User.role == Role.ADMIN).one()
        assert admin.email == "root@minereport.local"
        assert verify_password(initial_pw, admin.password_hash)

    # 已有 admin 后再跑：跳过，不覆盖既有密码
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", new_test_password())
    assert bootstrap.ensure_bootstrap_admin() is False
    with session_scope() as s:
        admin = s.query(User).filter(User.role == Role.ADMIN).one()
        assert verify_password(initial_pw, admin.password_hash)


def test_bootstrap_admin_skipped_without_env(db_engine, monkeypatch) -> None:
    from app import bootstrap

    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)
    assert bootstrap.ensure_bootstrap_admin() is False
