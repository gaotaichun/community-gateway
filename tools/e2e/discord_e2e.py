#!/usr/bin/env python3
"""End-to-end checks for the Discord login + guild gate.

Runs against a live gateway whose DISCORD_API_BASE points at tools/mockdiscord.
No third-party dependencies: urllib only.

Usage:
  python tools/e2e/discord_e2e.py --base http://127.0.0.1:3000 \
      --admin-user admin --admin-pass "$ADMIN_PASS" --scenario login
"""
import argparse
import os
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"


def request(base, method, path, body=None, token=None, allow_error=False):
    url = base.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode()
        if allow_error:
            try:
                return exc.code, json.loads(payload or "{}")
            except json.JSONDecodeError:
                return exc.code, {"raw": payload}
        raise RuntimeError(f"{method} {path} -> {exc.code}: {payload}")


def admin_token(base, user, password):
    _, payload = request(base, "POST", "/api/user/login",
                         {"username": user, "password": password})
    if not payload.get("success"):
        raise RuntimeError(f"admin login failed: {payload}")
    return payload["data"]["access_token"]


def set_option(base, token, key, value):
    request(base, "PUT", "/api/option/", {"key": key, "value": value}, token=token)


def configure_discord(base, token, client_id="mock-client", client_secret="mock-secret"):
    # Credentials first: enabling a provider without a client id is rejected.
    set_option(base, token, "discord.client_id", client_id)
    set_option(base, token, "discord.client_secret", client_secret)
    set_option(base, token, "discord.enabled", True)


def oauth_login(base, code):
    """Full server-side OAuth flow: state -> callback -> login bundle."""
    _, state_payload = request(base, "POST", "/api/oauth/state",
                               {"provider": "discord", "intent": "login"})
    if not state_payload.get("success"):
        return state_payload
    flow_token = state_payload["data"]["flow_token"]
    query = urllib.parse.urlencode({"code": code, "state": flow_token})
    status, payload = request(base, "GET", "/api/oauth/discord?" + query, allow_error=True)
    return payload


def self_user(base, token):
    _, payload = request(base, "GET", "/api/user/self", token=token)
    return payload.get("data") or {}


def check(name, condition, detail=""):
    print(f"{PASS if condition else FAIL}  {name}{('  -> ' + detail) if detail and not condition else ''}")
    return condition


def scenario_login(args):
    token = admin_token(args.base, args.admin_user, args.admin_pass)
    configure_discord(args.base, token)
    result = oauth_login(args.base, "code-alice")
    ok = check("Discord 登录返回 success", result.get("success") is True, json.dumps(result, ensure_ascii=False)[:300])
    if not ok:
        return False
    access = result["data"]["access_token"]
    user = self_user(args.base, access)
    ok &= check("discord_id 已落库", user.get("discord_id") == "100000000000000001", str(user.get("discord_id")))
    ok &= check("discord_avatar 已落库", "cdn.discordapp.com/avatars" in (user.get("discord_avatar") or ""), str(user.get("discord_avatar")))
    ok &= check("昵称来自 global_name", user.get("display_name") in ("Alice", "alice"), str(user.get("display_name")))
    return ok


def scenario_gate(args):
    """P2: non-member / missing role / compliant."""
    token = admin_token(args.base, args.admin_user, args.admin_pass)
    configure_discord(args.base, token)
    set_option(args.base, token, "discord.guild_id", args.guild_id)
    set_option(args.base, token, "discord.required_role_ids", args.role_id)
    set_option(args.base, token, "discord.invite_url", "https://discord.gg/example")

    ok = True
    # 1) member with the required role
    result = oauth_login(args.base, "code-alice")
    ok &= check("合规成员可登录", result.get("success") is True, json.dumps(result, ensure_ascii=False)[:300])

    # 2) not a member at all
    result = oauth_login(args.base, "code-bob")
    msg = json.dumps(result, ensure_ascii=False)
    ok &= check("非成员被拒绝", result.get("success") is False, msg[:300])
    ok &= check("非成员提示邀请链接", "discord.gg/example" in msg, msg[:300])

    # 3) member without the required role
    result = oauth_login(args.base, "code-carol")
    msg = json.dumps(result, ensure_ascii=False)
    ok &= check("无身份组被拒绝", result.get("success") is False, msg[:300])
    return ok


def scenario_member_only(args):
    """Production shape: only guild membership matters, no roles, no invite hint."""
    token = admin_token(args.base, args.admin_user, args.admin_pass)
    configure_discord(args.base, token)
    set_option(args.base, token, "discord.guild_id", args.guild_id)
    set_option(args.base, token, "discord.required_role_ids", "")
    set_option(args.base, token, "discord.invite_url", "")

    ok = True
    # 1) plain member, no special role at all -> allowed
    result = oauth_login(args.base, "code-dave")
    ok &= check("普通成员（无任何身份组）可登录", result.get("success") is True,
                json.dumps(result, ensure_ascii=False)[:300])

    # 2) member holding an unrelated role -> roles are ignored, still allowed
    result = oauth_login(args.base, "code-carol")
    ok &= check("成员持有无关身份组也可登录", result.get("success") is True,
                json.dumps(result, ensure_ascii=False)[:300])

    # 3) not a member -> rejected, and the message must not advertise an invite
    result = oauth_login(args.base, "code-bob")
    msg = json.dumps(result, ensure_ascii=False)
    ok &= check("非成员被拒绝", result.get("success") is False, msg[:300])
    ok &= check("拒绝文案为「仅限本社区 Discord 成员使用」", "仅限本社区 Discord 成员使用" in msg, msg[:300])
    ok &= check("拒绝文案不含邀请链接", "discord.gg" not in msg and "邀请" not in msg, msg[:300])
    ok &= check("拒绝原因标记为 not_member", "not_member" in msg, msg[:300])
    return ok


def scenario_skip_gate(args):
    """guild_id empty => gate disabled, everyone with a Discord account passes."""
    token = admin_token(args.base, args.admin_user, args.admin_pass)
    configure_discord(args.base, token)
    set_option(args.base, token, "discord.guild_id", "")
    result = oauth_login(args.base, "code-bob")
    return check("guild_id 为空时跳过门禁", result.get("success") is True, json.dumps(result, ensure_ascii=False)[:300])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:3000")
    parser.add_argument("--admin-user", default="admin")
    parser.add_argument("--admin-pass", default=os.environ.get("ADMIN_PASS", ""),
                        help="管理员密码，或用环境变量 ADMIN_PASS 提供")
    parser.add_argument("--scenario", default="login", choices=["login", "gate", "member-only", "skip-gate"])
    parser.add_argument("--guild-id", default="900000000000000001")
    parser.add_argument("--role-id", default="800000000000000001")
    args = parser.parse_args()

    scenarios = {
        "login": scenario_login,
        "gate": scenario_gate,
        "member-only": scenario_member_only,
        "skip-gate": scenario_skip_gate,
    }
    ok = scenarios[args.scenario](args)
    print("\nRESULT:", "ALL PASSED" if ok else "FAILED")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
