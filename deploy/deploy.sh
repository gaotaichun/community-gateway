#!/usr/bin/env bash
# Community gateway — interactive deployment wizard.
#
# Collects the Discord application, the guild gate, the site address, the
# administrator account and the Bifrost upstream; validates them; writes .env;
# builds and starts the container; then configures the gateway through its own
# admin API so a fresh machine ends up fully configured without clicking around.
#
# Usage:
#   bash deploy.sh                 # interactive deploy on this machine
#   bash deploy.sh --export out/   # build a clean, portable bundle for another host
#   bash deploy.sh --export out/ --with-config   # ... including this host's .env
#   bash deploy.sh --non-interactive   # read every value from the environment
#   bash deploy.sh --help
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="deploy"
EXPORT_DIR=""
WITH_CONFIG=0
NON_INTERACTIVE=0
BASE_URL="http://127.0.0.1:3000"
COMPOSE_FILE="$HERE/docker-compose.yml"

# ---------------------------------------------------------------- output ----
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_CYAN=$'\033[36m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_CYAN=""; C_BOLD=""
fi

say()  { printf '%s==>%s %s\n' "$C_CYAN" "$C_RESET" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%swarn%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%sERROR%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }
step() { printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"; }

usage() {
  # Print the leading comment block only; stop at the first real statement.
  awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "${BASH_SOURCE[0]}"
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --export) MODE="export"; EXPORT_DIR="${2:-}"; [ -n "$EXPORT_DIR" ] || die "--export 需要一个输出目录"; shift 2 ;;
    --with-config) WITH_CONFIG=1; shift ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    -h|--help) usage ;;
    *) die "未知参数: $1（用 --help 查看用法）" ;;
  esac
done

# --------------------------------------------------------------- prompts ----
prompt_value() {
  local prompt=$1 default=${2:-} answer
  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$prompt" "$default" >&2
  else
    printf '%s: ' "$prompt" >&2
  fi
  IFS= read -r answer || true
  [ -z "$answer" ] && answer="$default"
  printf '%s' "$answer"
}

prompt_secret() {
  local prompt=$1 answer
  printf '%s: ' "$prompt" >&2
  IFS= read -rs answer || true
  printf '\n' >&2
  printf '%s' "$answer"
}

# ask_required VAR "prompt" [default] [validator]
ask_required() {
  local var=$1 prompt=$2 default=${3:-} validator=${4:-}
  local current="${!var:-}" value err
  [ -n "$current" ] && default="$current"
  while :; do
    if [ "$NON_INTERACTIVE" = "1" ]; then
      value="$default"
      [ -n "$value" ] || die "非交互模式下缺少必填项 $var"
    else
      value=$(prompt_value "$prompt" "$default")
    fi
    if [ -z "$value" ]; then warn "这一项不能为空"; [ "$NON_INTERACTIVE" = "1" ] && die "缺少 $var"; continue; fi
    if [ -n "$validator" ]; then
      if ! err=$($validator "$value"); then warn "$err"; [ "$NON_INTERACTIVE" = "1" ] && die "$var 校验失败: $err"; continue; fi
    fi
    printf -v "$var" '%s' "$value"
    return 0
  done
}

# ask_optional VAR "prompt" [default] [validator]
ask_optional() {
  local var=$1 prompt=$2 default=${3:-} validator=${4:-}
  local current="${!var:-}" value err
  [ -n "$current" ] && default="$current"
  if [ "$NON_INTERACTIVE" = "1" ]; then
    printf -v "$var" '%s' "$default"
    return 0
  fi
  while :; do
    value=$(prompt_value "$prompt" "$default")
    if [ -n "$validator" ] && [ -n "$value" ]; then
      if ! err=$($validator "$value"); then warn "$err"; continue; fi
    fi
    printf -v "$var" '%s' "$value"
    return 0
  done
}

# ask_secret VAR "prompt" [default] [validator]
ask_secret() {
  local var=$1 prompt=$2 default=${3:-} validator=${4:-}
  local current="${!var:-}" value err
  [ -n "$current" ] && default="$current"
  while :; do
    if [ "$NON_INTERACTIVE" = "1" ]; then
      value="$default"
    else
      if [ -n "$default" ]; then
        printf '%s（回车沿用已保存的值）: ' "$prompt" >&2
        IFS= read -rs value || true
        printf '\n' >&2
        [ -z "$value" ] && value="$default"
      else
        value=$(prompt_secret "$prompt")
      fi
    fi
    if [ -z "$value" ]; then warn "这一项不能为空"; [ "$NON_INTERACTIVE" = "1" ] && die "缺少 $var"; continue; fi
    if [ -n "$validator" ]; then
      if ! err=$($validator "$value"); then warn "$err"; [ "$NON_INTERACTIVE" = "1" ] && die "$var 校验失败: $err"; continue; fi
    fi
    printf -v "$var" '%s' "$value"
    return 0
  done
}

# ------------------------------------------------------------ validators ----
v_site_url() {
  local url="${1%/}" rest
  case "$url" in *'<'*|*'>'*) echo "这是包里的占位符，请填真实的站点地址（如 https://ai.example.com）"; return 1 ;; esac
  case "$url" in http://*|https://*) ;; *) echo "必须以 http:// 或 https:// 开头"; return 1 ;; esac
  rest="${url#*://}"
  [ -n "$rest" ] || { echo "缺少域名"; return 1; }
  case "$rest" in */*) echo "只填站点根地址，不要带路径和结尾斜杠，例如 https://ai.example.com"; return 1 ;; esac
  if [ "${url#https://}" = "$url" ]; then
    case "$rest" in
      localhost*|127.0.0.1*) ;;
      *) echo "Discord 要求回调地址是 https；只有 localhost 可以用 http"; return 1 ;;
    esac
  fi
}

v_snowflake() {
  case "$1" in ''|*[!0-9]*) echo "应为纯数字 ID（Discord 里右键 → 复制 ID 得到）"; return 1 ;; esac
  [ ${#1} -ge 15 ] || { echo "ID 位数太短，不像 Discord ID"; return 1; }
}

v_role_list() {
  local list="${1// /}" id
  [ -z "$list" ] && return 0
  local IFS=','
  for id in $list; do
    case "$id" in ''|*[!0-9]*) echo "身份组 ID 用英文逗号分隔，只能包含数字"; return 1 ;; esac
  done
  return 0
}

v_invite() {
  [ -z "$1" ] && return 0
  case "$1" in
    https://discord.gg/*|https://discord.com/invite/*|https://discordapp.com/invite/*) return 0 ;;
    *) echo "邀请链接应形如 https://discord.gg/xxxx（可留空）"; return 1 ;;
  esac
}

v_password() {
  [ ${#1} -ge 8 ] || { echo "密码至少 8 位"; return 1; }
}

v_upstream_url() {
  local url="${1%/}"
  case "$url" in *'<'*|*'>'*) echo "这是包里的占位符，请填真实的 Bifrost 地址（同机可填 http://127.0.0.1:8080）"; return 1 ;; esac
  case "$url" in http://*|https://*) ;; *) echo "必须以 http:// 或 https:// 开头"; return 1 ;; esac
  case "$url" in
    */v1|*/v1/) echo "这里只填 Bifrost 根地址，不要带 /v1，网关会自己拼 /v1/chat/completions"; return 1 ;;
  esac
}

v_api_key() {
  case "$1" in sk-*) return 0 ;; *) echo "Bifrost 密钥通常以 sk- 开头，请确认没有粘错"; return 1 ;; esac
}

v_count() {
  case "$1" in ''|*[!0-9]*) echo "应为非负整数（0 表示不限）"; return 1 ;; esac
}

# ----------------------------------------------------------------- utils ----
json_escape() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\r'/}
  s=${s//$'\t'/\\t}
  printf '%s' "$s"
}

random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    od -An -tx1 -N32 /dev/urandom | tr -d ' \n'
  fi
}

# HTTP status of a loopback call: never go through an outbound proxy.
http_code() {
  local out
  out=$(curl -sS --noproxy '*' -o /dev/null -w '%{http_code}' --max-time "${2:-10}" "$1" 2>/dev/null) || true
  [ -n "$out" ] || out="000"
  printf '%s' "$out"
}

# HTTP status of an external call: honour HTTP_PROXY / HTTPS_PROXY.
http_code_external() {
  local out
  out=$(curl -sS -o /dev/null -w '%{http_code}' --max-time "${2:-10}" "$1" 2>/dev/null) || true
  [ -n "$out" ] || out="000"
  printf '%s' "$out"
}

api_call() {
  local method=$1 path=$2 body=${3:-}
  if [ -n "$body" ]; then
    curl -sS --noproxy '*' --max-time 30 -X "$method" "$BASE_URL$path" \
      -H 'Content-Type: application/json' -H "Authorization: Bearer ${TOKEN:-}" -d "$body"
  else
    curl -sS --noproxy '*' --max-time 30 -X "$method" "$BASE_URL$path" \
      -H "Authorization: Bearer ${TOKEN:-}"
  fi
}

json_field() {
  # json_field '<json>' field -> first match of "field":"value"
  printf '%s' "$1" | sed -n "s/.*\"$2\":\"\([^\"]*\)\".*/\1/p"
}

set_option() {
  local key=$1 value=$2 payload response
  payload=$(printf '{"key":"%s","value":"%s"}' "$(json_escape "$key")" "$(json_escape "$value")")
  response=$(api_call PUT /api/option/ "$payload")
  case "$response" in
    *'"success":true'*) ok "option $key" ;;
    *) die "设置 $key 失败: $response" ;;
  esac
}

# --------------------------------------------------------------- preflight --
# ---------------------------------------------------------------- binary ----
# The bundle ships one binary per architecture so the same tarball works on an
# x86_64 box and on an ARM (Oracle Ampere A1) box. Ask docker which platform it
# will actually run, because that — not the host — is what has to match.
detect_container_arch() {
  local arch=""
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    arch=$(docker info --format '{{.Architecture}}' 2>/dev/null || true)
  fi
  [ -n "$arch" ] || arch=$(uname -m)
  case "$arch" in
    x86_64|amd64|x86-64) printf 'amd64' ;;
    aarch64|arm64|armv8*|armv8l) printf 'arm64' ;;
    *) printf '' ;;
  esac
}

select_binary() {
  if [ -f "$HERE/bin/new-api-amd64" ] || [ -f "$HERE/bin/new-api-arm64" ]; then
    local arch
    arch=$(detect_container_arch)
    [ -n "$arch" ] || die "无法识别 CPU 架构（uname -m 返回 $(uname -m)），请手动指定"
    [ -f "$HERE/bin/new-api-$arch" ] || die "包内没有 linux/$arch 的二进制，请让构建机补一个"
    cp "$HERE/bin/new-api-$arch" "$HERE/bin/new-api"
    chmod 0755 "$HERE/bin/new-api"
    ok "已选择 linux/$arch 二进制"
  fi
  [ -f "$HERE/bin/new-api" ] || die "缺少 bin/new-api，请先在构建机执行 bash build.sh"
}

preflight() {
  step "1/6 环境检查"
  command -v curl >/dev/null 2>&1 || die "缺少 curl"
  if [ "$MODE" = "deploy" ]; then
    command -v docker >/dev/null 2>&1 || die "缺少 docker，请先安装 Docker Engine 或 Docker Desktop"
    docker compose version >/dev/null 2>&1 || die "缺少 docker compose（v2），请升级 Docker"
    docker info >/dev/null 2>&1 || die "Docker 守护进程没有运行，请先启动 Docker"
    ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null)"
    select_binary
  else
    [ -f "$HERE/bin/new-api" ] || [ -f "$HERE/bin/new-api-amd64" ] || die "缺少 bin/new-api，请先在构建机执行 bash build.sh"
    ok "二进制就绪"
  fi
  [ -f "$HERE/Dockerfile" ] || die "缺少 Dockerfile"
  [ -f "$HERE/docker-compose.yml" ] || die "缺少 docker-compose.yml"
}

# ----------------------------------------------------------------- config ---
# Read KEY<TAB>VALUE lines without evaluating them as shell code. Values already
# present in the environment win, so `SITE_URL=... bash deploy.sh` overrides the
# saved answer instead of being silently overwritten by it.
load_kv_file() {
  local file=$1 key value
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    key=${line%%$'\t'*}
    value=${line#*$'\t'}
    case "$key" in ''|*[!A-Za-z0-9_]*) continue ;; esac
    [ "$value" = "$line" ] && continue
    if [ -z "${!key:-}" ]; then printf -v "$key" '%s' "$value"; fi
  done < "$file"
}

# Parse a docker-compose style KEY=VALUE file (no shell evaluation).
load_env_file() {
  local file=$1 key value
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac
    key=${line%%=*}
    value=${line#*=}
    case "$key" in ''|*[!A-Za-z0-9_]*) continue ;; esac
    if [ -z "${!key:-}" ]; then printf -v "$key" '%s' "$value"; fi
  done < "$file"
}

load_existing_env() {
  if [ -f "$HERE/.deploy-config" ]; then
    load_kv_file "$HERE/.deploy-config"
    ok "沿用已有 .deploy-config 的取值作为默认值"
  fi
  if [ -f "$HERE/.env" ]; then
    load_env_file "$HERE/.env"
    ok "沿用已有 .env 的取值作为默认值"
  fi
  # Lower priority than the two files above. preset.local.env wins over the
  # generic preset.env; the local one is gitignored and only exists on the
  # machine that builds the release.
  if [ -f "$HERE/preset.local.env" ]; then
    load_env_file "$HERE/preset.local.env"
    ok "已载入 preset.local.env 的本机预设值"
  fi
  if [ -f "$HERE/preset.env" ]; then
    load_env_file "$HERE/preset.env"
    ok "已载入 preset.env 的预设值"
  fi
}

# Keep the collected answers next to the bundle so a re-run, or a deploy on
# another host, never has to ask again. Contains secrets: mode 600, never commit.
save_config() {
  umask 077
  {
    echo "# deploy.sh answers — 含密钥，权限 600，勿提交到版本库"
    printf 'SITE_URL\t%s\n' "$SITE_URL"
    printf 'DISCORD_CLIENT_ID\t%s\n' "$DISCORD_CLIENT_ID"
    printf 'DISCORD_CLIENT_SECRET\t%s\n' "$DISCORD_CLIENT_SECRET"
    printf 'DISCORD_GUILD_ID\t%s\n' "${DISCORD_GUILD_ID:-}"
    printf 'DISCORD_REQUIRED_ROLE_IDS\t%s\n' "${DISCORD_REQUIRED_ROLE_IDS:-}"
    printf 'DISCORD_INVITE_URL\t%s\n' "${DISCORD_INVITE_URL:-}"
    printf 'ADMIN_USER\t%s\n' "$ADMIN_USER"
    printf 'ADMIN_PASS\t%s\n' "$ADMIN_PASS"
    printf 'UPSTREAM_BASE_URL\t%s\n' "${UPSTREAM_BASE_URL:-}"
    printf 'UPSTREAM_API_KEY\t%s\n' "${UPSTREAM_API_KEY:-}"
    printf 'UPSTREAM_MODELS\t%s\n' "${UPSTREAM_MODELS:-}"
    printf 'OUTBOUND_PROXY\t%s\n' "${OUTBOUND_PROXY:-}"
    printf 'GLOBAL_RATE_LIMIT_PER_MINUTE\t%s\n' "${GLOBAL_RATE_LIMIT_PER_MINUTE:-30}"
    printf 'TZ_VALUE\t%s\n' "${TZ_VALUE:-Asia/Shanghai}"
  } > "$HERE/.deploy-config"
  chmod 600 "$HERE/.deploy-config"
  ok "配置已保存到 .deploy-config（权限 600，含密钥，勿提交）"
}

collect() {
  step "2/6 收集配置"
  printf '%s直接回车表示采用中括号里的默认值。%s\n' "$C_DIM" "$C_RESET"

  echo
  printf '%s站点地址%s\n' "$C_BOLD" "$C_RESET"
  printf '%s用户浏览器访问网关用的地址，必须和 Discord 里填的回调地址完全一致。%s\n' "$C_DIM" "$C_RESET"
  ask_required SITE_URL "站点地址（如 https://ai.example.com）" "${SITE_URL:-}" v_site_url
  SITE_URL="${SITE_URL%/}"

  echo
  printf '%sDiscord 应用%s\n' "$C_BOLD" "$C_RESET"
  printf '%sDiscord Developer Portal → 你的应用 → OAuth2，可拿到下面两项。%s\n' "$C_DIM" "$C_RESET"
  ask_required DISCORD_CLIENT_ID "Client ID" "${DISCORD_CLIENT_ID:-}" v_snowflake
  ask_secret   DISCORD_CLIENT_SECRET "Client Secret" "${DISCORD_CLIENT_SECRET:-}"

  echo
  printf '%sDiscord 服务器门禁%s\n' "$C_BOLD" "$C_RESET"
  printf '%s只判断「是不是这个服务器的成员」。服务器 ID 留空 = 不做门禁。%s\n' "$C_DIM" "$C_RESET"
  ask_optional DISCORD_GUILD_ID "Discord 服务器 ID" "${DISCORD_GUILD_ID:-}" v_snowflake
  ask_optional DISCORD_REQUIRED_ROLE_IDS "可选：只允许这些身份组（逗号分隔，留空=任何成员）" "${DISCORD_REQUIRED_ROLE_IDS:-}" v_role_list
  ask_optional DISCORD_INVITE_URL "可选：邀请链接（留空=拒绝时不展示）" "${DISCORD_INVITE_URL:-}" v_invite

  echo
  printf '%s管理员账号%s\n' "$C_BOLD" "$C_RESET"
  printf '%s用于首次初始化，以及 /sign-in?admin=1 应急登录。%s\n' "$C_DIM" "$C_RESET"
  ADMIN_USER="${ADMIN_USER:-}"
  if [ "$NON_INTERACTIVE" = "1" ]; then
    ADMIN_USER="${ADMIN_USER:-admin}"
  else
    ADMIN_USER="$(prompt_value "管理员用户名（≤12 字符）" "${ADMIN_USER:-admin}")"
  fi
  [ ${#ADMIN_USER} -le 12 ] || die "用户名最多 12 个字符"
  ask_secret ADMIN_PASS "管理员密码（至少 8 位）" "${ADMIN_PASS:-}" v_password

  echo
  printf '%s上游（Bifrost）%s\n' "$C_BOLD" "$C_RESET"
  printf '%s只填根地址，不带 /v1；密钥形如 sk-bf-xxx。留空可稍后在后台补。%s\n' "$C_DIM" "$C_RESET"
  ask_optional UPSTREAM_BASE_URL "Bifrost 根地址（如 https://bifrost.example.com）" "${UPSTREAM_BASE_URL:-}" v_upstream_url
  UPSTREAM_BASE_URL="${UPSTREAM_BASE_URL%/}"
  if [ -n "$UPSTREAM_BASE_URL" ]; then
    ask_secret UPSTREAM_API_KEY "Bifrost 密钥（sk-...）" "${UPSTREAM_API_KEY:-}" v_api_key
    ask_optional UPSTREAM_MODELS "对外暴露的模型名（逗号分隔，例 Z.ai/glm-5.3-flash,openrouter/minimax/minimax-m3）" "${UPSTREAM_MODELS:-}"
  fi

  echo
  printf '%s额度与限流%s\n' "$C_BOLD" "$C_RESET"
  printf '%s每小时 token 额度在后台「套餐」里设置（默认 MiniMax 30M / GLM 3M，全站共享）。%s\n' "$C_DIM" "$C_RESET"
  printf '%s这里只设全站每分钟请求上限，防止有人写死循环把上游打爆。%s\n' "$C_DIM" "$C_RESET"
  ask_optional GLOBAL_RATE_LIMIT_PER_MINUTE "全站每分钟最多调用次数（0=不限）" "${GLOBAL_RATE_LIMIT_PER_MINUTE:-30}" v_count

  echo
  printf '%s其他%s\n' "$C_BOLD" "$C_RESET"
  ask_optional OUTBOUND_PROXY "容器出网代理（如 http://172.17.0.1:7890，留空=直连）" "${OUTBOUND_PROXY:-}"
  ask_optional TZ_VALUE "时区" "${TZ_VALUE:-Asia/Shanghai}"
}

# --------------------------------------------------------------- validate ---
# The gateway runs inside a container, so a loopback upstream address means the
# container itself, not the host. Rewrite it to the docker host alias (mapped in
# docker-compose.yml via extra_hosts) before it is written into the channel.
# Every other address is used verbatim.
container_url() {
  local url=$1
  case "$url" in
    *//127.0.0.1[:/]*|*//127.0.0.1) printf '%s' "${url/\/\/127.0.0.1/\/\/host.docker.internal}" ;;
    *//localhost[:/]*|*//localhost) printf '%s' "${url/\/\/localhost/\/\/host.docker.internal}" ;;
    *) printf '%s' "$url" ;;
  esac
}

validate() {
  step "3/6 连通性校验"

  local code
  code=$(http_code_external "https://discord.com/api/v10/gateway" 8)
  case "$code" in
    200) ok "Discord API 可达" ;;
    000)
      warn "从本机访问不到 discord.com（返回 000）。"
      warn "如果这台机器需要代理才能出网，请在上一步填写容器出网代理。"
      warn "容器内若走不通，Discord 登录会在换取 token 时失败。"
      ;;
    *) warn "Discord API 返回 HTTP $code，请检查网络" ;;
  esac

  if [ -n "${UPSTREAM_BASE_URL:-}" ]; then
    local probe
    probe=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 12 \
      -H "Authorization: Bearer ${UPSTREAM_API_KEY}" \
      "${UPSTREAM_BASE_URL}/v1/models" 2>/dev/null) || true
    [ -n "$probe" ] || probe="000"
    case "$probe" in
      200) ok "上游 $UPSTREAM_BASE_URL 可达（/v1/models 返回 200）" ;;
      401|403) warn "上游可达但密钥被拒（HTTP $probe）→ 密钥或预算问题，稍后请求会返回同样的错误" ;;
      404) warn "上游返回 404 → 根地址可能填错（不要带 /v1），或该上游没有 /v1/models 端点" ;;
      000) warn "连不上 $UPSTREAM_BASE_URL → 网关不可达或需要代理" ;;
      *) warn "上游返回 HTTP $probe，请人工确认" ;;
    esac
  fi

  local redirect="${SITE_URL}/oauth/discord"
  echo
  printf '%s请把下面这一行原样填进 Discord Developer Portal → OAuth2 → Redirects：%s\n' "$C_BOLD" "$C_RESET"
  printf '  %s%s%s\n' "$C_GREEN" "$redirect" "$C_RESET"
  echo
}

# -------------------------------------------------------------------- env ---
write_env() {
  step "4/6 写入 .env"
  local session_secret="${SESSION_SECRET:-}"
  [ -n "$session_secret" ] || session_secret=$(random_secret)

  {
    echo "# Generated by deploy.sh on $(date '+%Y-%m-%d %H:%M:%S')"
    echo "# 不要把本文件提交到版本库。"
    echo "TZ=${TZ_VALUE:-Asia/Shanghai}"
    echo "PORT=3000"
    echo "SQLITE_PATH=/data/new-api.db"
    echo "SESSION_SECRET=${session_secret}"
    echo "NODE_NAME=community-gateway"
    echo "ERROR_LOG_ENABLED=true"
    echo "BATCH_UPDATE_ENABLED=true"
    echo "GIN_MODE=release"
    echo "# 全站每分钟请求上限（所有用户合计），0 = 不限。"
    echo "GLOBAL_RATE_LIMIT_PER_MINUTE=${GLOBAL_RATE_LIMIT_PER_MINUTE:-30}"
    if [ -n "${OUTBOUND_PROXY:-}" ]; then
      echo "HTTP_PROXY=${OUTBOUND_PROXY}"
      echo "HTTPS_PROXY=${OUTBOUND_PROXY}"
      echo "NO_PROXY=127.0.0.1,localhost,gateway"
    fi
  } > "$HERE/.env"
  chmod 600 "$HERE/.env"
  ok ".env 已写入（权限 600）"
}

# ------------------------------------------------------------------- boot ---
bring_up() {
  step "5/6 构建镜像并启动"
  docker compose -f "$COMPOSE_FILE" --env-file "$HERE/.env" up -d --build

  say "等待网关就绪（最多 120 秒）"
  local i code
  for i in $(seq 1 120); do
    code=$(http_code "$BASE_URL/api/status" 2)
    [ "$code" = "200" ] && { ok "网关已就绪"; return 0; }
    sleep 1
  done
  docker compose -f "$COMPOSE_FILE" logs --tail 40 || true
  die "网关 120 秒内没有就绪，请查看上面的日志"
}

# -------------------------------------------------------------- bootstrap ---
bootstrap() {
  step "6/6 初始化与后台配置"

  local setup
  setup=$(api_call GET /api/setup || true)
  if printf '%s' "$setup" | grep -q '"status":true'; then
    ok "系统已初始化，跳过创建管理员"
  else
    local payload
    payload=$(printf '{"username":"%s","password":"%s","confirmPassword":"%s"}' \
      "$(json_escape "$ADMIN_USER")" "$(json_escape "$ADMIN_PASS")" "$(json_escape "$ADMIN_PASS")")
    local created
    created=$(api_call POST /api/setup "$payload")
    case "$created" in
      *'"success":true'*) ok "管理员 $ADMIN_USER 已创建" ;;
      *) die "初始化失败: $created" ;;
    esac
  fi

  local login
  login=$(api_call POST /api/user/login \
    "$(printf '{"username":"%s","password":"%s"}' "$(json_escape "$ADMIN_USER")" "$(json_escape "$ADMIN_PASS")")")
  TOKEN=$(json_field "$login" access_token)
  [ -n "$TOKEN" ] || die "登录失败，请检查管理员账号: $login"
  ok "已登录后台"

  say "写入站点地址与 Discord 配置"
  set_option ServerAddress "$SITE_URL"
  set_option discord.client_id "$DISCORD_CLIENT_ID"
  set_option discord.client_secret "$DISCORD_CLIENT_SECRET"
  set_option discord.guild_id "${DISCORD_GUILD_ID:-}"
  set_option discord.required_role_ids "${DISCORD_REQUIRED_ROLE_IDS:-}"
  set_option discord.invite_url "${DISCORD_INVITE_URL:-}"
  set_option checkin_setting.enabled false
  # Self-use mode: an unpriced model is billed at a fallback ratio instead of
  # being rejected with "not been priced by the administrator". The gateway
  # meters tokens per hour, not money, and the model catalogue is owned by
  # Bifrost — so a model the upstream starts serving must work immediately,
  # without anyone remembering to add a price here first.
  set_option SelfUseModeEnabled true
  set_option PasswordLoginEnabled false
  set_option PasswordRegisterEnabled false
  # RegisterEnabled must stay true: the first Discord sign-in creates the account.
  set_option RegisterEnabled true
  set_option discord.enabled true

  if [ -n "${UPSTREAM_BASE_URL:-}" ] && [ -n "${UPSTREAM_API_KEY:-}" ]; then
    say "创建 Bifrost 渠道"
    local channel_url
    channel_url=$(container_url "$UPSTREAM_BASE_URL")
    local channel
    channel=$(printf '{"mode":"single","channel":{"name":"bifrost","type":1,"base_url":"%s","key":"%s","models":"%s","group":"default"}}' \
      "$(json_escape "$channel_url")" "$(json_escape "$UPSTREAM_API_KEY")" "$(json_escape "${UPSTREAM_MODELS:-}")")
    local created_channel
    created_channel=$(api_call POST /api/channel/ "$channel")
    case "$created_channel" in
      *'"success":true'*)
        if [ "$channel_url" != "$UPSTREAM_BASE_URL" ]; then
          ok "渠道已创建（base_url=${channel_url}，由 ${UPSTREAM_BASE_URL} 改写而来）"
        else
          ok "渠道已创建（base_url=${channel_url}）"
        fi
        ;;
      *) warn "渠道创建失败，请稍后在后台手动添加: $created_channel" ;;
    esac
  else
    warn "未提供上游，稍后可在后台「渠道」里添加"
  fi
}

summary() {
  echo
  printf '%s部署完成%s\n' "$C_GREEN$C_BOLD" "$C_RESET"
  cat <<EOF

  站点地址      ${SITE_URL}
  回调地址      ${SITE_URL}/oauth/discord
  管理后台      ${SITE_URL}/sign-in?admin=1
  管理员        ${ADMIN_USER}
  容器           community-gateway（仅监听 127.0.0.1:3000）
  数据卷         community-gateway-data

接下来：
  1. 在 Cloudflare Tunnel 或反向代理里把 ${SITE_URL} 指向 http://127.0.0.1:3000
  2. 确认 Discord Developer Portal 的 Redirects 里已加入 ${SITE_URL}/oauth/discord
  3. 浏览器打开 ${SITE_URL}，点「Discord 登录」走一次完整流程

常用命令：
  docker compose -f ${COMPOSE_FILE} logs -f      # 看日志
  docker compose -f ${COMPOSE_FILE} restart      # 重启
  docker compose -f ${COMPOSE_FILE} down         # 停止（数据保留在数据卷里）

EOF
}

# ----------------------------------------------------------------- export ---
export_bundle() {
  step "打包部署包"
  mkdir -p "$EXPORT_DIR"
  local name="community-gateway-$(date '+%Y%m%d')"
  local staging="$EXPORT_DIR/$name"
  rm -rf "$staging"
  mkdir -p "$staging/bin"

  # Shipped artifacts only. build.sh stays behind: it needs Go and bun, and the
  # receiving host is only supposed to run the container.
  cp "$HERE/Dockerfile" "$HERE/docker-compose.yml" "$HERE/deploy.sh" "$HERE/README.md" "$staging/"
  cp "$HERE/.env.example" "$staging/.env.example"
  # Ship the machine-local preset when it exists: the tracked preset.env only
  # carries placeholders, the real upstream lives in the gitignored local file.
  if [ -f "$HERE/preset.local.env" ]; then
    cp "$HERE/preset.local.env" "$staging/preset.env"
  else
    cp "$HERE/preset.env" "$staging/preset.env"
  fi
  [ -f "$HERE/.dockerignore" ] && cp "$HERE/.dockerignore" "$staging/.dockerignore"

  # One binary per architecture: deploy.sh picks the right one at install time.
  local copied=0
  for arch in amd64 arm64; do
    if [ -f "$HERE/bin/new-api-$arch" ]; then
      cp "$HERE/bin/new-api-$arch" "$staging/bin/new-api-$arch"
      chmod 0755 "$staging/bin/new-api-$arch"
      copied=$((copied + 1))
    fi
  done
  if [ "$copied" = "0" ]; then
    cp "$HERE/bin/new-api" "$staging/bin/new-api"
    chmod 0755 "$staging/bin/new-api"
  fi
  chmod 0755 "$staging/deploy.sh"
  [ -d "$HERE/licenses" ] && cp -r "$HERE/licenses" "$staging/licenses"

  if [ "$WITH_CONFIG" = "1" ]; then
    [ -f "$HERE/.env" ] || die "--with-config 需要当前目录已有 .env（先跑一次交互式部署）"
    cp "$HERE/.env" "$staging/.env"
    chmod 600 "$staging/.env"
    if [ -f "$HERE/.deploy-config" ]; then
      cp "$HERE/.deploy-config" "$staging/.deploy-config"
      chmod 600 "$staging/.deploy-config"
    fi
  fi

  # Run tar from inside the output directory so no absolute path (which may
  # contain a Windows drive letter) is ever passed as a tar argument.
  ( cd "$EXPORT_DIR" \
      && tar -czf "$name.tar.gz" "$name" \
      && sha256sum "$name.tar.gz" > "$name.tar.gz.sha256" )
  rm -rf "$staging"

  ok "已生成 $EXPORT_DIR/$name.tar.gz"
  printf '   %s\n' "$(cat "$EXPORT_DIR/$name.tar.gz.sha256")"

  if [ "$WITH_CONFIG" = "1" ]; then
    cat <<EOF

包内已包含本机的 .env 与 .deploy-config（含密钥），只适合内部迁移。
拷到服务器后执行：

  tar -xzf $name.tar.gz && cd $name
  bash deploy.sh --non-interactive     # 直接复用包内 .env 与全部配置
EOF
  else
    cat <<EOF

包内不含任何密钥或本机配置，只有 .env.example 作为模板。
拷到服务器后执行：

  tar -xzf $name.tar.gz && cd $name
  bash deploy.sh                       # 交互式向导，6 步填完即上线

向导会现问 Discord 应用、服务器 ID、站点地址、管理员账密与 Bifrost 地址。
EOF
  fi
}

# ------------------------------------------------------------------- main ---
preflight
# Read-only: only fills defaults from files that may exist. Safe in both modes,
# and it makes `--export` show which preset it picked up.
load_existing_env
if [ "$MODE" = "export" ]; then
  # A clean bundle ships no configuration, so nothing has to be collected here.
  # --with-config simply reuses this host's existing .env and .deploy-config.
  export_bundle
else
  collect
  validate
  write_env
  save_config
  bring_up
  bootstrap
  summary
fi
