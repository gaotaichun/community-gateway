# 社区 AI API 网关 · 部署与使用说明

一个给 Discord 社区用的 API 网关：**只负责「谁有资格用」和「全站每小时能用多少」**，
模型路由、上游密钥、模型治理全部交给已有的 Bifrost。

```
Discord 用户
   │  ① Discord OAuth2 登录（identify + guilds + guilds.members.read）
   ▼
本网关（new-api 底座 + 社区门禁 + 两个全站 token 池）
   │  ② 校验：是否在该 Discord 服务器 / 该模型所属池是否还有余量 / 全站每分钟次数
   ▼
Bifrost（OpenAI 兼容）  ──►  MiniMax / OpenRouter / …
```

---

## 目录

- [一、前置条件](#一前置条件)
- [二、创建 Discord 应用](#二创建-discord-应用)
- [三、拿到服务器 ID](#三拿到服务器-id)
- [四、部署](#四部署)
- [五、后台配置](#五后台配置)
- [六、日常运维](#六日常运维)
- [七、排障手册](#七排障手册)
- [八、安全清单](#八安全清单)

---

## 一、前置条件

| 需要 | 说明 |
|---|---|
| 一台 Linux 服务器 | 1 核 1G 起，只需装 Docker Engine 与 docker compose v2 |
| 一个域名 | 必须 HTTPS。Discord 只接受 `https://` 回调（`http://localhost` 例外） |
| 一个 Discord 服务器 | 社区成员必须都在同一个服务器里；只需要服务器 ID |
| Bifrost 地址与密钥 | 形如 `https://bifrost.example.com` + `sk-bf-xxx` |

> 网关默认只监听 `127.0.0.1:3000`。公网入口必须由 Cloudflare Tunnel、Caddy 或 nginx 提供。

---

## 二、创建 Discord 应用

1. 打开 <https://discord.com/developers/applications> → **New Application**，起个名字（例如 `社区网关`）。
2. 左侧 **OAuth2**：
   - 记下 **Client ID** 与 **Client Secret**（点 Reset Secret 才看得到，只显示一次）。
   - **Redirects** 里加入（把域名换成你的）：

     ```
     https://ai.example.com/oauth/discord
     ```

     > 这一行必须和网关的「站点地址」完全一致，路径固定是 `/oauth/discord`。
     > 三个地方（Discord 里填的、网关配置的站点地址、浏览器实际访问的域名）差一个字符都会登录失败。

   - **Scopes** 勾选三项（网关自己会带上，这里只是让你知道用途）：

     | Scope | 用途 |
     |---|---|
     | `identify` | 拿用户 ID、用户名、头像 |
     | `guilds` | 列出用户所在的服务器 |
     | `guilds.members.read` | 读用户**自己**在该服务器的成员对象；不是成员会返回 404，这就是门禁的判定依据 |

     > 不需要 Bot，不需要 `bot` scope，也不需要把机器人拉进服务器。

3. 不需要在 **Bot** 页面做任何事。

---

## 三、拿到服务器 ID

本部署只判断「是不是这个服务器的成员」，**不需要身份组**。所以只需要一个值：服务器 ID（雪花 ID，一长串数字）。

### 从链接里读（最快）

Discord 里任意一条消息/频道的链接都长这样：

```
https://discord.com/channels/<服务器 ID>/<频道 ID>
                             └──── 服务器 ID ────┘ └──── 频道 ID ────┘
```

第一段数字就是服务器 ID。同一个服务器的所有频道、子帖，这一段都一样。

### 从客户端复制

1. Discord 设置 → **高级** → 打开 **开发者模式**
2. 右键服务器图标 → **复制服务器 ID**

### 校验填对了没有

向导只能检查格式（纯数字），没法验证这个 ID 真的是目标社区——验证服务器是否存在需要 bot token，而本方案刻意不用 bot。

**所以部署完成后，请自己用 Discord 登录一次**：

- 能进 → 服务器 ID 正确
- 提示「仅限本社区 Discord 成员使用」→ ID 填错了，或你的账号不在该服务器

排障时对比日志里的 `guild=` 和你填的值：

```bash
docker compose -f docker-compose.yml logs gateway | grep OAuth-Discord
# [OAuth-Discord] gate: user=... guild=<服务器 ID> roles=[...] required=[] cached=false
```

> 身份组是**可选的进阶能力**：如果以后要收紧成「只有某个身份组能进」，把身份组 ID 填进 `required_role_ids` 即可，不用重新构建镜像。身份组 ID 可以从上面那行日志的 `roles=[...]` 里抄。

---

## 四、部署

### 4.1 在构建机上产出二进制（需要 Go 与 bun）

**只有构建机需要这一步。** 收到的发布包里已经带好了 `bin/new-api`，接收方不需要装 Go 或 bun。

```bash
cd deploy
bash build.sh              # 生成 bin/new-api（linux/amd64）
bash build.sh --image      # 顺便构建 docker 镜像
```

镜像构建本身**不需要 Go**：`Dockerfile` 只 `COPY` 已编译好的 `bin/new-api` 和系统运行库。
`build.sh` 不会被打进发布包，它只留在构建机上。

### 4.2 交互式部署（推荐）

把 `deploy/` 整个目录（含 `bin/new-api`）拷到服务器，然后：

```bash
cd deploy
bash deploy.sh
```

向导会依次做这些事：

| 步骤 | 内容 |
|---|---|
| 1/6 环境检查 | docker / compose / 二进制是否就绪 |
| 2/6 收集配置 | 站点地址、Discord 凭证、服务器 ID、管理员账号、Bifrost 上游、出网代理 |
| 3/6 连通性校验 | 探测 Discord API 与 Bifrost `/models`，并把**要填进 Discord 的回调地址**原样打印出来 |
| 4/6 写入 .env | 生成随机的 `SESSION_SECRET`，文件权限 600 |
| 5/6 构建并启动 | `docker compose up -d --build`，等 `/api/status` 返回 200 |
| 6/6 初始化与配置 | 创建管理员 → 登录 → 写入站点地址/Discord/签到关闭等选项 → 创建 Bifrost 渠道 |

跑完后会打印站点地址、回调地址、管理后台地址和常用命令。

> 向导会把答案存进 `.deploy-config`（权限 600，含密钥），下次重跑直接回车沿用，不用重填。

### 4.3 导出部署包（发给别人，或在另一台机器部署）

```bash
bash deploy.sh --export /tmp/out
# → /tmp/out/community-gateway-YYYYMMDD.tar.gz（同时生成 .sha256 校验值）
```

默认导出的是**干净包**：只有二进制、Dockerfile、compose、`deploy.sh`、`README.md`、许可证和 `.env.example`，
**不含 `.env`、`.deploy-config` 或任何密钥**，也不含 `build.sh`（接收方不需要 Go 和 bun）。

对方拿到包后：

```bash
tar -xzf community-gateway-YYYYMMDD.tar.gz && cd community-gateway-YYYYMMDD
bash deploy.sh                        # 交互式向导，6 步填完即上线
```

如果只是把自己的机器迁移到另一台（配置和密钥都要带过去）：

```bash
bash deploy.sh --export /tmp/out --with-config
# 包内会包含 .env 与 .deploy-config，权限 600，只适合内部迁移，别外发
tar -xzf community-gateway-YYYYMMDD.tar.gz && cd community-gateway-YYYYMMDD
bash deploy.sh --non-interactive      # 完全复用包内配置，不再提问
```

> 包内的 `deploy.sh` 是同一份脚本，所以对方以后重跑也能用 `--export` 再打包给下一个人。

### 4.4 公网入口（二选一）

**Cloudflare Tunnel**（不需要开公网端口）：

```bash
cloudflared tunnel --url http://127.0.0.1:3000
# 或用命名隧道绑定你的域名
```

**Caddy**（自带证书）：

```caddyfile
ai.example.com {
    reverse_proxy 127.0.0.1:3000
}
```

> 无论用哪种，`https://ai.example.com/oauth/discord` 必须和 Discord 里登记的回调地址一致。

---

## 五、后台配置

管理后台：`https://ai.example.com/sign-in?admin=1`

普通成员在 `/sign-in` 只会看到 **Continue with Discord** 一个按钮；管理员应急入口是带 `?admin=1` 的地址。

### 5.1 套餐与额度池

后台 → **订阅**（或直接访问 `/my-plan`）→ **Plan administration**：

| 字段 | 含义 |
|---|---|
| MiniMax pool | MiniMax 系列模型每小时的**全站** token 上限。默认 `30000000`（30M） |
| GLM pool | GLM 系列模型每小时的**全站** token 上限。默认 `3000000`（3M） |
| Enabled | 该套餐是否可用 |
| Grant automatically on first sign-in | 是否作为默认套餐，在新用户首次登录时自动发放 |
| Clear window usage | 清零两个池的已用量并重置计时（排障 / 活动前重置用） |

要点：

- **全站共享，不是每人一份。** 30M 是所有人加起来一小时的总量。
- **口径是输入 + 输出合计**，跟上游 `usage.total_tokens` 一致，不按美元折算。
- 两个池**互相独立**：MiniMax 打满不影响 GLM，反之亦然。
- 窗口是**滚动固定窗口**：从该池第一次被计入开始计时，1 小时后归零，不是整点重置。
- **0 表示该池不限。** 两个都填 0 = 完全不限。
- **非 minimax / glm 的模型免费**（比如本地 qwen3-embedding / reranker），不占额度、也不会被额度拦。
- 新用户首次 Discord 登录自动获得默认套餐；已有账号（含管理员）在无有效订阅时登录也会补发。
- 签到默认关闭。

### 5.1.1 全站每分钟限流

除了小时额度，还有一道独立的闸：`GLOBAL_RATE_LIMIT_PER_MINUTE`（默认 30，0 = 不限）。

它写在 `.env` 里，不是后台可改项——因为它属于进程级保护，用来防止单个客户端写死循环把 Bifrost 打爆。改了要 `docker compose up -d` 才生效。

```
GLOBAL_RATE_LIMIT_PER_MINUTE=30
```

触发时返回 `429`，文案是 `You have reached the request limit: maximum 30 requests in 1 minutes`，与"额度已用完"区分得开。

### 5.1.2 使用规范（发给成员看）

本站是社区公益资源，不是公共 API 服务。两条硬规则：

1. **密钥必须按「软件 + 用途」命名**，例如 `cherry-studio-个人笔记问答`。看不出用途的 key 直接删。
2. **禁止任何形式的商业化使用**：出售/转售/出租 API 或额度、接入商业平台或付费项目、作为第三方盈利服务的一部分调用、账号共享 / 代用 / 一人多号。

管理员日常要做的两件事：

- 扫 **API Keys**：名字不合规的删掉。
- 看 **Usage Logs**：单账号用量异常高的，核对是不是在跑商业服务。

完整措辞见 [使用说明书 1.3](../docs/使用说明书.md#13-使用规范必读)。

> **渠道页的「测试」按钮尽量别点。** 每点一次都是真实上游请求，会消耗额度、占用全站每分钟次数，还会往日志里写一条噪声。刚改完配置时点一次确认即可。

### 5.2 渠道（Bifrost 上游）

向导第 6 步会自动建好，**一般不用手动动**。要改就：后台 → **渠道** → 编辑。

| 字段 | 填什么 |
|---|---|
| 类型 | OpenAI |
| Base URL | Bifrost **根地址**，例如 `https://<域名>` —— **不要带 `/v1`** |
| 密钥 | `sk-bf-xxx` |
| 模型 | 逗号分隔，不能有空格，见下表 |

> Base URL 填了 `/v1` 会变成 `/v1/v1/chat/completions`，表现为 404。

本站上游当前登记的模型（全部由同一个 Bifrost 出口提供，所以一个渠道就够）：

| 模型 ID（客户端 `model` 字段原样填这个） | 用途 | 出口 |
|---|---|---|
| `Z.ai/glm-5.3-flash` | 对话 | 智谱 GLM |
| `openrouter/minimax/minimax-m3` | 对话 | OpenRouter → MiniMax |
| `openrouter/minimax/minimax-m3.1-flash` | 对话 | OpenRouter → MiniMax |
| `Local_Bifrost/AI_MAX/qwen3-embedding` | `/v1/embeddings` | 本地 AMD AI MAX（llama.cpp） |
| `Local_Bifrost/AI_MAX/qwen3-reranker` | `/v1/rerank` | 本地 AMD AI MAX（llama.cpp） |

模型列表随时可改，**改完立刻生效，不用重启、不用重新打包**。新增模型时直接在上面那一栏末尾追加 `,新模型ID`。

> 这两个本地模型是从 Oracle 机器经 Bifrost 回源到你本地 AI MAX 的，网关本身只认识 Bifrost 一个出口；
> 本地机器掉线时，这两个模型的请求会失败，但不影响 GLM / MiniMax。

### 5.3 Discord 门禁参数

后台 → **系统设置 → 认证 → Discord**：

| 字段 | 说明 |
|---|---|
| Guild ID | 服务器 ID。**留空则跳过门禁**，任何 Discord 账号都能登录 |
| Guild ID | 服务器 ID。**留空则跳过门禁**，任何 Discord 账号都能登录 |
| Required role IDs | **默认留空** = 只要是该服务器成员就放行。填了才按身份组收紧 |
| Invite URL | **默认留空** = 拒绝时不给邀请链接。填了才在提示里附上 |

判定结果缓存 60 秒，改完配置最多一分钟生效。

---

## 六、日常运维

```bash
# 日志
docker compose -f docker-compose.yml logs -f gateway

# 只过滤门禁相关
docker compose -f docker-compose.yml logs gateway | grep OAuth-Discord

# 重启 / 停止
docker compose -f docker-compose.yml restart
docker compose -f docker-compose.yml down          # 数据保留在数据卷里

# 备份数据库（SQLite 在数据卷 community-gateway-data 里）
docker run --rm -v community-gateway-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/gateway-$(date +%F).tar.gz -C /data .

# 恢复
docker compose -f docker-compose.yml down
docker run --rm -v community-gateway-data:/data -v "$PWD":/backup alpine \
  sh -c 'rm -rf /data/* && tar xzf /backup/gateway-YYYY-MM-DD.tar.gz -C /data'
docker compose -f docker-compose.yml up -d

# 升级：替换 bin/new-api 后重建镜像
bash build.sh --image && docker compose -f docker-compose.yml up -d
```

---

## 七、排障手册

### 登录相关

| 现象 | 原因 | 处理 |
|---|---|---|
| Discord 页面报 `Invalid OAuth2 redirect_uri` | Discord 里登记的回调地址与网关的不一致 | 三处对齐：Discord Redirects、后台站点地址、浏览器地址栏域名。路径固定 `/oauth/discord` |
| 回调后提示「仅限本社区 Discord 成员使用」 | 账号不在该服务器，或 Guild ID 填错 | 先确认 Guild ID 与日志里的 `guild=` 一致；再确认该账号已加入服务器 |
| 提示「缺少访问所需的身份组」 | 配了 Required role IDs 但用户没有 | 默认不该出现（该字段默认留空）；要放开就清空 Required role IDs |
| 日志出现 `missing scope` / 403 | Discord 应用少了 `guilds.members.read` | 重新走一次授权（用户需重新点登录，让 Discord 重新发 token） |
| 改完门禁没生效 | 结果缓存 60 秒 | 等一分钟，或重启容器 |
| 普通成员看到用户名密码框 | 社区模式没生效 | 确认 Discord 已启用（后台 OAuth 里 Enabled 打开） |

### 额度相关

| 现象 | 原因 | 处理 |
|---|---|---|
| `MiniMax全站额度已用完（已用 x tokens / 上限 y tokens），将于 … 重置` | 该池这一小时打满 | 等重置，或后台调高该池额度（立即生效）；急用可换另一个池的模型 |
| `You have reached the request limit: maximum 30 requests in 1 minutes` | 全站每分钟限流触发 | 降低并发；确认 `GLOBAL_RATE_LIMIT_PER_MINUTE` 的值 |
| 两个额度框都显示「不限」 | 套餐里填了 0 | 后台把 MiniMax / GLM 池填回 30000000 / 3000000 |
| 三个窗口都显示「不限」 | 额度填了 0 | 0 表示不限，这是预期行为 |
| 额度改了但请求仍被拒 | 缓存/前端未刷新 | 窗口状态每次请求实时查库，改完立刻生效；页面 30 秒自动刷新 |
| 用户显示「无有效套餐」 | 没有默认套餐或订阅过期 | 后台把某个套餐勾上 Grant automatically，让用户重新登录 |

### 上游相关

| 现象 | 原因 | 处理 |
|---|---|---|
| 请求返回 502 / 连接超时 | 网关到 Bifrost 不可达 | 容器内 `docker compose exec gateway wget -qO- $UPSTREAM_BASE_URL/models`；需要代理就在 `.env` 里设 `HTTP_PROXY` |
| 401 / 403 | Bifrost 密钥错误或预算耗尽 | 用同一把密钥在宿主机 `curl -H "Authorization: Bearer sk-bf-..." $BASE/models` 复现 |
| 404 `model_not_found` | 模型名写错，或 Base URL 带了 `/v1` | 模型名用 `provider/model`；Base URL 只填根地址 |
| `Model xxx has not been priced by the administrator yet` | 自用模式被关掉了（向导默认开启） | 后台 → 运营设置 → 打开 **自用模式**；或给该模型配倍率 |

### 系统相关

| 现象 | 原因 | 处理 |
|---|---|---|
| `database is locked (SQLITE_BUSY)` | SQLite 只允许一个写者，并发写会撞锁 | 网关已默认开启 `busy_timeout=10s` + WAL + 连接池上限 8（启动日志会打印 `sqlite: journal_mode=wal busy_timeout=10000ms max_open_conns=8`）。偶发一两次会自动重试；**频繁出现说明并发已经超出 SQLite 的能力**，请设 `SQL_DSN` 换 MySQL 或 PostgreSQL |
| 容器起不来，日志提示权限 | 数据卷属主不对 | 用命名卷 `community-gateway-data`，不要改成 root 拥有的 bind mount |
| 从外网能直连 3000 端口 | 端口绑错 | `docker-compose.yml` 必须是 `127.0.0.1:3000:3000`，公网走隧道/反代 |
| 日志警告 `TRUSTED_PROXIES is unset` | 未显式声明可信代理 | 本机反代默认已覆盖回环与内网段，可忽略；要收紧就设 `TRUSTED_PROXIES=none` |

---

## 八、安全清单

上线前逐条确认：

- [ ] `.env`、`.deploy-config` 权限 600，且**没有**提交到版本库
- [ ] `SESSION_SECRET` 是随机值（向导已自动生成）
- [ ] 端口只绑 `127.0.0.1:3000`，公网只开 443
- [ ] Discord Client Secret 没有出现在任何前端代码或日志里
- [ ] 管理员密码不是默认值，且 `/sign-in?admin=1` 没有对外宣传
- [ ] 已开启 Cloudflare / 反代的 HTTPS，且回调地址用的是 https
- [ ] 数据库有定期备份（见[日常运维](#六日常运维)）
