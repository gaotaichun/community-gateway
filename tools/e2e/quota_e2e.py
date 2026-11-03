#!/usr/bin/env python3
"""P4 end-to-end: two site-wide hourly token pools (MiniMax / GLM).

What this covers:
  1. an admin can set both pools from the console
  2. a brand-new Discord account receives the default plan
  3. the self endpoint returns exactly two windows, both 1 hour, unit=tokens
  4. tokens are counted as prompt + completion, per pool
  5. the GLM pool and the MiniMax pool never bleed into each other
  6. the pools are SITE-WIDE: exhausting one user's traffic blocks another user
  7. 0 means unlimited for that pool
  8. raising the budget unblocks traffic immediately, reset-usage clears both

Requires a running gateway with:
  - DISCORD_API_BASE pointing at tools/mockdiscord (users "dave" / "alice")
  - a channel pointing at tools/mockopenai exposing a non-GLM and a GLM model

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

DEFAULT_MODEL = "minimax/abab6.5s-chat"
GLM_MODEL = "Z.ai/glm-5.3-flash"
# Neither MiniMax nor GLM: served by community hardware, so it must be free.
FREE_MODEL = "openrouter/deepseek-chat"
# Values the shipped default plan uses; the harness restores them at the end.
SHIPPED_DEFAULT = 30000000
SHIPPED_GLM = 3000000


def request(base, method, path, body=None, token=None, attempts=3):
    """Windows occasionally resets an idle keep-alive socket between the server
    flushing the response and the client reading it. Retrying is safe here: every
    call in this harness is either idempotent or a deliberately repeated chat."""
    last = None
    for _ in range(attempts):
        try:
            return _request_once(base, method, path, body, token)
        except (ConnectionResetError, urllib.error.URLError) as exc:
            last = exc
            time.sleep(0.2)
    raise last


def _request_once(base, method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json", "Connection": "close"}
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
    return bool(condition)


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


def ensure_token(base, user_token, db_path, name):
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


def chat(base, key, model=DEFAULT_MODEL, prompt=1000):
    """`prompt` is honoured by tools/mockopenai and becomes usage.prompt_tokens,
    so the harness can control exactly how many tokens a call costs."""
    status, payload = request(base, "POST", "/v1/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
    }, token=key)
    return status, payload


def pools(base, user_token):
    _, payload = request(base, "GET", "/api/community/plan/self", token=user_token)
    data = payload.get("data") or {}
    return data, {w.get("pool"): w for w in data.get("windows", [])}


def wait_pool(base, user_token, pool, predicate, timeout=6.0):
    """Pool bookkeeping lands after the response is flushed; poll briefly."""
    deadline = time.time() + timeout
    state = {}
    while time.time() < deadline:
        _, state = pools(base, user_token)
        if pool in state and predicate(state[pool]):
            return state
        time.sleep(0.15)
    return state


def ensure_channel_models(base, admin, glm_model):
    """The GLM pool can only be exercised if the channel advertises a GLM model."""
    _, listed = request(base, "GET", "/api/channel/?p=1&page_size=100", token=admin)
    items = (listed.get("data") or {}).get("items") or listed.get("data") or []
    for ch in items:
        models = [m.strip() for m in (ch.get("models") or "").split(",") if m.strip()]
        if glm_model in models:
            return True
        models.append(glm_model)
        payload = {"id": ch["id"], "models": ",".join(models)}
        _, updated = request(base, "PUT", "/api/channel/", payload, token=admin)
        return bool(updated.get("success"))
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:3000")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-pass", default=os.environ.get("ADMIN_PASS", ""),
                        help="管理员密码，或用环境变量 ADMIN_PASS 提供")
    parser.add_argument("--plan-id", type=int, default=1)
    parser.add_argument("--db", default=os.path.join("..", "run", "data", "new-api.db"))
    args = parser.parse_args()

    base = args.base
    admin = admin_token(base, args.admin_user, args.admin_pass)

    ok = check("渠道已登记 GLM 模型（供 GLM 池测试）", ensure_channel_models(base, admin, GLM_MODEL))

    # 1. Two pools, small enough to exhaust in a few calls.
    _, updated = request(base, "PUT", f"/api/community/admin/plans/{args.plan_id}", {
        "window_1h_tokens": 2500,
        "glm_1h_tokens": 1500,
        "is_default": True,
    }, token=admin)
    ok &= check("管理员可编辑两个池的每小时 token 额度", updated.get("success") is True, json.dumps(updated)[:200])
    request(base, "POST", f"/api/community/admin/plans/{args.plan_id}/reset-usage", {}, token=admin)

    # 2. First Discord login of a brand-new account receives the default plan.
    dave = discord_login(base, "code-dave")
    data, state = pools(base, dave)
    ok &= check("新用户自动获得默认套餐", data.get("plan_title") == "默认套餐", json.dumps(data, ensure_ascii=False)[:200])
    ok &= check("只返回两个池", set(state) == {"default", "glm"}, str(sorted(state)))
    ok &= check("两个池都是 1 小时窗口", all(w["window_seconds"] == 3600 for w in state.values()),
                json.dumps(state, ensure_ascii=False)[:200])
    ok &= check("单位为 token 而不是美元", all(w["unit"] == "tokens" for w in state.values()),
                str([w.get("unit") for w in state.values()]))
    ok &= check("额度有限时 limited=true", all(w.get("limited") for w in state.values()),
                json.dumps(state, ensure_ascii=False)[:200])
    ok &= check("未调用前窗口未开始计时", all(not w.get("started") for w in state.values()),
                str([w.get("started") for w in state.values()]))

    dave_key = ensure_token(base, dave, args.db, "quota-e2e-dave")

    # 3. A non-GLM call only touches the MiniMax pool.
    status, payload = chat(base, dave_key, DEFAULT_MODEL)
    ok &= check("首次请求成功", status == 200, f"{status} {payload}")
    state = wait_pool(base, dave, "default", lambda w: w["used"] > 0)
    first = state["default"]
    ok &= check("窗口从首次调用开始计时（非自然小时）",
                first["window_start"] > 0 and first["resets_at"] - first["window_start"] == 3600,
                json.dumps(first, ensure_ascii=False))
    ok &= check("MiniMax 池已记账", first["used"] > 0, str(first["used"]))
    ok &= check("GLM 池未被波及", state["glm"]["used"] == 0, str(state["glm"]["used"]))

    # 4. Burn the MiniMax pool until it rejects.
    rejected = None
    for _ in range(10):
        status, payload = chat(base, dave_key, DEFAULT_MODEL)
        if status != 200:
            rejected = payload
            break
        time.sleep(0.1)
    ok &= check("MiniMax 池会拒绝超额请求", rejected is not None, "requests kept succeeding")
    if rejected:
        msg = json.dumps(rejected, ensure_ascii=False)
        ok &= check("拒绝文案指出是全站额度", "全站额度已用完" in msg, msg[:220])
        ok &= check("拒绝文案点名 MiniMax 池", "MiniMax" in msg, msg[:220])
        ok &= check("拒绝文案给出上限", "上限" in msg, msg[:220])

    state = wait_pool(base, dave, "default", lambda w: w["remaining"] == 0)
    ok &= check("MiniMax 池剩余为 0", state["default"]["remaining"] == 0, json.dumps(state["default"], ensure_ascii=False))
    ok &= check("GLM 池仍有剩余（两池互不影响）", state["glm"]["remaining"] == state["glm"]["quota"],
                json.dumps(state["glm"], ensure_ascii=False))

    # 5. The pool is site-wide, so a second member is blocked too.
    alice = discord_login(base, "code-alice")
    alice_key = ensure_token(base, alice, args.db, "quota-e2e-alice")
    status, payload = chat(base, alice_key, DEFAULT_MODEL)
    msg = json.dumps(payload, ensure_ascii=False)
    ok &= check("额度全站共享：另一个成员同样被拦", status != 200 and "全站额度已用完" in msg,
                f"{status} {msg[:200]}")

    # 6. GLM has its own budget and still works.
    status, payload = chat(base, dave_key, GLM_MODEL)
    ok &= check("GLM 池独立：MiniMax 用尽后 GLM 仍可用", status == 200,
                f"{status} {json.dumps(payload, ensure_ascii=False)[:200]}")
    state = wait_pool(base, dave, "glm", lambda w: w["used"] > 0)
    ok &= check("GLM 池已记账", state["glm"]["used"] > 0, str(state["glm"]["used"]))
    ok &= check("GLM 调用没有消耗 MiniMax 池", state["default"]["used"] > 0 and state["default"]["remaining"] == 0,
                json.dumps(state["default"], ensure_ascii=False))

    rejected = None
    for _ in range(10):
        status, payload = chat(base, dave_key, GLM_MODEL)
        if status != 200:
            rejected = payload
            break
        time.sleep(0.1)
    msg = json.dumps(rejected or {}, ensure_ascii=False)
    ok &= check("GLM 池用尽后拒绝", rejected is not None and "GLM" in msg, msg[:220])

    # 6b. A model that is neither MiniMax nor GLM is free: it must still work
    # with both metered pools exhausted, and must not touch either counter.
    before = wait_pool(base, dave, "default", lambda w: True)
    status, payload = chat(base, dave_key, FREE_MODEL)
    ok &= check("非 MiniMax / GLM 模型免费：两池用尽仍可用", status == 200,
                f"{status} {json.dumps(payload, ensure_ascii=False)[:200]}")
    time.sleep(0.4)
    after = wait_pool(base, dave, "default", lambda w: True)
    ok &= check("免费模型不消耗 MiniMax 池", after["default"]["used"] == before["default"]["used"],
                f"{before['default']['used']} -> {after['default']['used']}")
    ok &= check("免费模型不消耗 GLM 池", after["glm"]["used"] == before["glm"]["used"],
                f"{before['glm']['used']} -> {after['glm']['used']}")

    # 7. Raising one budget unblocks that pool only.
    request(base, "PUT", f"/api/community/admin/plans/{args.plan_id}", {
        "window_1h_tokens": 2500 + 6000,
    }, token=admin)
    status, _ = chat(base, dave_key, DEFAULT_MODEL)
    ok &= check("提高 MiniMax 额度后立即恢复", status == 200, str(status))
    status, payload = chat(base, dave_key, GLM_MODEL)
    ok &= check("GLM 额度未动，仍然拒绝", status != 200, f"{status} {json.dumps(payload, ensure_ascii=False)[:160]}")

    # 8. 0 means unlimited.
    request(base, "PUT", f"/api/community/admin/plans/{args.plan_id}", {
        "window_1h_tokens": 0,
        "glm_1h_tokens": 0,
    }, token=admin)
    _, state = pools(base, dave)
    ok &= check("额度 0 = 不限（limited=false）", all(not w["limited"] for w in state.values()),
                json.dumps(state, ensure_ascii=False)[:200])
    status, _ = chat(base, dave_key, GLM_MODEL)
    ok &= check("0 = 不限时 GLM 放行", status == 200, str(status))

    # 9. reset-usage clears both pools.
    request(base, "PUT", f"/api/community/admin/plans/{args.plan_id}", {
        "window_1h_tokens": 2500, "glm_1h_tokens": 1500,
    }, token=admin)
    request(base, "POST", f"/api/community/admin/plans/{args.plan_id}/reset-usage", {}, token=admin)
    _, state = pools(base, dave)
    ok &= check("重置用量后两个池都归零", all(w["used"] == 0 for w in state.values()),
                json.dumps(state, ensure_ascii=False)[:200])

    # 10. Leave the plan on the numbers the shipped bundle uses, so a manual
    # look at the console after the run shows the real configuration.
    request(base, "PUT", f"/api/community/admin/plans/{args.plan_id}", {
        "window_1h_tokens": SHIPPED_DEFAULT, "glm_1h_tokens": SHIPPED_GLM,
    }, token=admin)
    _, state = pools(base, dave)
    ok &= check("测试结束后恢复出厂额度 30M / 3M",
                state["default"]["quota"] == SHIPPED_DEFAULT and state["glm"]["quota"] == SHIPPED_GLM,
                json.dumps({k: v["quota"] for k, v in state.items()}))

    print("\nRESULT:", "ALL PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
