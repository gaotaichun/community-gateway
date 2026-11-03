# 类脑社区 AI API 网关

面向 Discord 社区的 AI API 网关：**用 Discord 身份做门禁，用三窗口额度做计量**，请求最终转发到自建的 Bifrost 上游。

它不训练模型、不做路由、不管上游密钥——只回答两个问题：**谁有资格用**、**每人能用多少**。

```
社区成员 → 本网关（门禁 + 额度） → Bifrost（OpenAI 兼容） → 各 provider
```

## 特性

| 能力 | 说明 |
|---|---|
| **Discord 门禁** | OAuth2 授权码流程，校验是否为指定服务器成员；身份组可选收紧，留空即"是成员就放行" |
| **三窗口额度** | 5 小时 / 7 天 / 30 天三个窗口同时生效，任一耗尽即拒绝；从首次消费开始滚动计时 |
| **额度可配** | 三个数字随时改，即时生效，填 0 表示该窗口不限 |
| **令牌管理** | 每个成员自助创建多把 key，可限制额度、过期时间、模型白名单、来源 IP |
| **用量审计** | 每次请求的模型、token 数、消耗额度、耗时全部落库 |
| **单二进制** | Go 编译产物内嵌前端，运行时只需一个容器、一个 SQLite 文件 |
| **双架构** | 发布包同时包含 `linux/amd64` 与 `linux/arm64`，安装时自动选择 |

## 快速开始

**部署方**（需要 Docker）：

```bash
tar -xzf community-gateway-YYYYMMDD.tar.gz
cd community-gateway-YYYYMMDD
bash deploy.sh          # 6 步向导
```

向导会依次收集 Discord 应用凭证、服务器 ID、站点地址、管理员账号、上游地址与密钥，然后自动建库、起容器、写配置、建渠道。

**使用者**：

1. 打开站点，点「使用 Discord 继续」
2. 概览页看自己的三窗口额度
3. `API Keys` 页创建密钥（**只显示一次**）
4. 用 `https://<域名>/v1` 作为 Base URL 调用

## 文档

| 文档 | 内容 |
|---|---|
| [使用说明书](docs/使用说明书.md) | 完整手册：成员怎么用、管理员怎么配、运维怎么修 |
| [部署指南](deploy/README.md) | 环境要求、Discord 应用创建、向导每一步、公网入口、排障 |

## 仓库结构

```
.
├── docs/
│   └── 使用说明书.md          # 完整使用手册（成员 / 管理员 / 运维）
├── deploy/                    # 交付物：打成一个 tar.gz 发给部署方
│   ├── Dockerfile             # 运行时镜像，只 COPY 预编译二进制，不含 Go
│   ├── docker-compose.yml     # 只绑 127.0.0.1:3000，公网必须走 HTTPS 入口
│   ├── deploy.sh              # 6 步交互式向导
│   ├── build.sh               # 构建机用：前端 + 双架构二进制
│   ├── preset.env             # 非密钥预设值（地址占位符 + 模型列表）
│   └── README.md              # 部署指南
└── tools/
    ├── e2e/                   # 端到端验证脚本（登录、门禁、额度、上游）
    ├── mockdiscord/           # 本地 mock Discord API，离线测门禁
    ├── mockopenai/            # 本地 mock 上游，离线测转发
    └── shot/                  # 截图脚本（playwright-core + 本机 Edge）
```

## 设计取舍

**为什么用 Docker 而不是直接跑二进制**
运行时镜像基于 `debian:bookworm-slim`，只 `COPY` 一个预编译二进制，不含 Go 和 Node。部署方不需要装任何编译环境。

**为什么默认只绑 `127.0.0.1`**
网关本身不做 TLS。它必须藏在 Cloudflare Tunnel / Caddy / nginx 后面，由那一层终止 TLS。直接暴露 3000 端口意味着会话 Cookie 明文传输。

**为什么额度从"首次消费"开始计时**
固定整点窗口会让"晚上 8 点才开始用的人"只享受 2 小时就重置，体验割裂。滚动窗口对每个用户公平，代价是重置时间因人而异。

**为什么 SQLite 要开 WAL + 连接池上限**
SQLite 是单写者模型。默认连接池会开到上千，并发写必然撞 `database is locked`。本项目改为 `journal_mode=WAL` + `busy_timeout=10s` + 连接池上限 8，中小社区足够；并发再高应换 MySQL / PostgreSQL。

## 安全须知

- `.env`、`.deploy-config`、`preset.local.env` 含密钥，**已被 `.gitignore` 忽略**
- 发布包默认不含任何密钥，只带占位符；`--with-config` 才会把本机配置打进去（仅限内部迁移）
- 上游密钥只存在于网关数据库，不下发给普通成员
- 备份文件包含所有 key 与用量，需加密存放

## 许可

本仓库的部署脚本与文档基于 [new-api](https://github.com/QuantumNous/new-api)（AGPL-3.0）构建，第三方许可全文见 `deploy/licenses/`。
