# cjt2mcp — 畅捷通 OpenAPI → MCP 转换服务

把畅捷通 T+ 开放平台接口转换成 [MCP](https://modelcontextprotocol.io)（streamable-http），
让 WorkBuddy 等 AI 客户端能直接查询 T+ 的库存、档案、单据数据。

**技术栈**：FastAPI + Jinja2 + HTMX + SQLite，单 Docker 容器部署，适合轻量多客户单机场景。

---

## 架构模型：多自建应用托管

每个租户（客户）对应畅捷通开放平台上一个**独立的自建应用**，拥有自己的一套凭据
（appKey / appSecret / 消息秘钥 / certificate）。平台管理员在后台添加租户时录入这些凭据
（敏感字段 AES-GCM 加密入库），再在租户下配置 MCP Key 交付给 AI 客户端。

两类端点均为**每租户独立**：

| 端点 | 谁调用 | 如何区分租户 |
|------|--------|--------------|
| 消息接收 `POST /webhook/chanjet/{client_code}` | 畅捷通平台推送 | URL 中的 `client_code` → 取该租户消息秘钥解密 |
| MCP 端点 `POST /chanjet/{client_code}/mcp` | WorkBuddy 等 AI 客户端 | URL `client_code` + `Authorization: Bearer <MCP Key>` |

> 消息 webhook 必须带 `client_code`：外层信封只有密文 `encryptMsg`，解密需要该租户的消息秘钥，
> 而租户身份恰在密文里——鸡生蛋，故身份由 URL 携带。每个租户在自己自建应用的「消息订阅」
> 菜单里把消息接收地址配成 `.../webhook/chanjet/{自己的 code}`。

## 鉴权模型（消息驱动，非 OAuth 重定向）

- 平台每 10 分钟向 webhook 推送 `appTicket`（30 分钟有效，滚动更新）。
- `certificate` 由管理员在畅捷通控制台手动授权后获得，录入后台。
- 换 token：`appKey + appSecret + appTicket + certificate` → `generateToken`，
  返回 accessToken（约 6 天有效）。无独立刷新接口，过期直接重新换取。

## 首版工具（只读查询，按 MCP Key 的 scope 裁剪）

| scope | 工具 | 畅捷通 T+ 接口 |
|-------|------|----------------|
| `stock:read` | `query_current_stock` 现存量 | `/tplus/api/v2/currentStock/Query` |
| `archive:read` | `query_inventory` 存货 | `/tplus/api/v2/inventory/Query` |
| `archive:read` | `query_customer` 客户 | `/tplus/api/v2/partner/Query`（PartnerType=客户） |
| `archive:read` | `query_warehouse` 仓库 | `/tplus/api/v2/warehouse/Query` |
| `sales:read` | `query_sales_order` 销售订单 | `/tplus/api/v2/saleOrder/Query` |
| `purchase:read` | `query_purchase_order` 采购订单 | `/tplus/api/v2/purchaseOrder/Query` |

---

## 快速开始

### 1. 准备环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入平台级机密（租户畅捷通凭据不在此，走后台录入）：

```bash
# AES-GCM 主密钥（加密租户凭据），生成：
python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"

# Session 签名密钥，生成：
python -c "import secrets; print(secrets.token_hex(32))"
```

必填：`MASTER_KEY`、`ADMIN_PASSWORD`、`SESSION_SECRET`；
`PUBLIC_BASE_URL` 填对外访问域名（用于生成 webhook / MCP 地址）。

### 2. Docker 部署（推荐）

```bash
docker compose up -d --build
```

访问后台 `http://<host>:8000/admin`，用 `.env` 里的管理员账号登录。

### 3. 本地开发

```bash
pip install -r requirements.txt
export $(grep -v '^#' .env | xargs)   # 或用 direnv
uvicorn app.main:app --reload
pytest                                # 46 项测试
```

---

## 交付一个客户的流程

1. 后台「添加客户」，录入该租户自建应用的 appKey/appSecret/消息秘钥/certificate。
2. 详情页复制**消息接收地址**，填入该租户自建应用的「消息订阅」菜单。
3. 等待平台推送 appTicket（10 分钟内）。
4. 生成 MCP Key，勾选所需权限（库存/档案/销售/采购）。
5. 「测试连接」验证链路（身份→凭据→appTicket→token→业务接口）。
6. 一键复制 WorkBuddy MCP 配置，交付客户。

---

## 可信域名（畅捷通拨测）

畅捷通平台会不定期拨测 `http://<可信域名>/CHANJET_CHECK.txt` 校验域名所有权。本服务按请求 **Host 头**区分租户，返回各自的验证文件内容，因此多个租户可各用独立子域名、互不干扰。

每个租户的配置步骤：

1. **后台配置**：详情页「可信域名」卡片，填域名（如 `sdfx.hacka.cn`，可带端口），上传从畅捷通控制台下载的 `CHANJET_CHECK.txt`（或直接粘贴内容）。上传新文件即覆盖，只改域名不会清空已存内容。
2. **Nginx 反代**：把该域名反代到本服务，**必须透传 Host 头**（服务靠它分流）：
   ```nginx
   server {
       server_name sdfx.hacka.cn;
       location / {
           proxy_pass http://127.0.0.1:8002;
           proxy_set_header Host $host;   # 必须：按域名定位租户
       }
   }
   ```
3. **畅捷通控制台**：在「可信域名」里填入 `sdfx.hacka.cn`，平台拨测 `http://sdfx.hacka.cn/CHANJET_CHECK.txt` 命中该租户内容即验证通过。

要点：
- 文件名固定为 `CHANJET_CHECK.txt`（畅捷通硬性要求，不可改名）。
- 每个一级域名只能被一个 ISV 认领，务必**一租户一子域名**。
- 每个 ISV 最多 5 个可信域名，不够需联系畅捷通客服扩容。

---

## 安全设计

- **租户畅捷通凭据**（appSecret / 消息秘钥 / certificate / token）：AES-GCM 加密入库，页面永不回显。
- **MCP Key**：只存 SHA-256 哈希 + 前缀，完整 Key 仅创建时返回一次；日志不记录 Key。
- **平台级机密**（主密钥 / 管理员密码 / Session 密钥）：走环境变量，不入库不入库。
- **调用日志**：仅记录租户/工具/状态/错误码/耗时，不记录 Key、Token、查询条件、业务数据。
- MCP 端点强制 Bearer 校验，且 Key 必须匹配 URL 租户，防跨租户访问。

> ⚠️ 公开部署时务必置于 HTTPS 反向代理之后，并设置强管理员密码。

---

## 镜像

GitHub Actions 在 push 到 `main` 或打 tag 时自动构建并发布镜像到 GitHub Container Registry：

```bash
docker pull ghcr.io/hkxiaoyao/cjt2mcp:latest
```

## 目录结构

```
app/
├── main.py            # FastAPI 入口，挂载三组路由
├── config.py          # 平台级机密（env）
├── db.py              # SQLite 建表 + 迁移
├── crypto.py          # AES/ECB 消息解密 + AES-GCM 字段加密
├── security.py        # MCP Key 哈希、管理员登录、Session
├── store.py           # 数据访问层
├── chanjet/           # 畅捷通对接：client / token_mgr / webhook / conntest
├── mcpsrv/            # MCP 协议：server（JSON-RPC）/ tools（工具+权限）
└── admin/             # 后台管理（Jinja2 + HTMX）
```
