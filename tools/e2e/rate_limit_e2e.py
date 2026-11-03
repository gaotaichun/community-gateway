#!/usr/bin/env python3
"""P4 end-to-end: the site-wide per-minute request cap.

This is the last line of defence — it stops one runaway client from hammering
Bifrost, independently of the hourly token pools.

The limiter lives in process memory, so the gateway must be freshly started with
a small cap for the test to be deterministic:

  GLOBAL_RATE_LIMIT_PER_MINUTE=5 ./new-api --port 3001

Usage:
  python tools/e2e/rate_limit_e2e.py --base http://127.0.0.1:3001 --limit 5
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


def request(base, method, path, body=None, token=None, attempts=3):
    last = None
    for _ in range(attempts):
        try:
            return _request_once(base, method, path, body, token)
        except (ConnectionResetError, urllib.error.URLError) as exc:
            last = exc
            time.sleep(0.2)
    raise last


def check(name, condition, detail=""):
    print(f"{PASS if condition else FAIL}  {name}" + (f"  -> {detail}" if detail and not condition else ""))
    return bool(condition)


def token_key_from_db(db_path, name):
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("select key from tokens where name = ? order by id desc limit 1", (name,)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:3001")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-pass", default=os.environ.get("ADMIN_PASS", ""))
    parser.add_argument("--limit", type=int, default=int(os.environ.get("GLOBAL_RATE_LIMIT_PER_MINUTE", "5")),
                        help="must match the cap the gateway was started with")
    parser.add_argument("--db", default=os.path.join("..", "run", "data", "new-api.db"))
    parser.add_argument("--model", default="minimax/abab6.5s-chat")
    args = parser.parse_args()

    base = args.base
    _, login = request(base, "POST", "/api/user/login", {"username": args.admin_user, "password": args.admin_pass})
    admin = login["data"]["access_token"]

    # Unlimited pools so nothing else can reject the calls under test.
    request(base, "PUT", "/api/community/admin/plans/1", {
        "window_1h_tokens": 0, "glm_1h_tokens": 0, "is_default": True,
    }, token=admin)
    request(base, "POST", "/api/community/admin/plans/1/reset-usage", {}, token=admin)

    key = token_key_from_db(args.db, "quota-e2e-dave")
    ok = check("测试用 token 存在（先跑 quota_e2e.py）", bool(key), "no token named quota-e2e-dave")
    if not ok:
        sys.exit(1)
    key = key if key.startswith("sk-") else "sk-" + key

    allowed, blocked = 0, 0
    first_blocked_at = None
    for i in range(1, args.limit + 4):
        status, payload = request(base, "POST", "/v1/chat/completions", {
            "model": args.model,
            "messages": [{"role": "user", "content": "ping"}],
        }, token=key)
        if status == 200:
            allowed += 1
        elif status == 429:
            blocked += 1
            if first_blocked_at is None:
                first_blocked_at = i
                first_payload = payload
        else:
            blocked += 1
            if first_blocked_at is None:
                first_blocked_at = i
                first_payload = payload

    ok &= check(f"限流前 {args.limit} 次放行", allowed == args.limit, f"allowed={allowed}")
    ok &= check("超出后返回 429", blocked > 0, f"blocked={blocked}")
    ok &= check(f"第 {args.limit + 1} 次开始被拦", first_blocked_at == args.limit + 1, f"first_blocked_at={first_blocked_at}")
    msg = json.dumps(first_payload if first_blocked_at else {}, ensure_ascii=False)
    ok &= check("拒绝文案说明是频率限制而非额度",
                "request limit" in msg or "请求上限" in msg or "请求过快" in msg, msg[:220])
    ok &= check("拒绝文案带上限值", str(args.limit) in msg, msg[:220])

    # Restore the shipped budgets: this test runs with unlimited pools so that
    # nothing but the rate limiter can reject a call, and leaving 0 behind would
    # make the console read "不限" for both pools.
    request(base, "PUT", "/api/community/admin/plans/1", {
        "window_1h_tokens": 30000000, "glm_1h_tokens": 3000000,
    }, token=admin)

    print("\nRESULT:", "ALL PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
