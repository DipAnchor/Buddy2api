import asyncio

import pytest

import auth_manager
import credential_crypto
import database as db


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    path = tmp_path / "gateway.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CB_GATEWAY_MASTER_KEY", "pytest-master-key")
    credential_crypto.reset_cache()
    db.init_db()
    yield path
    credential_crypto.reset_cache()


def test_domain_filter_match_semantics():
    m = auth_manager._domain_filter_match
    assert m("www.workbuddy.ai", "*.workbuddy.ai")
    assert m("workbuddy.ai", "*.workbuddy.ai")
    assert not m("www.workbuddy.cn", "*.workbuddy.ai")
    assert m("www.workbuddy.ai", "www.workbuddy.ai")
    assert not m("api.workbuddy.ai", "www.workbuddy.ai")
    assert m("anything.example", "*")
    assert not m("", "*.workbuddy.ai")
    # 防误配：后缀必须落在点上
    assert not m("evilworkbuddy.ai", "*.workbuddy.ai")
    # 只接受精确 / *.suffix / *；其余形式一律不匹配
    assert not m("evilworkbuddy.ai", "*workbuddy.ai")
    assert not m("notworkbuddy.ai", "*workbuddy.ai")
    assert not m("www.workbuddy.ai", ".*workbuddy.ai")
    assert not m("www.workbuddy.ai", "w*.workbuddy.ai")
    assert not m("www.workbuddy.ai", "*.work*.ai")


def test_normalize_domain_filter():
    n = auth_manager._normalize_domain_filter
    assert n(None) is None
    assert n([]) is None
    assert n("*.WorkBuddy.AI") == ["*.workbuddy.ai"]
    assert n([" www.workbuddy.ai ", ""]) == ["www.workbuddy.ai"]


def test_api_key_domain_filter_roundtrip(isolated_db):
    key = "sk-cb-" + "x" * 20
    kid = db.add_api_key(
        key, "filtered", default_channel="workbuddy",
        account_domain_filter=["*.workbuddy.ai"],
    )
    info = db.get_api_key_by_key(key)
    assert info["account_domain_filter"] == ["*.workbuddy.ai"]

    db.update_api_key(kid, {"account_domain_filter": ["www.workbuddy.cn"]})
    assert db.get_api_key_by_key(key)["account_domain_filter"] == ["www.workbuddy.cn"]

    db.update_api_key(kid, {"account_domain_filter": []})
    assert db.get_api_key_by_key(key)["account_domain_filter"] is None

    listed = {row["id"]: row for row in db.list_api_keys()}
    assert listed[kid]["account_domain_filter"] is None


def test_pick_account_respects_domain_filter(isolated_db, monkeypatch):
    accounts = [
        {"id": 1, "name": "cn", "domain": "www.workbuddy.cn", "priority": 100,
         "weight": 1, "total_requests": 0},
        {"id": 2, "name": "intl", "domain": "www.workbuddy.ai", "priority": 1,
         "weight": 1, "total_requests": 0},
    ]
    monkeypatch.setattr(db, "get_active_accounts", lambda provider: accounts)
    monkeypatch.setattr(auth_manager, "account_is_cooling_down", lambda aid: False)
    with auth_manager._route_lock:
        auth_manager._sticky_account_id.clear()

    # 无过滤：按优先级取国内号
    assert auth_manager.pick_account(provider="workbuddy")["id"] == 1

    # 带国际过滤：即使国内号优先级更高也绝不选中
    picked = auth_manager.pick_account(provider="workbuddy", domain_filter=["*.workbuddy.ai"])
    assert picked["id"] == 2

    # 过滤集不同 → 粘性槽位互不干扰（国内号粘性仍保留）
    picked = auth_manager.pick_account(provider="workbuddy")
    assert picked["id"] == 1

    # 无匹配账号 → 返回 None
    assert auth_manager.pick_account(provider="workbuddy", domain_filter=["nope.example"]) is None


def test_pick_account_with_fallback_respects_domain_filter(isolated_db, monkeypatch):
    accounts = [
        {"id": 1, "name": "cn", "domain": "www.workbuddy.cn", "status": "active",
         "priority": 100, "weight": 1, "total_requests": 0},
    ]
    monkeypatch.setattr(db, "get_active_accounts", lambda provider: accounts)
    monkeypatch.setattr(auth_manager, "account_is_cooling_down", lambda aid: False)
    monkeypatch.setattr(db, "list_accounts", lambda provider=None: [])
    with auth_manager._route_lock:
        auth_manager._sticky_account_id.clear()

    result = asyncio.run(auth_manager.pick_account_with_fallback(
        provider="workbuddy", domain_filter=["*.workbuddy.ai"],
    ))
    assert result is None


def test_validate_account_domain_filter_normalizes_and_rejects():
    import server

    v = server._validate_account_domain_filter
    assert v(None) is None
    assert v([]) is None
    assert v(["*.WorkBuddy.AI"]) == ["*.workbuddy.ai"]
    assert v([" WWW.WorkBuddy.AI "]) == ["www.workbuddy.ai"]
    assert v(["*"]) == ["*"]
    # scheme / path / port 归一化到 host 后保存，保证 picker 能比较
    assert v(["https://www.workbuddy.ai"]) == ["www.workbuddy.ai"]
    assert v(["www.workbuddy.ai:443"]) == ["www.workbuddy.ai"]
    assert v(["www.workbuddy.ai/api"]) == ["www.workbuddy.ai"]

    for bad in ([1], [None], [""], ["   "], ["*workbuddy.ai"], ["w*.workbuddy.ai"],
                ["*.work*.ai"], ["*."], "not-a-list", {"a": 1}):
        with pytest.raises(Exception):
            v(bad)


def test_refresh_that_changes_host_is_not_handed_to_filtered_key(isolated_db, monkeypatch):
    """刷新把账号换到别的域后，不能直接交给被过滤的 key（单次绕过）。"""
    expired = {"id": 7, "name": "intl", "domain": "www.workbuddy.ai", "status": "expired",
               "priority": 10, "weight": 1, "total_requests": 0}
    moved = dict(expired, domain="www.workbuddy.cn")

    monkeypatch.setattr(db, "get_active_accounts", lambda provider: [])
    monkeypatch.setattr(db, "list_accounts", lambda provider=None: [expired])
    monkeypatch.setattr(db, "get_account", lambda aid: dict(moved))
    monkeypatch.setattr(auth_manager, "account_is_cooling_down", lambda aid: False)

    async def fake_refresh(account):
        return True

    monkeypatch.setattr(auth_manager, "refresh_token", fake_refresh)
    with auth_manager._route_lock:
        auth_manager._sticky_account_id.clear()

    result = asyncio.run(auth_manager.pick_account_with_fallback(
        provider="workbuddy", domain_filter=["*.workbuddy.ai"],
    ))
    assert result is None
    with auth_manager._route_lock:
        assert auth_manager._sticky_account_id == {}

    # 未移动时正常返回
    monkeypatch.setattr(db, "get_account", lambda aid: dict(expired))
    result = asyncio.run(auth_manager.pick_account_with_fallback(
        provider="workbuddy", domain_filter=["*.workbuddy.ai"],
    ))
    assert result and result["id"] == 7
