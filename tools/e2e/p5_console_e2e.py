#!/usr/bin/env python3
"""P5 console verification: the pages must mirror what the admin API stores.

Usage:
  python p5_console_e2e.py --set 2,10,30      # write the three windows (USD)
  python p5_console_e2e.py --show             # print the plan/self payload
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:3000")
NO_PROXY = {"127.0.0.1", "localhost"}
for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(key, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def request(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
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
    print(f"{PASS if condition else FAIL}  {name}" + (f"  -> {detail}" if detail else ""))
    return bool(condition)


def login(user, password):
    _, payload = request("POST", "/api/user/login", {"username": user, "password": password})
    if not payload.get("success"):
        raise SystemExit(f"login failed: {payload}")
    return payload["data"]["access_token"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-pass", default=os.environ.get("ADMIN_PASS", ""),
                        help="管理员密码，或用环境变量 ADMIN_PASS 提供")
    parser.add_argument("--plan-id", type=int, default=1)
    parser.add_argument("--set", default=None, help="5h,7d,30d in USD")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()

    token = login(args.admin_user, args.admin_pass)
    _, status = request("GET", "/api/status")
    unit = int(status["data"].get("quota_per_unit") or 500000)
    print(f"quota_per_unit = {unit}")

    if args.set:
        usd = [float(x) for x in args.set.split(",")]
        body = {
            "window_5h_quota": round(usd[0] * unit),
            "window_7d_quota": round(usd[1] * unit),
            "window_30d_quota": round(usd[2] * unit),
        }
        code, payload = request("PUT", f"/api/community/admin/plans/{args.plan_id}", body, token)
        print("PUT", code, json.dumps(payload, ensure_ascii=False)[:300])

    _, plans = request("GET", "/api/community/admin/plans", token=token)
    for plan in plans.get("data") or []:
        print(
            "plan",
            plan["id"],
            plan["title"],
            "enabled=", plan["enabled"],
            "default=", plan["is_default"],
            "windows(USD)=",
            [round(plan[k] / unit, 4) for k in ("window_5h_quota", "window_7d_quota", "window_30d_quota")],
        )

    if args.show:
        _, self_plan = request("GET", "/api/community/plan/self", token=token)
        data = self_plan.get("data") or {}
        print("plan_title:", data.get("plan_title"))
        for window in data.get("windows") or []:
            print(
                "  window",
                window["name"],
                "quota=", round(window["quota"] / unit, 4),
                "used=", round(window["used"] / unit, 4),
                "remaining=", round(window["remaining"] / unit, 4),
                "started=", window.get("started"),
                "resets_at=", window.get("resets_at"),
            )
        check("three windows returned", len(data.get("windows") or []) == 3)


if __name__ == "__main__":
    main()
