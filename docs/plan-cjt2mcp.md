# 畅捷通 OpenAPI → MCP 转换服务 · 实施计划

> 状态：决策已锁定，待你批准执行 | 技术栈：FastAPI + Jinja2 + HTMX + SQLite | 部署：单 Docker 容器
> 交付范围（已对齐）：先出计划文件 → 本版真实对接畅捷通 OpenAPI → 每客户独立 MCP 端点
>
> **已锁定决策**：① 对外域名 `mcp.example.com`（占位，部署时替换）；② **多自建应用托管模型**——每个租户是一套独立的畅捷通自建应用，管理员添加租户时录入该租户的 appKey/appSecret/certificate/消息秘钥（加密入库，非全局 env）；`0aMlbJaE` + `fuxinqiche202607` 是第一个租户（振原水泥）的凭据示例；③ 产品线锁定 **T+ / TPLUS**；④ 首版工具 = 库存 + 基础档案 + 销售 + 采购 共 6 类只读查询。

---

## 一、调研结论（关键，纠正了原方案的架构假设）

原方案文档假设"生成授权链接 + OAuth callback"，但畅捷通**自建应用**实际是**消息驱动模型**。以下均来自开放平台官方文档，是本计划的事实基础：

### 1.1 部署模型：多自建应用托管，凭据按租户隔离

本服务是**多个自建应用的托管方**。每个租户（客户）对应畅捷通开放平台上一个**独立的自建应用**，拥有自己的一套凭据。管理员在后台"添加租户"时录入：

| 凭据 | 层级 | 来源 | 存储 |
|------|------|------|------|
| `appKey` / `appSecret` | **每租户** | 该客户自建应用创建时获得 | 租户表（appSecret 加密） |
| 消息秘钥（encodingAesKey） | **每租户** | 该客户自建应用"消息订阅"菜单设置 | 租户表（加密） |
| `certificate` | **每租户** | 企业管理员授权后，凭"企业临时授权码"换取 | 租户表（加密） |
| `appTicket` | **每租户**（滚动刷新） | 平台每 10 分钟推送到该租户 webhook | 租户表（单字段滚动更新） |

> 因此不存在"全局应用状态"。所有畅捷通凭据都是租户级数据，走后台录入 + 加密入库，**不进 env**。env 只保留平台级机密（管理员密码、AES-GCM 主密钥）。

### 1.2 鉴权模型：消息驱动，非标准 OAuth 重定向

换 token 接口（每租户用各自凭据调用）：
- `POST https://openapi.chanjet.com/v1/common/auth/selfBuiltApp/generateToken`
  - Header：`appKey` / `appSecret`（该租户的） / `Content-Type: application/json`
  - Body：`{ "appTicket": <该租户滚动 ticket>, "certificate": <该租户 certificate> }`
  - 返回：`accessToken`（`expiresIn≈518400s / 6天`）、`refreshToken`、`orgId`、`userId`、`scope`、`expiresIn`、`refreshExpiresIn`、`appName`
  - **返回字段落库映射**（全部写入该租户 `tenants` 行）：

    | 返回字段 | 落库列 | 说明 |
    |----------|--------|------|
    | `accessToken` | `access_token_enc` | AES-GCM 加密 |
    | `refreshToken` | `refresh_token_enc` | AES-GCM 加密 |
    | `expiresIn` | `token_expires_at` | 换算为绝对到期时间（`now + expiresIn`） |
    | `refreshExpiresIn` | `refresh_expires_at` | 换算为绝对到期时间 |
    | `orgId` | `org_id` | 授权企业 ID |
    | `userId` | `user_id` | 授权用户 ID |
    | `scope` | `scope` | 授权范围（默认 `auth_all`） |
    | `appName` | `app_name` | 应用名 |
    | （换 token 时刻） | `token_refreshed_at` | 记录本次刷新时间 |

### 1.3 两类端点：均为每客户独立

| 端点 | 数量 | 谁调用 | 如何区分租户 |
|------|------|--------|--------------|
| 消息接收 webhook `/webhook/chanjet/{client_code}` | **每客户独立** | 畅捷通平台主动推送 | **URL 中的 `client_code`**（见下方原因） |
| MCP 端点 `/chanjet/{client_code}/mcp` | **每客户独立** | WorkBuddy 等 AI 客户端 | URL 中的 `client_code` + Bearer Key |

> **webhook 为何必须带 `client_code`**：外层信封只有 `encryptMsg`，解密需要**该租户的消息秘钥**；不解密就读不到内部 appKey/身份——鸡生蛋。故租户身份必须由 URL 携带。每个租户的自建应用在各自"消息订阅"菜单里，把消息接收地址配成 `https://mcp.example.com/webhook/chanjet/{自己的code}`（如 `/webhook/chanjet/zysl`）。服务据 `client_code` 取该租户消息秘钥解密。

### 1.4 消息信封与加解密（以用户提供的权威规范为准）

- 信封：`{ "encryptMsg": "<Base64(AES密文)>" }`
- 加解密：`AES/ECB/PKCS5Padding`，秘钥 = **该租户消息秘钥** UTF-8 字节**直接**作为 AES key（不做 Base64 解码）
  - 示例租户消息秘钥 `fuxinqiche202607` = 16 字节 = AES-128（与规范吻合）
- 解密后明文结构：`{ id, appKey, appId, msgType, time, bizContent }`
- **响应要求：1 秒内返回 `{"result":"success"}`**，否则平台视为失败并重试

需处理的 `msgType`：

| msgType | 处理动作 | 首版 |
|---------|----------|------|
| `APP_TEST` | 首次配置地址验证，直接返回成功 | ✅ |
| appTicket 消息 | 更新**该租户** appTicket（滚动） | ✅ |
| 企业临时授权码 | 换取 certificate → 更新**该租户**授权态 | ✅ |
| 订单支付 / 产品线订阅 | 正常 ACK，不处理业务 | ✅（仅 ACK） |

### 1.5 业务接口调用规范（T+ 产品线）

> **阶段 5 调研纠正**：认证不是 `access_token + dataKey`，而是 **Header 三件套** `appKey` + `appSecret` + `openToken`（openToken 即 generateToken 返回的 accessToken）。原假设作废。

目标产品线：**T+**（凭据文件 portal 为 `/tplus/`，客户 振原水泥 用 T+ 正式账套）。

- **认证（全部放 Header）**：`appKey`、`appSecret`、`openToken`（= accessToken），`Content-Type: application/json`
- **请求体**：`{ "param": { ...查询条件对象... } }`（现存量接口 param 为对象，非序列化字符串；不同接口可能有差异，逐一坐实）
- **响应**：现存量接口直接返回**对象数组**（每元素含 WarehouseCode/InventoryCode/ExistingQuantity/AvailableQuantity 等）；部分接口用统一结构 `{result, value, error}`——客户端两种都要兼容
- **已坐实接口**：现存量查询 `POST https://openapi.chanjet.com/tplus/api/v2/currentStock/Query`
- **存疑（联调坐实）**：该接口文档未列账套（dataKey）参数。多账套企业如何指定账套待联调确认——可能隐含在 openToken(JWT 内含 orgId)，或需额外 Header。实现时把账套作为**可选扩展点**预留，默认走租户默认账套。

### 1.6 阶段 4 调研已坐实的两条结论（原文档缺口，现已定）

> 通过 `/md` + 模糊搜索接口查证畅捷通开放平台文档，纠正了先前两处假设：

1. **certificate 无"授权码换取接口"，是手动授权流程获得**（文档《自建应用软证书指引》
   `/md/docs/file/qa/qa/cjwt-sqgl/sqgl-zjyyrzszy`）：开发者在控制台"开发管理-权限管理"
   生成授权链接 → 企业应用管理员访问并授权 → **授权者把软证书同步给开发者** → 开发者手动录入。
   因此 certificate 由后台"添加租户"时录入（印证既定模型），**不存在自动换取接口**。
   （阶段 3 webhook 对 authCode 的兜底记录保留无害，但它不是 certificate 的来路。）
2. **refreshToken 无独立刷新接口**（多轮检索 0 命中）。accessToken 有效期约 6 天，
   过期时直接用该租户 **(滚动 appTicket + 已存 certificate)** 重新调 generateToken 换新 token
   即可——比依赖未文档化的刷新端点更稳。故 token_mgr 不实现 refresh 端点调用。

仍需实现阶段坐实（业务接口层，阶段 5 处理）：
3. T+ 业务接口的**确切 base URL** 与公共参数放置位置（Header vs Query，dataKey vs orgId）
4. 首版要开放的具体业务接口清单（见 §五 工具映射）

> 模糊搜索：`https://open.chanjet.com/md/manifest/search?q=<kw>&format=md`；任意 UI 文档路径前加 `/md` 得纯文本。

---

## 二、总体架构

```
                    畅捷通开放平台（每租户一个自建应用）
                         │
      ┌──────────────────┼───────────────────┐
      │ 推送消息(appTicket/授权码/APP_TEST)    │ 业务API调用
      ▼                                       ▲
┌─────────────────────────────────────────────────────────┐
│                  FastAPI 单容器服务                        │
│                                                           │
│  /webhook/chanjet/{code}  每客户消息端点(取租户秘钥解密+ACK)│
│  /chanjet/{code}/mcp      每客户 MCP 端点(Bearer 鉴权)     │
│  /admin/*                 后台管理(Jinja2+HTMX, 登录保护)  │
│                                                           │
│  核心模块：                                                │
│   crypto      AES/ECB 解密(用租户秘钥) + AES-GCM 凭据加密   │
│   chanjet     每租户 token 管理 + 业务接口客户端           │
│   mcp         MCP 协议(streamable-http) + 工具注册/权限     │
│   store       SQLite 访问层                                │
│   security    MCP Key 哈希校验 / 敏感字段 AES-GCM 加密      │
└─────────────────────────────────────────────────────────┘
                         │
                    SQLite (租户凭据/授权/账套/Key/权限/最小日志)
```

### 2.1 "每客户独立端点"落地方式

- MCP 地址 `https://mcp.example.com/chanjet/{client_code}/mcp`（如 `/chanjet/zysl/mcp`）
- Webhook 地址 `https://mcp.example.com/webhook/chanjet/{client_code}`（填入该租户自建应用消息订阅）
- MCP 侧：客户端用 `Authorization: Bearer <MCP Key>`；路由据 `client_code` + Key 哈希定位租户，注入该租户账套/权限/token 上下文
- 隔离：一个租户的 Key 只能访问自己端点；工具列表按该租户权限动态裁剪

---

## 三、数据库设计（SQLite）

凭据全部下沉到租户表并加密；无全局应用状态表。

```sql
-- 租户（客户）= 一套独立自建应用
CREATE TABLE tenants (
    id            TEXT PRIMARY KEY,          -- 内部 UUID
    client_code   TEXT NOT NULL UNIQUE,      -- ZYSL，用于端点路径，字母数字横线
    client_name   TEXT NOT NULL,
    contact       TEXT, phone TEXT, remark TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    -- 该租户自建应用凭据（敏感字段 AES-GCM 加密）
    app_key            TEXT,                  -- 明文可存(非机密)，用于调用 header
    app_secret_enc     TEXT,                  -- 加密
    msg_secret_enc     TEXT,                  -- 消息秘钥，加密（webhook 解密用）
    certificate_enc    TEXT,                  -- 加密证书，加密
    app_ticket         TEXT,                  -- 滚动 appTicket（30 分钟有效）
    ticket_updated_at  TEXT,
    -- 授权与企业（generateToken 返回落库）
    org_id        TEXT,                       -- 授权企业ID
    user_id       TEXT,                       -- 授权用户ID
    scope         TEXT,                       -- 授权范围(默认 auth_all)
    app_name      TEXT,                       -- 应用名
    auth_status   TEXT NOT NULL DEFAULT 'pending', -- pending/authorized/expired
    -- token 缓存(AES-GCM 加密)
    access_token_enc  TEXT, refresh_token_enc TEXT,
    token_expires_at  TEXT, refresh_expires_at TEXT,   -- 由 expiresIn/refreshExpiresIn 换算的绝对到期时间
    token_refreshed_at TEXT,
    -- 默认账套
    default_account_key TEXT,
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 账套（一个企业可有多账套）
CREATE TABLE account_sets (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    account_key TEXT NOT NULL,      -- dataKey
    name        TEXT NOT NULL,
    alias       TEXT,               -- "正式账套"
    is_default  INTEGER NOT NULL DEFAULT 0,
    enabled     INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id),
    UNIQUE(tenant_id, account_key)
);

-- MCP Key（一个租户可多 Key）
CREATE TABLE mcp_clients (
    id           TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    client_name  TEXT NOT NULL,      -- "WorkBuddy正式"
    key_prefix   TEXT NOT NULL,      -- mcp_zysl_7Kx9
    api_key_hash TEXT NOT NULL UNIQUE, -- SHA-256(完整Key)
    scopes_json  TEXT NOT NULL DEFAULT '[]',
    enabled      INTEGER NOT NULL DEFAULT 1,
    expires_at   TEXT, revoked_at TEXT,
    last_used_at TEXT,
    created_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
);

-- 管理员
CREATE TABLE admin_users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,     -- bcrypt/argon2
    display_name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 最小调用日志（不记录 Key/Token/业务数据/查询条件）
CREATE TABLE call_logs (
    id TEXT PRIMARY KEY,
    tenant_id TEXT, client_name TEXT,
    tool_name TEXT, status TEXT,       -- success/error
    error_code TEXT,                   -- CHANJET_TOKEN_EXPIRED 等
    duration_ms INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

---

## 四、模块与目录结构

```
cjt2mcp/
├── app/
│   ├── main.py              # FastAPI 入口、路由挂载
│   ├── config.py            # 环境变量读取(仅平台级机密：主密钥、管理员)
│   ├── db.py                # SQLite 连接 + 建表 + 迁移
│   ├── crypto.py            # AES/ECB 消息解密(租户秘钥) + AES-GCM 字段加密
│   ├── security.py          # MCP Key 哈希校验、管理员登录、Session
│   ├── chanjet/
│   │   ├── client.py        # generateToken/refresh/业务接口调用(按租户凭据)
│   │   ├── token_mgr.py     # 每租户 appTicket + certificate → token 缓存/刷新
│   │   └── webhook.py       # 按 client_code 取秘钥解密、msgType 分发、1s ACK
│   ├── mcp/
│   │   ├── server.py        # streamable-http MCP 端点 + Bearer 鉴权
│   │   └── tools.py         # 工具定义 + 权限裁剪 + 调畅捷通
│   └── admin/
│       ├── routes.py        # 后台 API(§六)
│       └── templates/       # Jinja2 + HTMX 页面
├── tests/
│   ├── test_crypto.py       # 用户提供的测试向量(必须通过)
│   └── test_webhook.py
├── .env.example
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

---

## 五、MCP 工具映射（首版：只读查询类）

首版仅开放查询，关闭增删改/审核/红冲。按业务模块分组、按租户权限裁剪：

| 分组 | 工具（MCP tool） | 畅捷通接口（阶段7坐实） | scope | 首版默认 |
|------|-----------------|-----------|-------|---------|
| 库存 | `query_current_stock` 查询现存量 | `POST /tplus/api/v2/currentStock/Query` | stock:read | ✅ |
| 基础档案 | `query_inventory` 查询存货 | `POST /tplus/api/v2/inventory/Query` | archive:read | ✅ |
| 基础档案 | `query_customer` 查询客户 | `POST /tplus/api/v2/partner/Query`（PartnerType=客户） | archive:read | ✅ |
| 基础档案 | `query_warehouse` 查询仓库 | `POST /tplus/api/v2/warehouse/Query` | archive:read | ✅ |
| 销售 | `query_sales_order` 查询销售订单 | `POST /tplus/api/v2/saleOrder/Query` | sales:read | ✅ |
| 采购 | `query_purchase_order` 查询采购订单 | `POST /tplus/api/v2/purchaseOrder/Query` | purchase:read | ✅ |

> 全部 6 工具已实装（见记忆 chanjet-tplus-endpoints）。⚠️ **响应结构关键点**：T+ 查询接口的 `result` 字段本身是数据载荷（数组或含 `Data[]` 的对象），仅失败时 `result=false` 带 error——与 generateToken 的布尔信封相反，client 层用 `_business_query` + `_norm_list` 分别处理。工具输入 schema 暴露分页（page_index/page_size）与关键字筛选（code/name 等）；账套多选待联调坐实（§1.5 存疑点）。

---

## 六、后台 API 清单（沿用原方案，按新数据模型对齐）

- 客户：`GET/POST /api/admin/tenants`（**新增租户时录入 appKey/appSecret/msgSecret/certificate**，敏感字段服务端加密）、`GET/PATCH /api/admin/tenants/{id}`、`enable/disable`
- 租户凭据：`PATCH /api/admin/tenants/{id}/credentials`（更新 appSecret/msgSecret/certificate，页面只显状态不回显明文）
- MCP Key：`GET .../mcp-keys`、`POST .../mcp-keys/generate`（返回 `plain_key` 仅一次）、`.../custom`、`revoke/enable/DELETE`
- MCP 配置生成：`GET .../mcp-config?client_type=workbuddy&key_id=...`（日常页面 Key 位置显示占位符，只有新建 Key 时返回明文）
- 授权：`POST .../chanjet/test`、`.../chanjet/refresh-accounts`、`.../chanjet/revoke`（授权由该租户自建应用推送企业授权码驱动，无重定向 callback）
- 账套：`GET/PATCH .../account-sets`、`.../default`
- 工具权限：`GET/PUT .../tools`
- Webhook 地址提示：客户详情页展示该租户应填入自建应用的消息接收地址 `.../webhook/chanjet/{code}`

---

## 七、安全要求（落实到实现）

- 平台级机密（AES-GCM 主密钥、管理员初始密码）走**环境变量**；`.env` 已在 `.gitignore`（已完成）
- **租户级畅捷通凭据**（appSecret / 消息秘钥 / certificate / token / refreshToken）：**AES-GCM 加密**入库，页面永不回显明文，只显状态
- MCP Key：数据库只存 SHA-256 哈希 + 前缀；完整 Key 仅创建时返回一次；日志不记录 Key
- 管理后台：管理员登录 + Session 有效期 + 连续失败限制；MCP 端点强制 Bearer 校验
- MCP 端点默认拒绝未授权访问（无 Key / Key 吊销 / 租户停用 → 401/403）
- Webhook 端点：`client_code` 不存在或租户停用 → 拒绝；解密失败/非法消息拒绝处理，不泄露内部错误细节

---

## 八、实施阶段（建议顺序，每阶段可验证）

1. **骨架 + crypto**：项目结构、config、db 建表；实现 `crypto.py`（AES/ECB 解密 + AES-GCM 字段加密），用**用户提供的测试向量**跑通解密单测（key `1234567890123456` → 指定明文）。← 解密是全案地基
2. **租户管理 + 凭据加密**：添加租户（录入 appKey/appSecret/msgSecret/certificate → 加密入库）；客户列表/详情
3. **Webhook**：`/webhook/chanjet/{code}` 按租户秘钥解密 + msgType 分发 + 1s ACK；APP_TEST 验证；appTicket 落库
4. **Token 管理**：企业授权码 → certificate（坐实接口）→ generateToken（租户凭据）→ 加密缓存 + 刷新逻辑
5. **业务客户端 + MCP 端点**：T+ 业务接口封装 → MCP streamable-http 端点 + Bearer 鉴权 + 现存量工具打通
6. **后台补齐**：MCP Key 生成与一键复制配置 → 账套/权限 → 测试连接页 → webhook 地址提示
7. **工具扩展**：坐实并补齐 §五 其余查询工具
8. **打包**：Dockerfile + compose + `.env.example` + README

---

## 九、验证策略

- `crypto`：以用户测试向量为断言锚点，必过
- `webhook`：构造某租户加密消息 → 断言用该租户秘钥解密结果与 ACK 格式
- 集成：`chanjet/test` 连接测试串联"身份→授权→token→账套→轻量接口"，失败时返回明确错误码（`CHANJET_TOKEN_EXPIRED` / `CHANJET_PERMISSION_DENIED` / `MCP_TOOL_DISABLED` / `ACCOUNT_SET_NOT_ALLOWED` 等）
- MCP：用 MCP inspector / curl 验证 `initialize` 与 `tools/list`（权限裁剪）与 `tools/call`

---

## 十、已锁定决策（原开放点，现已定）

1. **域名**：`mcp.example.com`（占位，部署时替换为真实域名/cpolar 临时域名）
2. **模型**：多自建应用托管——每租户独立 appKey/appSecret/消息秘钥/certificate，管理员录入加密入库；`0aMlbJaE` + `fuxinqiche202607` 为第一个租户（振原水泥）示例凭据
3. **产品线**：锁定 **T+ / TPLUS**
4. **首版工具**：库存 + 基础档案 + 销售 + 采购 共 6 类只读查询
