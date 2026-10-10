# 图书管理系统（Python + Flask + SQLite）

一个开箱即用的图书管理系统，采用 **统一登录入口 + 双端工作台** 的结构：

- **统一登录页（首页 `/`）**：打开网站即是登录页，同一个登录框自动识别身份——读者输借书证号 / 手机号，馆员输工作账号；页面上同时保留图书馆介绍、借阅规则、馆藏推荐 / 新书上架与**免登录的馆藏检索**。
- **读者工作台**（读者登录）：凭读者证号或手机号登录「我的借阅」，查看在借 / 历史 / 逾期与滞纳金，自助续借、维护联系方式与密码。
- **后台控制台**（管理员登录）：图书 / 读者 / 借阅三大业务域 + 数据概览，全部管理功能在此完成。

无论读者还是馆员，都必须登录才能进入各自的工作台；未登录只能浏览公开的馆藏信息。

纯 Python 标准库 + Flask 单依赖，数据库用单文件 SQLite，无需安装任何数据库服务。

## 快速开始

```bash
# 1. 安装依赖（仅 Flask）
pip install -r requirements.txt

# 2. 启动（首次启动自动建表并创建默认管理员；加 --seed 写入演示数据）
python app.py run --seed

# 3. 打开浏览器
#    统一登录页 http://127.0.0.1:8000/   （首页即登录页，登录框自动识别读者 / 馆员）
#    读者工作台 http://127.0.0.1:8000/reader
#    后台控制台 http://127.0.0.1:8000/admin
```

**默认管理员账号**：`admin` / `admin123`（首次启动时从配置创建，**登录后请立即修改密码**；之后以数据库为准，环境变量不再覆盖）。

**读者账号**：随读者档案自动开通，**初始密码 = 借书证号后 6 位**（如证号 `2024001` → 密码 `024001`）；管理员可在后台一键重置，读者登录后可自行修改。历史数据升级时自动为存量读者回填初始密码。

常用命令：

| 命令 | 说明 |
| --- | --- |
| `python app.py run` | 启动 Web 服务 |
| `python app.py run --seed` | 启动并写入演示数据（库非空则跳过） |
| `python app.py run --reset` | 清空重建演示数据后启动 |
| `python app.py init` | 仅初始化数据库 |
| `python app.py seed --force` | 强制重写演示数据 |
| `python app.py reset` | 重置数据库并重建演示数据 |
| `python tests/smoke_test.py` | 运行 220 项端到端冒烟测试 |

端口、馆名、借阅规则、会话与限流等全部可用环境变量覆盖，见 `.env.example`。

## 页面与访问规则

| 路径 | 是否需要登录 | 说明 |
| --- | --- | --- |
| `/` | 否 | **统一登录页**：登录框（自动识别读者 / 馆员）+ 简介、推荐、免登录馆藏检索；已登录访问直接跳对应工作台 |
| `/reader` | **读者** | 读者个人中心「我的借阅」，未登录跳回首页登录 |
| `/admin` | **管理员** | 后台管理控制台，未登录跳回首页登录 |
| `/login` `/reader/login` | 否 | 旧登录链接，兼容性 302 到首页 `/` |
| `/api/public/*` | 否 | 门户只读接口 |
| `/api/auth/login` `/logout` `/me` | 否 | **统一登录**、登出、当前管理员会话 |
| `/api/reader/login` `/logout` | 否 | 读者专用登录（兼容保留）、登出 |
| 其余 `/api/reader/*` | **读者** | 个人中心接口，未登录返回 401 |
| 其余 `/api/*` | **管理员** | 图书 / 读者 / 借阅 / 概览，未登录返回 401 |

登录入口全站只有一个（首页）：一份账号密码按"先管理员用户名、后读者证号 / 手机号"的顺序自动识别身份，**一次登录只保留一种身份**（登录会刷新会话），读者与管理员使用各自的 session 键与 CSRF token，互不可访问对方的接口。

## 功能

### 读者账号与个人中心（读者登录）

凭**借书证号或手机号**登录，初始密码为证号后 6 位。登录后可：

- **我的借阅**：在借 / 未归还 / 逾期 / 已归还筛选 + 分页，显示应还日期、续借次数、逾期天数与滞纳金；
- **借阅概览**：在借册数、逾期册数、累计借阅、剩余可借额度、累计滞纳金一目了然；
- **自助续借**：直接续借自己的在借图书（沿用管理端规则：未逾期、未达续借上限、账号未冻结）；
- **维护联系方式**：自行修改手机号与邮箱（手机号可登录，改动会做占用校验；姓名、证号、可借上限只能由馆员维护）；
- **修改密码**：改后强制重新登录。

账号被冻结的读者仍可登录查看历史借阅，但无法自助续借。越权访问他人借阅记录一律返回 403。

### 读者门户（前台，免登录）

图书馆介绍与借阅规则展示、按分类浏览馆藏、"本馆推荐"与"新书上架"两个书单、关键词 / 分类 / 仅看可借 三条件检索 + 分页，实时显示每本书的可借状态（可借 N 册 / 仅剩 1 册 / 全部借出）。检索支持按**馆藏条码**直查；书卡与检索结果展示**内容简介摘要**，点击可打开详情弹窗查看简介全文与**可扫描的条码图**。

### 后台控制台（需登录）

**概览**：馆藏 / 可借 / 在借 / 逾期 / 到期 / 读者六项核心指标，近 14 天借阅趋势图，分类借阅排行，热门图书 TOP6，逾期提醒列表。

**图书管理**：新增 / 编辑 / 删除、ISBN 自动规整与唯一校验、**馆藏条码**（留空自动生成 `BK` 编号，可填馆内已有条码，唯一校验）与**图书简介**（≤1000 字，用于门户展示）、分类与关键词筛选（命中书名 / 作者 / ISBN / 条码 / 出版社）、仅看可借、分页；图书详情弹窗展示条码图与简介全文，支持**打印条码标签**；有未归还或历史借阅的图书受删除保护。

**读者管理**：新增 / 编辑 / 删除、证号唯一校验、类型（学生 / 教师 / 社会 / VIP）、冻结 / 恢复、可借上限单独设置、借阅记录弹窗（含累计罚金）；**门户账号列**显示开通状态，新建读者时自动开通并提示初始密码，支持一键重置密码。

**借阅管理**：借书（选人 + 选书，只列可借库存与正常状态读者）、还书（自动结算罚金）、续借（限次）、按状态筛选（全部 / 在借 / 逾期 / 已归还）、逾期天数与罚金自动计算。

### 业务规则

- 借期默认 30 天，最多续借 2 次，逾期罚金默认 0.50 元 / 天（均可配置）。
- 有逾期未还、被冻结或已达可借上限的读者不可借书；同一读者不可重复借同一本书。
- 库存扣减使用条件 UPDATE（`WHERE available_copies > 0`）+ 事务，并发下不会超借。
- 删除保护：图书 / 读者存在借阅记录时禁止删除，避免破坏流通档案。

### 馆藏条码与图书简介

- 每本书都有独立的**馆藏条码号**（默认 `BK` + 7 位序号，如 `BK0000006`），与 ISBN 解耦；可手工改为馆内已有条码（4–32 位字母 / 数字 / 连字符，全局唯一）。
- 条码图由 `static/js/barcode.js` 在前端生成 **Code 128** SVG（零依赖，码表已与 `python-barcode` 独立实现逐位比对一致），可直接扫码。
- 每本书都有**简介**字段（≤1000 字），后台表单可编辑；门户书卡 / 检索详情、后台图书列表与详情弹窗均会展示。
- 老库自动升级：`init_db` 幂等迁移加列、为存量图书回填条码并建唯一索引；演示书目按 ISBN 自动回填简介（只补空值，不覆盖已录入内容）。
- 借阅记录（后台借阅列表、读者"我的借阅"、借书 / 还书提示）均带条码号，便于按条码上架归位与扫码核对。

### 安全与鉴权

- **双身份体系**：统一入口自动识别读者 / 馆员；管理员与读者使用各自的会话键与 CSRF token，一次登录只保留一种身份；读者端接口强制只读写本人数据，跨读者访问返回 403。
- **密码存储**：PBKDF2-SHA256 加盐哈希，绝不落明文；读者初始密码 = 证号后 6 位，登录后可改，管理员可重置。
- **会话**：基于 Flask 签名 Session；Cookie 标记 `HttpOnly` / `SameSite=Lax`（启用 HTTPS 后可开 `Secure`），有效期默认 8 小时，登录成功后轮换会话（防会话固定）。
- **CSRF**：写操作（POST / PUT / DELETE）需带 `X-CSRF-Token`，管理员与读者各用独立 token，使用 `hmac.compare_digest` 常量时间比对。
- **登录限流**：管理员与读者**共用同一套计数**（防止交替调用入口绕过锁定），按 `账号 + IP` 连续失败 5 次锁定 5 分钟（可配置）。
- **访问隔离**：后台页面与全部管理接口统一守卫，未登录一律拒绝；接口返回 401 JSON，页面 302 跳统一登录页，登录后按身份落回原目标。
- **安全头**：CSP、X-Frame-Options、nosniff、Referrer-Policy；`/admin`、`/reader` 与 `/api/*` 响应禁止缓存。

## 接口一览

**门户（免登录）**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/public/overview` | 门户聚合：馆情统计、规则、分类、推荐、新书 |
| GET | `/api/public/search` | 馆藏检索（`keyword` `category` `available_only` `page` `page_size`） |

**鉴权**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/login` | **统一登录** `{identifier, password}`（兼容 `username` / `account`）→ 自动识别身份 `{role: "admin"|"reader", redirect, admin|member, csrf_token}` |
| POST | `/api/auth/logout` | 登出 |
| GET | `/api/auth/me` | 当前会话 + CSRF token + 馆名 |
| POST | `/api/auth/password` | 修改密码 `{old_password, new_password}`（成功后强制重新登录） |

**读者（需读者登录 + 写操作需读者 CSRF）**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/reader/login` | 读者专用登录 `{account, password}`（account = 证号或手机号，兼容保留） |
| POST | `/api/reader/logout` | 读者登出 |
| GET | `/api/reader/me` | 个人资料 + 借阅统计 + 借阅规则 + CSRF token |
| GET | `/api/reader/loans` | 我的借阅（`status` `page` `page_size`，强制只看本人） |
| POST | `/api/reader/loans/:id/renew` | 自助续借（校验记录归属） |
| PUT | `/api/reader/profile` | 修改联系方式 `{phone, email}` |
| POST | `/api/reader/password` | 修改密码 `{old_password, new_password}`（成功后强制重登） |

**管理（需管理员登录 + 写操作需 CSRF）**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` `/ready` | 存活 / 就绪探针（免登录） |
| GET | `/api/books` | 图书分页列表（`keyword` `category` `available_only` `page` `page_size`） |
| POST / PUT / DELETE | `/api/books[/:id]` | 图书增删改 |
| GET | `/api/books/categories` | 分类聚合 |
| GET | `/api/members` | 读者分页列表（含在借 / 逾期统计） |
| POST / PUT / DELETE | `/api/members[/:id]` | 读者增删改 |
| GET | `/api/loans` | 借阅分页列表（`status` `keyword` `member_id` `book_id`） |
| POST | `/api/loans/borrow` | 借书 `{book_id, member_id, note?}` |
| POST | `/api/loans/:id/return` | 还书 |
| POST | `/api/loans/:id/renew` | 续借 |
| GET | `/api/dashboard/overview` | 概览聚合 |

统一错误结构：`{"error": {"code", "message", "details"}}`，校验失败会带出 `field` 便于前端定位；每个响应带 `X-Request-ID`。

## 项目结构

```
├── app.py                    # 入口与 CLI（run / init / seed / reset）
├── config.py                 # 集中配置，启动时校验（fail fast）
├── library/
│   ├── web.py                # 应用工厂：蓝图、鉴权守卫、错误处理、安全头、CORS
│   ├── db.py                 # 连接管理、Schema、事务、查询辅助
│   ├── errors.py             # 类型化错误层级
│   ├── validation.py         # 输入校验
│   ├── logging_conf.py       # 结构化 JSON 日志 + request id
│   ├── timeutil.py           # 时间处理
│   ├── seed.py               # 演示数据
│   ├── auth/                 # 统一登录：身份识别、密码哈希、登录限流、会话、CSRF
│   ├── reader/               # 读者账号：登录会话、个人借阅中心、自助服务
│   ├── public/               # 统一登录页 + 门户：介绍统计 + 馆藏检索
│   ├── books/                # 图书：routes → service → repository
│   ├── members/              # 读者：routes → service → repository
│   ├── loans/                # 借阅：routes → service → repository
│   └── dashboard/            # 概览统计
├── templates/
│   ├── public.html           # 统一登录页（首页：登录框 + 简介 + 检索 + 推荐）
│   ├── reader.html           # 读者个人中心「我的借阅」
│   └── index.html            # 后台控制台单页
├── static/
│   ├── css/                  # site / public / login（登录表单控件）/ reader / app 样式
│   └── js/                   # public.js（含统一登录）/ reader.js / app.js / barcode.js，原生 JS 无依赖
└── tests/smoke_test.py       # 220 项端到端测试
```

分层约定：Controller 只搬运参数，业务规则全部在 Service，SQL 全部在 Repository 且过滤条件下推到数据库、参数化绑定，杜绝拼接注入。

## 技术要点

- **并发安全**：借书走 `UPDATE ... WHERE available_copies > 0` 的原子扣减 + 显式事务，WAL 模式减少读写锁冲突。
- **类型化错误**：`ValidationError(400) / UnauthorizedError(401) / NotFoundError(404) / ConflictError(409) / BusinessRuleError(422)`，全局处理器统一输出。
- **可观测性**：单行 JSON 日志（含 request id、路径、状态码、耗时），登录成功 / 失败 / 锁定均有审计日志，`/health` `/ready` 探针。
- **安全**：PBKDF2 密码哈希、签名会话、CSRF 双提交校验、登录限流、SQL 参数绑定、前端全量 HTML 转义（防 XSS）、CSP / X-Frame-Options / nosniff 安全头、显式白名单 CORS（默认关闭）。
- **前端**：无框架原生实现，防抖搜索、分页、弹窗表单、字段级错误提示、SVG 手绘趋势图；门户与管理台样式各自独立，共享 `site.css` 设计令牌。

## 已知限制

- 单机单文件 SQLite，适合中小规模（校园 / 班级 / 小型机构）；如需多人并发写入建议迁移 PostgreSQL（Repository 层已隔离，替换成本低）。
- 登录限流为进程内计数，多进程 / 多实例部署时需改用 Redis 等共享存储。
- 目前仅单一管理员角色（Schema 已预留 `role` 字段与 `is_active` 停用位，可扩展多角色）。
- 读者自助续借沿用管理端规则（未逾期、未达上限、未冻结）；预约 / 催还通知等尚未实现。
- 罚金仅做估算与结算展示，未接入支付。
