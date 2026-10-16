"""QwenWork daily credit notice. The 1.3.1 client claims it with one POST."""

from __future__ import annotations

import httpx

import database as db
from providers.qwenwork.constants import (
    CHANNEL_ID,
    CLAIM_APP_MODE,
    CLAIM_BUILD,
    CLAIM_IDE_VERSION,
    CLAIM_RELEASE_VERSION,
    CLAIM_USER_AGENT,
    DAILY_CREDIT_NOTICE_CLAIM_PATH,
    GATEWAY,
)
from providers.qwenwork.token import QwenWorkAuthError, is_token_expired, refresh_account


def parse_daily_credit_notice(payload) -> dict:
    """Match the desktop parser: unwrap adapter data, keep a positive integer credit."""
    data = payload if isinstance(payload, dict) else {}
    nested = data.get("data")
    if isinstance(nested, dict):
        data = nested
        inner = data.get("data")
        if isinstance(inner, dict):
            data = inner
    credits = data.get("credits") if isinstance(data, dict) else None
    amount = 0.0
    if isinstance(credits, int) and not isinstance(credits, bool) and credits > 0:
        amount = float(credits)
    should_show = isinstance(data, dict) and data.get("should_show") is True and amount > 0
    return {"should_show": should_show, "credits": amount}


def _row(
    account: dict,
    *,
    ok: bool,
    status_code: int = 0,
    message: str = "",
    claimed: bool = False,
    already_claimed: bool = False,
    credit: float = 0,
) -> dict:
    return {
        "account_id": account.get("id"),
        "account_name": account.get("nickname") or account.get("name") or str(account.get("id")),
        "ok": ok,
        "claimed": claimed,
        "already_claimed": already_claimed,
        "status_code": status_code,
        "message": message,
        "credit": credit,
        "active": True,
        "today_checked_in": already_claimed or claimed,
        "today_credit": credit,
        "channel": CHANNEL_ID,
    }


def _remember(account: dict, result: dict) -> dict:
    account_id = account.get("id")
    if account_id and result.get("ok") and (result.get("claimed") or result.get("already_claimed")):
        db.upsert_account_checkin_cache(int(account_id), result)
    return result


def _cached_today(account: dict) -> dict | None:
    account_id = account.get("id")
    if not account_id:
        return None
    cached = db.get_account_checkin_cache(int(account_id))
    if not isinstance(cached, dict) or not cached.get("ok"):
        return None
    if not (cached.get("claimed") or cached.get("already_claimed") or cached.get("today_checked_in")):
        return None
    cached["claimed"] = False
    cached["already_claimed"] = True
    cached["today_checked_in"] = True
    cached["stale"] = False
    cached["channel"] = CHANNEL_ID
    return cached


def _claim_headers(access: str) -> dict[str, str]:
    # Official 1.3.1 claim is a body-less POST. Content-Type plus {} is a 400.
    headers = {
        "Accept": "application/json",
        "User-Agent": CLAIM_USER_AGENT,
        "X-QwenWork-Version": CLAIM_IDE_VERSION,
        "X-QwenWork-Release-Version": CLAIM_RELEASE_VERSION,
        "X-QwenWork-Build": CLAIM_BUILD,
        "X-QwenWork-Platform": "win32",
        "X-QwenWork-Arch": "x64",
        "X-QwenWork-Channel": "stable",
        "X-QwenWork-App-Mode": CLAIM_APP_MODE,
    }
    if access:
        headers["Authorization"] = f"Bearer {access}"
    return headers


def _error_detail(payload: dict, status: int) -> str:
    detail = ""
    if isinstance(payload, dict):
        nested = payload.get("error")
        if isinstance(nested, dict):
            detail = str(nested.get("message") or nested.get("code") or "")
        detail = detail or str(
            payload.get("errorMessage") or payload.get("errorCode") or payload.get("message") or ""
        )
    text = f"QwenWork 赠分 HTTP {status}"
    if detail and detail != "None":
        text += f" {detail}"
    return text[:240]


async def _post_claim(account: dict) -> tuple[int, dict, str]:
    headers = _claim_headers(str(account.get("access_token") or ""))
    url = f"{GATEWAY}{DAILY_CREDIT_NOTICE_CLAIM_PATH}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, headers=headers)
    except httpx.HTTPError as exc:
        return 0, {}, str(exc)[:240]
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    return response.status_code, payload if isinstance(payload, dict) else {}, ""


def _from_notice(account: dict, status: int, payload: dict) -> dict:
    parsed = parse_daily_credit_notice(payload)
    if parsed["should_show"]:
        return _row(
            account,
            ok=True,
            status_code=status,
            claimed=True,
            credit=parsed["credits"],
            message="领取成功",
        )
    if parsed["credits"] > 0:
        return _row(
            account,
            ok=True,
            status_code=status,
            already_claimed=True,
            credit=parsed["credits"],
            message="今日已领",
        )
    return _row(
        account,
        ok=True,
        status_code=status,
        already_claimed=True,
        message="今日没有新的赠分",
    )


async def fetch_checkin(account: dict, force: bool = False) -> dict:
    del force
    cached = _cached_today(account)
    if cached:
        return cached
    return _row(account, ok=True, message="尚未领取")


async def claim_checkin(account: dict) -> dict:
    current = account
    if is_token_expired(current) or not str(current.get("access_token") or ""):
        try:
            current = await refresh_account(current)
        except QwenWorkAuthError as exc:
            return _row(account, ok=False, message=str(exc)[:240])
    status, payload, error = await _post_claim(current)
    if error or status in {401, 403}:
        try:
            current = await refresh_account(current)
            status, payload, error = await _post_claim(current)
        except QwenWorkAuthError as exc:
            return _row(account, ok=False, status_code=status, message=(error or str(exc))[:240])
    if error:
        return _row(account, ok=False, message=error)
    if status >= 400:
        return _row(account, ok=False, status_code=status, message=_error_detail(payload, status))
    return _remember(current, _from_notice(current, status, payload))
