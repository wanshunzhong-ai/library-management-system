"""数据库连接、Schema 迁移与查询辅助。"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from config import get_config
from library.barcode import generate

logger = logging.getLogger(__name__)

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    isbn             TEXT    NOT NULL UNIQUE,
    barcode          TEXT    NOT NULL DEFAULT '',
    title            TEXT    NOT NULL,
    author           TEXT    NOT NULL DEFAULT '',
    publisher        TEXT    NOT NULL DEFAULT '',
    category         TEXT    NOT NULL DEFAULT '未分类',
    publish_year     INTEGER,
    location         TEXT    NOT NULL DEFAULT '',
    description      TEXT    NOT NULL DEFAULT '',
    total_copies     INTEGER NOT NULL DEFAULT 1 CHECK (total_copies >= 0),
    -- 可借数不得为负、也不得超过总册数（后者是并发还书重复回补的兜底）
    available_copies INTEGER NOT NULL DEFAULT 1
                     CHECK (available_copies >= 0 AND available_copies <= total_copies),
    created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_no       TEXT    NOT NULL UNIQUE,
    name          TEXT    NOT NULL,
    phone         TEXT    NOT NULL DEFAULT '',
    email         TEXT    NOT NULL DEFAULT '',
    member_type   TEXT    NOT NULL DEFAULT '普通',
    status        TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'frozen')),
    max_borrow    INTEGER NOT NULL DEFAULT 5 CHECK (max_borrow >= 1),
    password_hash TEXT    NOT NULL DEFAULT '',
    -- 借书证挂失（读者自助保护）：与 status 正交，挂失不影响查看历史，
    -- 但会挡下借书 / 续借 / 预约，需解挂或换证后恢复。
    card_lost     INTEGER NOT NULL DEFAULT 0 CHECK (card_lost IN (0, 1)),
    lost_at       TEXT,
    -- 首次登录强制改密：馆员建号 / 重置密码 / 办证发证后置 1（此时口令是
    -- 「证号后 6 位」这种可离线猜解的弱口令），读者自行改密后清零。
    must_change_password INTEGER NOT NULL DEFAULT 0 CHECK (must_change_password IN (0, 1)),
    last_login_at TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS loans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE RESTRICT,
    member_id   INTEGER NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    borrowed_at TEXT    NOT NULL,
    due_at      TEXT    NOT NULL,
    returned_at TEXT,
    renew_count INTEGER NOT NULL DEFAULT 0,
    note        TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL DEFAULT '',
    role          TEXT    NOT NULL DEFAULT 'admin' CHECK (role IN ('admin', 'librarian')),
    is_active     INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 预约：读者对「暂无可借复本」的图书排队，到书后转为可借并通知。
-- status 流转：waiting（排队中）→ ready（已到书，可到馆借阅）→ fulfilled（已借出）；
-- 任一未结束状态都可 cancel（读者取消 / 馆员取消）或 expired（过期未取）。
CREATE TABLE IF NOT EXISTS reservations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id)   ON DELETE CASCADE,
    member_id   INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    status      TEXT    NOT NULL DEFAULT 'waiting'
                CHECK (status IN ('waiting', 'ready', 'fulfilled', 'cancelled', 'expired')),
    ready_at    TEXT,
    notified_at TEXT,
    closed_at   TEXT,
    note        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 罚金缴纳记录：读者自助缴纳或馆员代收 / 减免。金额一律为正，方向由 kind 区分。
-- 应缴金额不落库（由 loans + fine.py 统一口径实时计算），这里只记「已经缴了多少」，
-- 避免把派生数据抄成第二份后与逾期天数脱节。
CREATE TABLE IF NOT EXISTS fine_payments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    loan_id    INTEGER NOT NULL REFERENCES loans(id)   ON DELETE CASCADE,
    amount     REAL    NOT NULL CHECK (amount >= 0),
    kind       TEXT    NOT NULL DEFAULT 'payment' CHECK (kind IN ('payment', 'waiver')),
    method     TEXT    NOT NULL DEFAULT '',
    note       TEXT    NOT NULL DEFAULT '',
    operator   TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 公告 / 开馆时间：供未登录的访客在门户首页直接看到，由馆员在后台维护。
-- `kind` 区分「公告」与「开馆时间」（开馆时间本质上是一条长期生效、会被门户
-- 单独渲染的公告）；`publish_at` / `expire_at` 构成生效窗口，为空表示不限。
CREATE TABLE IF NOT EXISTS announcements (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL,
    body       TEXT    NOT NULL DEFAULT '',
    kind       TEXT    NOT NULL DEFAULT 'notice' CHECK (kind IN ('notice', 'hours')),
    level      TEXT    NOT NULL DEFAULT 'info'   CHECK (level IN ('info', 'important')),
    pinned     INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    status     TEXT    NOT NULL DEFAULT 'published'
               CHECK (status IN ('published', 'draft', 'archived')),
    publish_at TEXT,
    expire_at  TEXT,
    operator   TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 在线办证申请：访客自助提交，馆员审核后发证。
-- 出于隐私考虑**只存证件号后 4 位**（`id_tail`），完整证件号由馆员现场核验，
-- 不在系统里再抄一份——与 operation_logs 不记业务快照的原则一致。
CREATE TABLE IF NOT EXISTS member_applications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    code          TEXT    NOT NULL UNIQUE,          -- 申请编号，供访客查询进度
    name          TEXT    NOT NULL,
    phone         TEXT    NOT NULL,
    email         TEXT    NOT NULL DEFAULT '',
    id_type       TEXT    NOT NULL DEFAULT '身份证',
    id_tail       TEXT    NOT NULL DEFAULT '',
    member_type   TEXT    NOT NULL DEFAULT '社会',
    note          TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected')),
    reject_reason TEXT    NOT NULL DEFAULT '',
    member_id     INTEGER REFERENCES members(id) ON DELETE SET NULL,
    card_no       TEXT    NOT NULL DEFAULT '',
    reviewed_by   TEXT    NOT NULL DEFAULT '',
    reviewed_at   TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 站内通知：读者的「消息中心」载体。
-- 到书、预约过期、即将到期、逾期、审核结果、馆员催还都往这里写一条。
-- 本系统没有短信 / 邮件网关，也没有定时任务框架，因此**通知是惰性生成的**：
-- 读者打开个人中心时按需扫描在借记录并补写应得的通知。语义是「只要你打开就一定
-- 看得到该看到的」，代价是「不打开就不会被推送到」——这一点在 README 里明确写出，
-- 避免把「站内通知」说成「主动推送」。
-- `dedupe_key` 是幂等键（配合下面的部分唯一索引），同一个业务事件只落一条，
-- 反复扫描不会给读者刷出一屏重复消息。
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    kind       TEXT    NOT NULL DEFAULT 'system'
               CHECK (kind IN ('reservation_ready', 'reservation_expired', 'due_soon',
                               'overdue', 'fine', 'profile', 'book_request', 'system')),
    title      TEXT    NOT NULL,
    body       TEXT    NOT NULL DEFAULT '',
    link_view  TEXT    NOT NULL DEFAULT '',   -- 前端跳转锚点：loans / reservations / fines / bookshelf
    ref_type   TEXT    NOT NULL DEFAULT '',   -- loan / reservation / profile_request / book_request
    ref_id     INTEGER,
    dedupe_key TEXT    NOT NULL DEFAULT '',
    read_at    TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 我的书架（收藏）：读者把感兴趣的书收起来，与「预约」「借阅」都独立。
-- 收藏不占用任何库存与额度，纯粹是个人清单，因此不设数量上限（由前端提示）。
CREATE TABLE IF NOT EXISTS book_favorites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    book_id    INTEGER NOT NULL REFERENCES books(id)   ON DELETE CASCADE,
    note       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (member_id, book_id)
);

-- 图书评价：评分 + 短评。
-- **只有借过这本书的读者才能评价**——否则评分可以被随手刷，也就失去了参考价值。
-- 一人一书一条（UNIQUE），可修改；`status` 供馆员隐藏明显不当的内容。
CREATE TABLE IF NOT EXISTS book_reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id    INTEGER NOT NULL REFERENCES books(id)   ON DELETE CASCADE,
    member_id  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    rating     INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    content    TEXT    NOT NULL DEFAULT '',
    status     TEXT    NOT NULL DEFAULT 'published' CHECK (status IN ('published', 'hidden')),
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (member_id, book_id)
);

-- 荐购登记：读者登记「希望馆里买这本」，馆员处理后回写结果。
-- 与「收藏」的区别是它面向**馆里还没有的书**，是采购线索而不是个人清单。
CREATE TABLE IF NOT EXISTS book_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    title       TEXT    NOT NULL,
    author      TEXT    NOT NULL DEFAULT '',
    isbn        TEXT    NOT NULL DEFAULT '',
    publisher   TEXT    NOT NULL DEFAULT '',
    reason      TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'purchased', 'rejected')),
    reply       TEXT    NOT NULL DEFAULT '',
    reviewed_by TEXT    NOT NULL DEFAULT '',
    reviewed_at TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 资料变更申请：姓名 / 读者类型这类字段读者不能直接改（涉及证件与借阅权限），
-- 改为「读者申请 → 馆员审核」。联系电话走自助修改，不必再走审批。
-- 同一读者同一字段只允许有一条待审记录，避免重复提交堆成垃圾。
CREATE TABLE IF NOT EXISTS profile_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    field       TEXT    NOT NULL CHECK (field IN ('name', 'member_type')),
    old_value   TEXT    NOT NULL DEFAULT '',
    new_value   TEXT    NOT NULL,
    reason      TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected')),
    reply       TEXT    NOT NULL DEFAULT '',
    reviewed_by TEXT    NOT NULL DEFAULT '',
    reviewed_at TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 赔偿处理：丢书 / 损书的赔付登记与结案。
-- 与「罚金」是两回事：罚金是逾期违约金，赔偿是图书**本体**的损失，两者可并存。
-- 结案时可选「按丢失结案」——借阅置为已归还但**不回补库存**（书已经没了），
-- 同时把总册数 -1，保证「可借数 = 总册数 - 在借数」这条不变量始终成立。
CREATE TABLE IF NOT EXISTS compensations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id     INTEGER NOT NULL REFERENCES loans(id)   ON DELETE CASCADE,
    member_id   INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    book_id     INTEGER NOT NULL REFERENCES books(id)   ON DELETE CASCADE,
    kind        TEXT    NOT NULL DEFAULT 'lost' CHECK (kind IN ('lost', 'damaged')),
    amount      REAL    NOT NULL CHECK (amount >= 0),
    paid_amount REAL    NOT NULL DEFAULT 0 CHECK (paid_amount >= 0),
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'settled', 'cancelled')),
    note        TEXT    NOT NULL DEFAULT '',
    operator    TEXT    NOT NULL DEFAULT '',
    settled_at  TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 盘点单：一次「按架位清点在架册数」的作业。
-- 开单时把该范围内的图书**快照**成明细（expected = 开单时刻的总册数），
-- 之后录入实盘数即可对比出盘盈 / 盘亏；关闭后冻结只读，可选择性按差异调账。
CREATE TABLE IF NOT EXISTS stocktaking_batches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code       TEXT    NOT NULL UNIQUE,          -- 盘点单号，如 PD20261011-001
    location   TEXT    NOT NULL DEFAULT '',      -- 空串 = 全馆
    status     TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    note       TEXT    NOT NULL DEFAULT '',
    operator   TEXT    NOT NULL DEFAULT '',
    item_count INTEGER NOT NULL DEFAULT 0 CHECK (item_count >= 0),
    closed_at  TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS stocktaking_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id        INTEGER NOT NULL REFERENCES stocktaking_batches(id) ON DELETE CASCADE,
    book_id         INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    expected_copies INTEGER NOT NULL DEFAULT 0 CHECK (expected_copies >= 0),
    counted_copies  INTEGER CHECK (counted_copies IS NULL OR counted_copies >= 0),
    note            TEXT    NOT NULL DEFAULT '',
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (batch_id, book_id)
);

-- 采编验收单：采购到馆后先登记，再逐条验收。
-- 验收时按 ISBN 去重——馆里已有同 ISBN 就**加册**（而不是建一条重复书目），
-- 没有才新建图书；`book_id` 回填验收结果，便于后续追溯是哪批采购进来的。
CREATE TABLE IF NOT EXISTS acquisitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    isbn          TEXT    NOT NULL DEFAULT '',
    title         TEXT    NOT NULL,
    author        TEXT    NOT NULL DEFAULT '',
    publisher     TEXT    NOT NULL DEFAULT '',
    category      TEXT    NOT NULL DEFAULT '未分类',
    publish_year  INTEGER,
    location      TEXT    NOT NULL DEFAULT '',
    copies        INTEGER NOT NULL DEFAULT 1 CHECK (copies >= 1),
    unit_price    REAL    NOT NULL DEFAULT 0 CHECK (unit_price >= 0),
    supplier      TEXT    NOT NULL DEFAULT '',
    note          TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'accepted', 'rejected')),
    book_id       INTEGER REFERENCES books(id) ON DELETE SET NULL,
    reject_reason TEXT    NOT NULL DEFAULT '',
    reviewed_by   TEXT    NOT NULL DEFAULT '',
    reviewed_at   TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 读者附件：照片与证件影像。
-- 只存**随机存储名**与原始展示名，不落磁盘绝对路径（路径由上传根目录 + 读者 id
-- 拼出），避免把部署路径写进数据、也避免路径穿越。文件落 `data/uploads/<member_id>/`。
CREATE TABLE IF NOT EXISTS member_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    kind        TEXT    NOT NULL DEFAULT 'photo' CHECK (kind IN ('photo', 'id_card')),
    label       TEXT    NOT NULL DEFAULT '',
    filename    TEXT    NOT NULL,
    stored_name TEXT    NOT NULL,
    mime        TEXT    NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0 CHECK (size >= 0),
    operator    TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 操作日志：谁在什么时候改了什么。分权的前提是能追责，否则拆权限等于拆掉问责能力。
-- 只记「谁 + 做了什么 + 打到哪条记录」，不记业务字段快照（避免把读者联系方式等
-- 个人信息复制成第二份、扩大泄露面）。
CREATE TABLE IF NOT EXISTS operation_logs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_type TEXT    NOT NULL DEFAULT 'system',   -- admin / reader / system
    actor_id   INTEGER,
    actor_name TEXT    NOT NULL DEFAULT '',
    actor_role TEXT    NOT NULL DEFAULT '',
    action     TEXT    NOT NULL,                    -- 例如 book.update / account.disable
    entity     TEXT    NOT NULL DEFAULT '',         -- book / member / loan / account
    entity_id  TEXT    NOT NULL DEFAULT '',
    summary    TEXT    NOT NULL DEFAULT '',         -- 人类可读的一句话
    detail     TEXT    NOT NULL DEFAULT '',         -- 结构化补充（JSON），已截断
    status     TEXT    NOT NULL DEFAULT 'ok',       -- ok / denied
    ip         TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 后台扫描任务的执行留痕：谁（定时线程 / 管理员手动）、何时、跑了多久、结果如何。
-- 只有「可观测」的调度才敢放心改间隔——出了问题有据可查，而不是只能翻应用日志。
CREATE TABLE IF NOT EXISTS job_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT    NOT NULL,                    -- sweep（目前唯一任务，留扩展位）
    kind        TEXT    NOT NULL DEFAULT 'auto',     -- auto（定时线程）/ manual（管理员手动触发）
    started_at  TEXT    NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    ok          INTEGER NOT NULL DEFAULT 1,
    result      TEXT    NOT NULL DEFAULT '',         -- JSON 计数（释放了几单、提醒了几人…）
    error       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at DESC);

-- 邮件出站队列：站内通知之外的第二条触达渠道。入队与发送分离——
-- 入队永远成功（SMTP 未配置就停在 pending），发送失败退避重试、超限转 failed。
CREATE TABLE IF NOT EXISTS outbound_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id   INTEGER NOT NULL,
    channel     TEXT    NOT NULL DEFAULT 'email',
    to_address  TEXT    NOT NULL DEFAULT '',
    subject     TEXT    NOT NULL,
    body        TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT    NOT NULL DEFAULT '',
    dedupe_key  TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    sent_at     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbound_dedupe
    ON outbound_messages(dedupe_key) WHERE dedupe_key <> '';
CREATE INDEX IF NOT EXISTS idx_outbound_status ON outbound_messages(status, id);

-- 轻量审批工单：敏感操作（罚金减免）超过阈值时先「提交」再「审批」，
-- 把「操作权力」和「批准权力」拆开。审批通过才真正执行业务写入。
CREATE TABLE IF NOT EXISTS approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,                   -- fine_waiver（罚金减免）
    entity_id    INTEGER,
    entity_label TEXT    NOT NULL DEFAULT '',        -- 人类可读的一句话（谁、哪笔、多少钱）
    member_id    INTEGER,
    amount       REAL    NOT NULL DEFAULT 0,
    payload      TEXT    NOT NULL DEFAULT '',        -- 原始请求（JSON），审批通过后按它执行
    requested_by TEXT    NOT NULL DEFAULT '',
    requested_at TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    note         TEXT    NOT NULL DEFAULT '',        -- 申请人理由
    status       TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    reviewed_by  TEXT    NOT NULL DEFAULT '',
    reviewed_at  TEXT,
    review_note  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, id DESC);

CREATE INDEX IF NOT EXISTS idx_books_title      ON books(title);
CREATE INDEX IF NOT EXISTS idx_books_category   ON books(category);
CREATE INDEX IF NOT EXISTS idx_books_author     ON books(author);
CREATE INDEX IF NOT EXISTS idx_members_name     ON members(name);
CREATE INDEX IF NOT EXISTS idx_members_card     ON members(card_no);
CREATE INDEX IF NOT EXISTS idx_members_phone    ON members(phone);
CREATE INDEX IF NOT EXISTS idx_loans_book       ON loans(book_id);
CREATE INDEX IF NOT EXISTS idx_loans_member     ON loans(member_id);
CREATE INDEX IF NOT EXISTS idx_loans_open       ON loans(returned_at, due_at);
-- 预约：按书查排队 / 按读者查我的预约；未结束的唯一约束（同一读者同一本书只能有一条）
CREATE INDEX IF NOT EXISTS idx_reservations_book   ON reservations(book_id, status, id);
CREATE INDEX IF NOT EXISTS idx_reservations_member ON reservations(member_id, status, id DESC);
-- 取书保留期扫描：只关心「状态是 ready」的那些，按到书时间判断是否超期
CREATE INDEX IF NOT EXISTS idx_reservations_ready  ON reservations(status, ready_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reservations_open_unique
    ON reservations(book_id, member_id) WHERE status IN ('waiting', 'ready');
-- 罚金缴纳：按读者查缴纳历史 / 按借阅记录核对该笔已缴金额
CREATE INDEX IF NOT EXISTS idx_fine_payments_member ON fine_payments(member_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_fine_payments_loan   ON fine_payments(loan_id);
-- 公告：门户首页按「置顶 → 生效开始时间」倒序取当前生效的那几条
CREATE INDEX IF NOT EXISTS idx_announcements_active
    ON announcements(status, pinned DESC, publish_at DESC, id DESC);
-- 办证申请：后台按状态分页、按手机号查重、按编号取件
CREATE INDEX IF NOT EXISTS idx_member_applications_status ON member_applications(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_member_applications_phone  ON member_applications(phone, id DESC);
-- 通知：按读者取未读 / 全量倒序；dedupe_key 非空时同一业务事件只落一条
CREATE INDEX IF NOT EXISTS idx_notifications_member
    ON notifications(member_id, read_at, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedupe
    ON notifications(member_id, dedupe_key) WHERE dedupe_key <> '';
-- 收藏：一个读者一本书只能有一条；按读者倒序取个人书架
CREATE INDEX IF NOT EXISTS idx_book_favorites_member ON book_favorites(member_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_book_favorites_book   ON book_favorites(book_id);
-- 评价：按书取已发布的评价（倒序）；按读者取「我的评价」
CREATE INDEX IF NOT EXISTS idx_book_reviews_book   ON book_reviews(book_id, status, id DESC);
CREATE INDEX IF NOT EXISTS idx_book_reviews_member ON book_reviews(member_id, id DESC);
-- 荐购：后台按状态分页、读者按本人查看
CREATE INDEX IF NOT EXISTS idx_book_requests_status ON book_requests(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_book_requests_member ON book_requests(member_id, id DESC);
-- 资料变更：后台按状态分页；同一读者同一字段只允许一条待审
CREATE INDEX IF NOT EXISTS idx_profile_requests_status ON profile_requests(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_profile_requests_member ON profile_requests(member_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_requests_open
    ON profile_requests(member_id, field) WHERE status = 'pending';
-- 赔偿：后台按状态分页、按读者 / 借阅记录追溯；同一笔借阅不得有两条待结案赔付单
CREATE INDEX IF NOT EXISTS idx_compensations_status ON compensations(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_compensations_member ON compensations(member_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_compensations_loan   ON compensations(loan_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_compensations_open_unique
    ON compensations(loan_id) WHERE status = 'pending';
-- 盘点：明细按单取；同一架位同时只允许一张开启中的盘点单（避免两单互相打架）
CREATE INDEX IF NOT EXISTS idx_stocktaking_items_batch ON stocktaking_items(batch_id, id);
CREATE INDEX IF NOT EXISTS idx_stocktaking_items_counted
    ON stocktaking_items(batch_id, counted_copies);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stocktaking_open_location
    ON stocktaking_batches(location) WHERE status = 'open';
-- 采编验收：后台按状态分页与关键字检索
CREATE INDEX IF NOT EXISTS idx_acquisitions_status ON acquisitions(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_acquisitions_isbn   ON acquisitions(isbn);
-- 读者附件：按读者 + 类型取列表
CREATE INDEX IF NOT EXISTS idx_member_files_member ON member_files(member_id, kind, id DESC);
CREATE INDEX IF NOT EXISTS idx_logs_created     ON operation_logs(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_logs_actor       ON operation_logs(actor_type, actor_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_logs_entity      ON operation_logs(entity, entity_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_logs_action      ON operation_logs(action, id DESC);
"""


def _connect() -> sqlite3.Connection:
    cfg = get_config()
    db_path = Path(cfg.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def get_conn() -> sqlite3.Connection:
    """请求内复用同一个连接；非 Web 上下文（测试/脚本）退化为线程级连接。"""
    try:
        from flask import g, has_app_context
    except ImportError:  # pragma: no cover - Flask 一定存在
        has_app_context = lambda: False  # noqa: E731
        g = None  # type: ignore[assignment]

    if g is not None and has_app_context():
        conn = getattr(g, "_library_conn", None)
        if conn is None:
            conn = _connect()
            g._library_conn = conn
        return conn

    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def close_conn(exc: BaseException | None = None) -> None:
    """Flask teardown：请求结束关闭连接。"""
    try:
        from flask import g, has_app_context
    except ImportError:  # pragma: no cover
        return
    if not has_app_context():
        return
    conn = getattr(g, "_library_conn", None)
    if conn is not None:
        conn.close()
        g._library_conn = None


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """显式事务：成功提交，异常回滚。用于借书/还书这类多表联动写操作。

    ``immediate=True`` 使用 ``BEGIN IMMEDIATE``：事务开始即取写锁，把并发写请求
    串行化。默认的 deferred 事务只有在首次写入时才取锁，事务内的 SELECT 仍读旧快照，
    「先查后写」的校验（借阅上限、重复借、续借次数）会被并发绕过 —— 凡是有
    「读-判断-写」语义的借阅写路径都必须用 immediate。
    """
    conn = get_conn()
    if immediate and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def query_all(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cur = get_conn().execute(sql, params)
    try:
        return [dict(row) for row in cur.fetchall()]
    finally:
        cur.close()


def query_one(sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    cur = get_conn().execute(sql, params)
    try:
        row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        cur.close()


def query_scalar(sql: str, params: Sequence[Any] = ()) -> Any:
    cur = get_conn().execute(sql, params)
    try:
        row = cur.fetchone()
        return row[0] if row is not None else None
    finally:
        cur.close()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """执行写语句，返回 lastrowid。"""
    with transaction() as conn:
        cur = conn.execute(sql, params)
        last_id = cur.lastrowid
        cur.close()
    return int(last_id or 0)


def executemany(sql: str, seq: Iterable[Sequence[Any]]) -> None:
    with transaction() as conn:
        conn.executemany(sql, seq)


def order_clause(
    filters: Mapping[str, Any], columns: Mapping[str, str], default: str
) -> str:
    """把 ``sort`` / ``order`` 查询参数解析成安全的 ORDER BY 片段。

    ``columns`` 是「对外排序键 → 真实列表达式」的白名单，未命中或方向非法时
    回落到 ``default``。拼进 SQL 的字符串只来自代码里的常量映射，
    用户输入仅用于查字典与二选一，因此不存在拼接注入面。
    """
    key = str(filters.get("sort") or "").strip().lower()
    column = columns.get(key)
    if not column:
        return default
    direction = "ASC" if str(filters.get("order") or "").strip().lower() == "asc" else "DESC"
    return f"{column} {direction}"


def init_db() -> None:
    """建表 + 建索引 + 增量迁移（幂等）。"""
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    logger.info("数据库已就绪", extra={"extra_fields": {"db_path": get_config().db_path}})


# 老库升级用的增量迁移：新库由 SCHEMA 直接建全，这里只补历史库缺的列。
# 每条迁移都是幂等的（先查 PRAGMA 再决定加不加），可安全重复执行。
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("members", "password_hash", "ALTER TABLE members ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''"),
    ("members", "last_login_at", "ALTER TABLE members ADD COLUMN last_login_at TEXT"),
    ("books", "barcode", "ALTER TABLE books ADD COLUMN barcode TEXT NOT NULL DEFAULT ''"),
    ("books", "description", "ALTER TABLE books ADD COLUMN description TEXT NOT NULL DEFAULT ''"),
    # 借书证挂失：独立于 status（frozen 是馆方处置，挂失是读者自我保护），
    # 因此不能复用 status，否则自助挂失会与"未还清不可冻结"的馆方规则打架。
    ("members", "card_lost", "ALTER TABLE members ADD COLUMN card_lost INTEGER NOT NULL DEFAULT 0"),
    ("members", "lost_at", "ALTER TABLE members ADD COLUMN lost_at TEXT"),
    # 首次登录强制改密。老库里的读者一律置 0（不追溯打扰），只有此后新建 /
    # 重置密码的账号才会被要求改密。
    (
        "members",
        "must_change_password",
        "ALTER TABLE members ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0",
    ),
    # 邮件订阅开关：读者可退订催还 / 到期提醒之外的营销性邮件？——不，
    # 本系统只发业务必需邮件（催还 / 到期 / 到书），这里给读者的是「一键全关」的
    # 尊重开关；默认开启，退订后出站队列直接跳过该读者。
    (
        "members",
        "email_subscribed",
        "ALTER TABLE members ADD COLUMN email_subscribed INTEGER NOT NULL DEFAULT 1 "
        "CHECK (email_subscribed IN (0, 1))",
    ),
)

# 条码唯一索引必须晚于「列创建 + 存量回填」执行，因此不放进 SCHEMA，
# 由 _migrate 在回填之后幂等创建。
_BARCODE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_books_barcode ON books(barcode) WHERE barcode <> ''"
)

# 同一读者对同一本书只能有一条「未归还」记录。
# 应用层已有校验，这里是数据库层兜底，防止并发写入产生重复在借。
_LOANS_OPEN_UNIQUE_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_loans_open_unique "
    "ON loans(book_id, member_id) WHERE returned_at IS NULL"
)

# 手机号与证号一样是登录账号，必须全局唯一（空串代表未填，不参与约束）。
# 应用层校验之外再加数据库兜底，避免并发建档写入重复手机号后身份归属不明。
_MEMBERS_PHONE_UNIQUE_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_members_phone_unique "
    "ON members(phone) WHERE phone <> ''"
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    try:
        return {str(row[1]) for row in cur.fetchall()}
    finally:
        cur.close()


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _COLUMN_MIGRATIONS:
        if column in _table_columns(conn, table):
            continue
        conn.execute(ddl)
        logger.info(
            "数据库迁移完成",
            extra={"extra_fields": {"table": table, "added_column": column}},
        )
    _backfill_barcodes(conn)
    conn.execute(_BARCODE_INDEX_SQL)
    _repair_inventory(conn)
    _create_open_loan_index(conn)
    _create_member_phone_index(conn)


def _repair_inventory(conn: sqlite3.Connection) -> None:
    """把越界的可借数夹回合法区间（幂等）。

    历史版本在并发还书时可能重复回补库存，导致 ``available_copies > total_copies``；
    老库无法通过 ALTER 补上 CHECK 约束，因此这里做一次性数据修复。
    """
    over = conn.execute(
        "UPDATE books SET available_copies = total_copies WHERE available_copies > total_copies"
    )
    under = conn.execute("UPDATE books SET available_copies = 0 WHERE available_copies < 0")
    fixed = int(over.rowcount or 0) + int(under.rowcount or 0)
    if fixed:
        logger.warning(
            "库存越界数据已修复",
            extra={"extra_fields": {"fixed_rows": fixed}},
        )


def _create_open_loan_index(conn: sqlite3.Connection) -> None:
    """创建「同一读者同一本书只能有一条未归还记录」的唯一索引。

    老库若已存在重复在借数据，建索引会失败：此时记警告并跳过，
    不自动改写借阅记录（由 check_consistency() 报告，交人工处理）。
    """
    try:
        conn.execute(_LOANS_OPEN_UNIQUE_SQL)
    except sqlite3.IntegrityError as exc:
        logger.warning(
            "存在重复在借记录，未能创建唯一索引 idx_loans_open_unique：%s；"
            "请先用 check_consistency() 排查",
            exc,
        )


def _create_member_phone_index(conn: sqlite3.Connection) -> None:
    """创建手机号唯一索引（空串不参与）。

    老库若已有重复手机号，建索引会失败：记警告并跳过，不自动清空任何人的
    联系方式（那是丢数据），由 check_consistency() 报告后交人工处理。
    """
    try:
        conn.execute(_MEMBERS_PHONE_UNIQUE_SQL)
    except sqlite3.IntegrityError as exc:
        logger.warning(
            "存在重复手机号，未能创建唯一索引 idx_members_phone_unique：%s；"
            "请先用 check_consistency() 排查",
            exc,
        )


def check_consistency() -> dict[str, Any]:
    """数据一致性自检：库存越界、重复在借、库存总量与在借数是否对得上。

    返回 ``{"ok": bool, "issues": [...]}``，可用于运维巡检与测试断言。
    """
    issues: list[dict[str, Any]] = []

    for row in query_all(
        "SELECT id, title, total_copies, available_copies FROM books "
        " WHERE available_copies < 0 OR available_copies > total_copies"
    ):
        issues.append({"type": "inventory_out_of_range", **row})

    for row in query_all(
        "SELECT book_id, member_id, COUNT(*) AS duplicates FROM loans "
        " WHERE returned_at IS NULL GROUP BY book_id, member_id HAVING duplicates > 1"
    ):
        issues.append({"type": "duplicate_open_loan", **row})

    for row in query_all(
        "SELECT phone, COUNT(*) AS duplicates FROM members "
        " WHERE phone <> '' GROUP BY phone HAVING duplicates > 1"
    ):
        issues.append({"type": "duplicate_member_phone", **row})

    for row in query_all(
        "SELECT m.id AS member_id, m.card_no, m.phone, x.id AS other_member_id "
        "  FROM members m JOIN members x ON x.card_no = m.phone AND x.id <> m.id "
        " WHERE m.phone <> ''"
    ):
        issues.append({"type": "phone_shadows_card_no", **row})

    for row in query_all(
        "SELECT m.id AS member_id, m.card_no, m.phone, a.username "
        "  FROM members m JOIN admins a "
        "    ON a.username = m.card_no OR (m.phone <> '' AND a.username = m.phone)"
    ):
        issues.append({"type": "member_identifier_shadows_admin", **row})

    summary = query_one(
        "SELECT (SELECT COALESCE(SUM(total_copies), 0)     FROM books) AS total_copies,"
        "       (SELECT COALESCE(SUM(available_copies), 0) FROM books) AS available_copies,"
        "       (SELECT COUNT(*) FROM loans WHERE returned_at IS NULL) AS open_loans"
    ) or {}
    total_copies = int(summary.get("total_copies") or 0)
    available_copies = int(summary.get("available_copies") or 0)
    open_loans = int(summary.get("open_loans") or 0)
    if available_copies != total_copies - open_loans:
        issues.append(
            {
                "type": "inventory_mismatch",
                "available_copies": available_copies,
                "expected": total_copies - open_loans,
            }
        )

    return {
        "ok": not issues,
        "issues": issues,
        "total_copies": total_copies,
        "available_copies": available_copies,
        "open_loans": open_loans,
    }


def repair_consistency() -> dict[str, Any]:
    """修复有明确正确答案的数据问题：同一读者同一本书的重复在借记录。

    保留最早的一条，把多余的标记为已归还（归还时间取其应还日期），并把因此
    多出来的那一册补回可借数——账实依旧对得上。只碰这一种情况，其余问题
    （例如重复手机号）一律留给人工判断，避免误改真实数据。

    返回 ``{"closed_loans": n, "before": ..., "after": ...}``。
    """
    before = check_consistency()
    closed = 0
    with transaction(immediate=True) as conn:
        groups = conn.execute(
            "SELECT book_id, member_id FROM loans "
            " WHERE returned_at IS NULL GROUP BY book_id, member_id HAVING COUNT(*) > 1"
        ).fetchall()
        for book_id, member_id in groups:
            duplicates = conn.execute(
                "SELECT id, due_at FROM loans WHERE book_id = ? AND member_id = ? "
                "  AND returned_at IS NULL ORDER BY id",
                [book_id, member_id],
            ).fetchall()
            for loan_id, due_at in duplicates[1:]:
                conn.execute(
                    "UPDATE loans SET returned_at = ? WHERE id = ? AND returned_at IS NULL",
                    [due_at or _now_str(), loan_id],
                )
                conn.execute(
                    "UPDATE books SET available_copies = MIN(total_copies, available_copies + 1), "
                    "       updated_at = datetime('now', 'localtime') WHERE id = ?",
                    [book_id],
                )
                closed += 1

    if closed:
        # 数据干净了，把当初建不上的唯一索引补上
        conn = get_conn()
        _create_open_loan_index(conn)
        conn.commit()
        logger.warning(
            "已修复重复在借记录",
            extra={"extra_fields": {"closed_loans": closed}},
        )
    after = check_consistency()
    return {"closed_loans": closed, "before": before, "after": after}


def _now_str() -> str:
    from library import timeutil

    return timeutil.to_str(timeutil.now())


def _backfill_barcodes(conn: sqlite3.Connection) -> None:
    """为缺条码的图书补生成馆藏条码号（幂等）。

    条码号默认取「BK + 7 位序号」，序号优先复用自己的图书 id；
    若该号已被手工占用则顺延到第一个未占用的序号。
    """
    rows = conn.execute("SELECT id FROM books WHERE barcode IS NULL OR barcode = '' ORDER BY id").fetchall()
    if not rows:
        return

    used = {
        str(row[0]).upper()
        for row in conn.execute("SELECT barcode FROM books WHERE barcode <> ''").fetchall()
    }
    seq = 1
    for row in rows:
        book_id = int(row[0])
        candidate = generate(book_id)
        if candidate in used:
            while generate(seq) in used:
                seq += 1
            candidate = generate(seq)
        used.add(candidate)
        conn.execute("UPDATE books SET barcode = ? WHERE id = ?", [candidate, book_id])

    logger.info(
        "数据库迁移完成",
        extra={"extra_fields": {"table": "books", "backfilled_barcodes": len(rows)}},
    )
