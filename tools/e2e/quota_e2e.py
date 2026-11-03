#!/usr/bin/env python3
"""P4 end-to-end: three quota windows (5h / 7d / 30d).

Requires a running gateway with:
  - DISCORD_API_BASE pointing at tools/mockdiscord (user "dave")
  - a channel pointing at tools/mockopenai (model minimax/abab6.5s-chat)

Usage:
  python tools/e2e/quota_e2e.py --base http://127.0.0.1:3000
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def request(base, method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def check(name, condition, detail=""):
    print(f"{PASS if condition else FAIL}  {name}" + (f"  -> {detail}" if detail and not condition else ""))
    return condition


def admin_token(base, user, password):
    _, payload = request(base, "POST", "/api/user/login", {"username": user, "password": password})
    return payload["data"]["access_token"]


def discord_login(base, code):
    _, state = request(base, "POST", "/api/oauth/state", {"provider": "discord", "intent": "login"})
    query = urllib.parse.urlencode({"code": code, "state": state["data"]["flow_token"]})
    _, payload = request(base, "GET", "/api/oauth/discord?" + query)
    if not payload.get("success"):
        raise RuntimeError(f"login failed: {payload}")
    return payload["data"]["access_token"]


def token_key_from_db(db_path, name):
    """The console masks token keys on purpose, so the harness reads the key
    straight from the local SQLite file it started the gateway with."""
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("select key from tokens where name = ? order by id desc limit 1", (name,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def ensure_token(base, user_token, db_path, name="quota-e2e"):
    key = token_key_from_db(db_path, name)
    if key:
        return key if key.startswith("sk-") else "sk-" + key
    _, created = request(base, "POST", "/api/token/", {
        "name": name, "remain_quota": 0, "unlimited_quota": True, "expired_time": -1,
    }, token=user_token)
    if not created.get("success"):
        raise RuntimeError(f"token create failed: {created}")
    key = token_key_from_db(db_path, name)
    if not key:
        raise RuntimeError("token not found after creation")
    return key if key.startswith("sk-") else "sk-" + key


def chat(base, key, model="minimax/abab6.5s-chat"):
    status, payload = request(base, "POST", "/v1/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
    }, token=key)
    return status, payload


def window_state(base, user_token):
    _, payload = request(base, "GET", "/api/community/plan/self", token=user_token)
    return payload.get("data") or {}


def wait_window(base, user_token, seconds, predicate, timeout=5.0):
    """Window bookkeeping lands after the response is flushed; poll briefly."""
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        state = {w["window_seconds"]: w for w in window_state(base, user_token).get("windows", [])}
        if seconds in state and predicate(state[seconds]):
            return state
        time.sleep(0.15)
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:3000")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-pass", default=os.environ.get("ADMIN_PASS", ""),
                        help="管理员密码，或用环境变量 ADMIN_PASS 提供")
    parser.add_argument("--code", default="code-dave", help="mock discord code of a brand-new user")
    parser.add_argument("--plan-id", type=int, default=1)
    parser.add_argument("--db", default=os.path.join("..", "run", "data", "new-api.db"))
    args = parser.parse_args()

    base = args.base
    admin = admin_token(base, args.admin_user, args.admin_pass)

    # 1. Small windows so the test can exhaust them quickly.
    _, updated = request(base, "PUT", f"/api/community/admin/plans/{args.plan_id}", {
        "window_5h_quota": 2500,
        "window_7d_quota": 6000,
        "window_30d_quota": 12000,
        "is_default": True,
    }, token=admin)
    ok = check("管理员可编辑三窗口额度", updated.get("success") is True, json.dumps(updated)[:200])
    if not ok:
        sys.exit(1)
    request(base, "POST", f"/api/community/admin/plans/{args.plan_id}/reset-usage", {}, token=admin)

    # 2. First Discord login of a brand-new account receives the default plan.
    user_token = discord_login(base, args.code)
    state = window_state(base, user_token)
    ok &= check("新用户自动获得默认套餐", state.get("plan_title") == "默认套餐", json.dumps(state, ensure_ascii=False)[:200])
    windows = {w["window_seconds"]: w for w in state.get("windows", [])}
    ok &= check("返回三个窗口", set(windows) == {18000, 604800, 2592000}, str(sorted(windows)))
    ok &= check("额度 0 表示不限（此处三窗口均有限）", all(w.get("limited") for w in windows.values()), json.dumps(windows, ensure_ascii=False)[:200])

    key = ensure_token(base, user_token, args.db)

    # 3. Burn quota until the 5h window rejects.
    status, payload = chat(base, key)
    ok &= check("首次请求成功", status == 200, f"{status} {payload}")
    windows = wait_window(base, user_token, 18000, lambda w: w["used"] > 0)
    first = windows[18000]
    ok &= check("窗口从首次使用开始计时（非自然日）",
                first["window_start"] > 0 and first["resets_at"] - first["window_start"] == 18000,
                json.dumps(first, ensure_ascii=False))
    ok &= check("5h 已记账", first["used"] > 0, str(first["used"]))

    rejected = None
    for _ in range(12):
        status, payload = chat(base, key)
        if status != 200:
            rejected = payload
            break
        time.sleep(0.12)
    ok &= check("5h 窗口先于 7d/30d 生效", rejected is not None, "requests kept succeeding")
    if rejected:
        msg = json.dumps(rejected, ensure_ascii=False)
        ok &= check("拒绝原因明确指向 5 小时窗口", "5 小时" in msg, msg[:200])

    windows = wait_window(base, user_token, 18000, lambda w: w["remaining"] == 0)
    ok &= check("5h 剩余为 0", windows[18000]["remaining"] == 0, str(windows[18000]))
    ok &= check("7d 窗口仍有剩余（独立）", windows[604800]["remaining"] > 0, str(windows[604800]))
    ok &= check("30d 窗口仍有剩余（独立）", windows[2592000]["remaining"] > 0, str(windows[2592000]))
    ok &= check("7d 用量小于 5h 用量口径下的总量",
                windows[604800]["used"] == windows[18000]["used"],
                f"7d={windows[604800]['used']} 5h={windows[18000]['used']}")

    # 4. Raising the 5h quota lets the same user continue while 7d keeps counting.
    used_7d_before = windows[604800]["used"]
    request(base, "PUT", f"/api/community/admin/plans/{args.plan_id}", {
        "window_5h_quota": 2500 + 5000,
    }, token=admin)
    status, payload = chat(base, key)
    ok &= check("提高 5h 额度后立即可继续", status == 200, f"{status} {json.dumps(payload, ensure_ascii=False)[:200]}")
    windows = wait_window(base, user_token, 604800, lambda w: w["used"] > used_7d_before)
    ok &= check("7d 窗口持续累计（未因 5h 调整重置）", windows[604800]["used"] > used_7d_before,
                f"{used_7d_before} -> {windows[604800]['used']}")

    print("\nRESULT:", "ALL PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
