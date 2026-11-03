#!/usr/bin/env python3
"""P7 — 通过网关调用上游，并证明三类失败原因可区分。

1. 正常请求：/v1/chat/completions 返回 200 且内容是上游给的
2. 模型名错：返回 model_not_found 一类错误，而不是「网关挂了」
3. 密钥/预算错：上游 401 透出，且提示指向密钥
4. 网关不可达：连接失败，提示指向渠道地址/网络

脚本会在最后把渠道恢复原样。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
    os.environ.pop(key, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
ok_all = True


def check(name, condition, detail=""):
    global ok_all
    ok_all = ok_all and bool(condition)
    print(f"{PASS if condition else FAIL}  {name}" + (f"  -> {detail}" if detail and not condition else ""))
    return condition


def request(base, method, path, body=None, token=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else {"raw": raw})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}
    except Exception as exc:  # noqa: BLE001 - connection failures are data here
        return 0, {"raw": f"{type(exc).__name__}: {exc}"}


def put_channel(base, token, body, attempts=5):
    """PUT a channel patch, retrying transient SQLITE_BUSY.

    SQLite allows a single writer, so an admin write that races a request's
    quota update can come back as "database is locked". The gateway serialises
    most of it, but a short retry keeps this harness deterministic.
    """
    last = {}
    for attempt in range(attempts):
        _, payload = request(base, "PUT", "/api/channel/", body, token=token)
        if payload.get("success"):
            return payload
        last = payload
        if "locked" not in str(payload.get("message", "")).lower():
            return payload
        time.sleep(0.3 * (attempt + 1))
    return last


def login(base, user, password):
    _, payload = request(base, "POST", "/api/user/login", {"username": user, "password": password})
    return payload["data"]["access_token"]


def ensure_token(base, token, name="p7-e2e"):
    """Create the API key if needed and return its plaintext value.

    The list endpoint masks keys, so the plaintext is read from the SQLite file
    (either a local path or copied out of the container with `docker cp`).
    """
    _, listing = request(base, "GET", "/api/token/?p=1&size=50", token=token)
    data = listing.get("data") or {}
    items = data.get("items") if isinstance(data, dict) else data
    exists = any(item.get("name") == name for item in items or [])
    if not exists:
        _, created = request(
            base,
            "POST",
            "/api/token/",
            {"name": name, "remain_quota": 0, "unlimited_quota": True, "expired_time": -1, "model_limits_enabled": False},
            token=token,
        )
        if not created.get("success"):
            raise SystemExit(f"创建 token 失败: {created}")
    return read_plaintext_key(name)


def read_plaintext_key(name, container="community-gateway", db_path=None):
    import sqlite3
    import subprocess
    import tempfile

    path = db_path or os.environ.get("SQLITE_PATH")
    tmp = None
    if not path:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        subprocess.run(["docker", "cp", f"{container}:/data/new-api.db", tmp.name], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        path = tmp.name
    try:
        conn = sqlite3.connect(path)
        row = conn.execute("select key from tokens where name = ? order by id desc limit 1", (name,)).fetchone()
        conn.close()
    finally:
        if tmp is not None:
            os.unlink(tmp.name)
    if not row:
        raise SystemExit(f"数据库里找不到 token {name}")
    key = row[0]
    return key if key.startswith("sk-") else "sk-" + key


def ensure_pricing(base, token, model):
    _, payload = request(base, "GET", "/api/option/", token=token)
    ratios = {}
    for option in payload.get("data") or []:
        if option.get("key") == "ModelRatio":
            raw = option.get("value") or "{}"
            try:
                ratios = json.loads(raw)
            except json.JSONDecodeError:
                ratios = {}
    if model in ratios:
        return
    ratios[model] = 1
    _, result = request(
        base, "PUT", "/api/option/", {"key": "ModelRatio", "value": json.dumps(ratios)}, token=token
    )
    if not result.get("success"):
        raise SystemExit(f"写入模型倍率失败: {result}")


def chat(base, api_key, model, timeout=60):
    status, payload = request(
        base,
        "POST",
        "/v1/chat/completions",
        {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 16},
        token=api_key,
        timeout=timeout,
    )
    return status, payload


def describe(payload):
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return f"{err.get('message')} | type={err.get('type')} code={err.get('code')}"
        if "message" in payload:
            return str(payload["message"])
        return json.dumps(payload, ensure_ascii=False)[:200]
    return str(payload)[:200]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:3000")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-pass", default=os.environ.get("ADMIN_PASS", ""),
                        help="管理员密码，或用环境变量 ADMIN_PASS 提供")
    parser.add_argument("--model", default="minimax/abab6.5s-chat")
    parser.add_argument("--channel-name", default="bifrost")
    parser.add_argument("--channel-key", default=os.environ.get("UPSTREAM_API_KEY", "sk-bf-test"))
    parser.add_argument(
        "--channel-base-url",
        default=os.environ.get("UPSTREAM_BASE_URL"),
        help="渠道应该指向的根地址；默认沿用渠道当前值",
    )
    parser.add_argument("--api-key", default=os.environ.get("API_KEY"), help="直接指定 sk- 密钥，默认从数据库读取")
    parser.add_argument("--db", default=os.environ.get("SQLITE_PATH"), help="本地 SQLite 路径（默认从容器拷出）")
    args = parser.parse_args()

    admin = login(args.base, args.admin_user, args.admin_pass)
    if args.api_key:
        api_key = args.api_key
    else:
        ensure_token(args.base, admin)
        api_key = read_plaintext_key("p7-e2e", db_path=args.db)
    ensure_pricing(args.base, admin, args.model)

    _, listing = request(args.base, "GET", "/api/channel/?p=1", token=admin)
    data = listing.get("data") or {}
    channels = data.get("items") if isinstance(data, dict) else data
    channel = next((c for c in channels or [] if c.get("name") == args.channel_name), None)
    if channel is None:
        raise SystemExit(f"找不到渠道 {args.channel_name}，请先部署向导创建")

    # The list endpoint hides the key, and PUT rejects read-only fields such as
    # `status`, so build a minimal writable patch from scratch.
    def patch(**overrides):
        body = {
            "id": channel["id"],
            "name": channel["name"],
            "type": channel["type"],
            "base_url": channel["base_url"],
            "models": channel["models"],
            "group": channel["group"],
            "key": args.channel_key,
        }
        body.update(overrides)
        return body

    original_base = args.channel_base_url or channel["base_url"]
    original_key = args.channel_key

    print("\n== 0. 归一化渠道（复位 key 与 base_url） ==")
    normalized = put_channel(args.base, admin, patch(base_url=original_base, key=original_key))
    check("渠道已归一化", bool(normalized.get("success")), str(normalized)[:200])

    print("\n== 1. 正常请求 ==")
    status, payload = chat(args.base, api_key, args.model)
    content = ""
    if isinstance(payload, dict):
        choices = payload.get("choices") or []
        if choices:
            content = (choices[0].get("message") or {}).get("content", "")
    check("HTTP 200", status == 200, f"status={status} {describe(payload)}")
    check("返回上游内容", "pong" in content, f"content={content!r}")
    check("带 usage", bool(payload.get("usage")), str(payload.get("usage")))

    print("\n== 2. 模型名不存在 ==")
    status, payload = chat(args.base, api_key, "missing/model")
    text = describe(payload)
    print(f"     HTTP {status} | {text}")
    check("被拒绝（非 200）", status != 200, f"status={status}")
    check("错误指向模型名", "model" in text.lower(), text)

    print("\n== 3. 上游密钥被拒 ==")
    updated = put_channel(args.base, admin, patch(key="bad-key"))
    if not updated.get("success"):
        print(f"{FAIL}  改渠道密钥失败: {updated}")
    time.sleep(1)
    status, payload = chat(args.base, api_key, args.model)
    text = describe(payload)
    print(f"     HTTP {status} | {text}")
    check("请求失败", status != 200, f"status={status}")
    check("提示指向密钥/鉴权", any(w in text.lower() for w in ("key", "auth", "401", "invalid")), text)

    print("\n== 4. 上游不可达 ==")
    updated = put_channel(args.base, admin, patch(base_url="http://127.0.0.1:59999"))
    if not updated.get("success"):
        print(f"{FAIL}  改渠道地址失败: {updated}")
    time.sleep(1)
    status, payload = chat(args.base, api_key, args.model)
    text = describe(payload)
    print(f"     HTTP {status} | {text}")
    check("请求失败", status != 200, f"status={status}")
    check(
        "提示指向连接/网络",
        any(w in text.lower() for w in ("connect", "dial", "refused", "timeout", "上游", "channel", "no available")),
        text,
    )

    print("\n== 恢复渠道 ==")
    restored = put_channel(args.base, admin, patch(base_url=original_base, key=original_key))
    check("渠道已恢复", bool(restored.get("success")), str(restored)[:200])
    time.sleep(1)
    status, payload = chat(args.base, api_key, args.model)
    check("恢复正常后可继续请求", status == 200, f"status={status} {describe(payload)}")

    print("\nRESULT:", "ALL PASSED" if ok_all else "FAILED")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
