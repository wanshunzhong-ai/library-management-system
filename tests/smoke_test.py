"""端到端冒烟测试：用 Flask 测试客户端走完读者门户、登录鉴权与后台业务链路。

运行：python tests/smoke_test.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config, get_config  # noqa: E402
from library import db, timeutil  # noqa: E402
from library.auth import service as auth_service  # noqa: E402
from library.validation import isbn_check  # noqa: E402
from library.web import create_app  # noqa: E402

PASSED = 0
FAILED = 0

ADMIN_USER = "admin"
ADMIN_PWD = "admin123"


def check(label: str, condition: bool, extra: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label} {extra}")


def config_rejected(module) -> bool:
    """调用 module.load_config() 并判断是否被配置校验拒绝。"""
    try:
        module.load_config()
    except module.ConfigError:
        return True
    return False


def with_ctx(app, func, *args, **kwargs):
    """在应用上下文里调用 ``func``。

    ``get_conn()`` 在应用上下文内走 ``flask.g``（按当前配置连库），在上下文外退化
    为**线程级缓存连接**——本测试文件早前已建立过指向别的临时库的连接，因此凡是
    直接调用后台扫描等「非请求路径」的逻辑，都必须像后台线程那样显式进上下文。
    """
    with app.app_context():
        return func(*args, **kwargs)


def librarian_client(app, username="smoke-librarian", password="librarian123"):
    """建一个流通馆员账号并返回其登录后的客户端。

    用于验证「馆员看不到管理员功能」这类**能力边界**断言——权限矩阵若被改错，
    这里会立刻红。
    """
    from library.auth import passwords as auth_passwords  # noqa: PLC0415

    with app.app_context():
        row = db.query_one("SELECT id FROM admins WHERE username = ?", [username])
        if row is None:
            db.execute(
                "INSERT INTO admins (username, password_hash, display_name, role, is_active) "
                "VALUES (?, ?, ?, 'librarian', 1)",
                [username, auth_passwords.hash_password(password), "冒烟测试馆员"],
            )
    client = Api(app.test_client())
    payload = client.post("/api/auth/login", json={"username": username, "password": password}).get_json() or {}
    client.csrf = payload.get("csrf_token")
    return client


def _b12_fake_restore_rejected(root: Path) -> bool:
    """用一个假备份文件调恢复，期望在写目标库之前就被拒（``ValueError``）。"""
    from library import backup as backup_module  # noqa: PLC0415

    fake = Path(root) / "not-a-db.db"
    fake.write_bytes(b"this is definitely not a sqlite database")
    try:
        backup_module.restore_backup(fake)
    except ValueError:
        return True
    except FileNotFoundError:
        return False
    return False


def _b12_restore_keeps_safety_copy(root: Path) -> bool:
    """恢复时目标库已存在 → 必须先留一份 ``library-pre-restore-*.db``。

    「恢复错了还能回头」全靠这份副本，属于运维安全的最后一道闸，必须钉死。
    首次恢复（目标库不存在）按设计**不会**留副本，所以这里先造一个已存在的
    目标库，再走一次完整的「备份文件 → 覆盖目标」：
    目标已存在 + ``safety_copy`` 指向真实文件 = 通过。
    """
    from dataclasses import replace as _dc_replace  # noqa: PLC0415
    from config import get_config, set_config  # noqa: PLC0415
    from library import backup as backup_module  # noqa: PLC0415

    backups = Path(root) / "backups"
    sources = [p for p in sorted(backups.glob("library-*.db")) if p.is_file()]
    if not sources:
        return False

    work = Path(root) / "safety"
    work.mkdir(parents=True, exist_ok=True)
    target = work / "s.db"
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS books (id INTEGER PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()

    saved = get_config()
    try:
        set_config(_dc_replace(saved, db_path=str(target), backup_dir=str(backups)))
        result = backup_module.restore_backup(sources[0])
    finally:
        set_config(saved)

    safety = Path(result.get("safety_copy") or "")
    return safety.name.startswith("library-pre-restore-") and safety.is_file()


@contextlib.contextmanager
def b11_env(**values):
    """临时设置环境变量并在退出时还原（配置解析用例专用）。

    ``None`` 表示「临时删掉这个变量」，用来测「未设置时取默认值」。
    """
    saved = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = str(value)
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def build_app(db_path: str):
    cfg = Config(
        db_path=db_path,
        host="127.0.0.1",
        port=8000,
        debug=False,
        loan_days=30,
        max_renew=2,
        fine_per_day=0.5,
        default_max_borrow=5,
        log_level="WARNING",
        secret_key="test-secret",
        library_name="测试图书馆",
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PWD,
        login_max_attempts=3,
        login_lock_minutes=5,
        cookie_secure=False,
    )
    return create_app(cfg)


class Api:
    """带 CSRF 头的测试客户端封装。"""

    def __init__(self, client):
        self.client = client
        self.csrf = None

    def _headers(self, method):
        if method in ("GET", "HEAD"):
            return {}
        return {"X-CSRF-Token": self.csrf} if self.csrf else {}

    def get(self, path, **kw):
        return self.client.get(path, **kw)

    def post(self, path, **kw):
        headers = dict(kw.pop("headers", {}) or {})
        headers.update(self._headers("POST"))
        return self.client.post(path, headers=headers, **kw)

    def put(self, path, **kw):
        headers = dict(kw.pop("headers", {}) or {})
        headers.update(self._headers("PUT"))
        return self.client.put(path, headers=headers, **kw)

    def delete(self, path, **kw):
        headers = dict(kw.pop("headers", {}) or {})
        headers.update(self._headers("DELETE"))
        return self.client.delete(path, headers=headers, **kw)


def main() -> int:
    auth_service.login_throttle.reset()
    tmp_dir = tempfile.mkdtemp(prefix="library-smoke-")
    db_path = os.path.join(tmp_dir, "test.db")
    app = build_app(db_path)
    client = app.test_client()
    api = Api(client)

    print("\n=== 1. 运维探针 ===")
    check("GET /health 返回 200", client.get("/health").status_code == 200)
    check("GET /ready 返回 200", client.get("/ready").status_code == 200)

    print("\n=== 2. 统一登录页（首页） ===")
    r = client.get("/")
    body = r.get_data(as_text=True)
    check("首页可访问", r.status_code == 200, r.status_code)
    check("首页含馆名", "测试图书馆" in body, body[:80])
    check("首页即统一登录表单", 'id="portalLoginForm"' in body)
    check("首页登录框同时服务读者与馆员", "借书证号 / 手机号 / 工作账号" in body)
    check("首页仍保留馆藏检索", 'id="searchForm"' in body)
    check("首页保留推荐与规则区块", 'id="recommendCards"' in body and 'id="ruleGrid"' in body)
    check("首页不再有独立的管理员登录链接", 'href="/login"' not in body)

    r = client.get("/api/public/overview")
    check("门户概览免登录可访问", r.status_code == 200, r.status_code)
    data = r.get_json()
    for key in ("library", "stats", "rules", "categories", "recommended", "new_arrivals"):
        check(f"门户概览包含 {key}", key in data, list(data.keys()))

    r = client.get("/api/public/search", query_string={"keyword": "Python"})
    check("门户检索免登录可用", r.status_code == 200, r.status_code)
    payload = r.get_json()
    check("门户检索有分页字段", all(k in payload for k in ("items", "total", "page", "pages")), list(payload.keys()))
    if payload["items"]:
        item = payload["items"][0]
        check("门户结果含在架状态", "status_label" in item and "available" in item, list(item.keys()))
        check("门户结果含馆藏位置", "location" in item, list(item.keys()))
        check("门户结果不含读者信息", "member_name" not in item and "member_card" not in item, list(item.keys()))

    print("\n=== 3. 未登录访问后台被拦截 ===")
    check("GET /api/books 未登录 401", client.get("/api/books").status_code == 401)
    check("GET /api/members 未登录 401", client.get("/api/members").status_code == 401)
    check("GET /api/dashboard/overview 未登录 401", client.get("/api/dashboard/overview").status_code == 401)
    check("POST /api/loans/borrow 未登录 401", client.post("/api/loans/borrow", json={}).status_code == 401)
    check("POST /api/books/1/shelf 未登录 401", client.post("/api/books/1/shelf", json={"total_copies": 0}).status_code == 401)
    r = client.get("/admin")
    check(
        "GET /admin 未登录跳统一登录页",
        r.status_code == 302 and r.headers.get("Location", "").startswith("/?next="),
        r.headers.get("Location"),
    )
    r = client.get("/reader")
    check(
        "GET /reader 未登录跳统一登录页",
        r.status_code == 302 and r.headers.get("Location", "").startswith("/?next="),
        r.headers.get("Location"),
    )
    r = client.get("/api/auth/me")
    check("未登录 /api/auth/me 返回 401", r.status_code == 401, r.status_code)

    print("\n=== 4. 统一登录（自动识别身份） ===")
    r = client.post("/api/auth/login", json={"identifier": ADMIN_USER, "password": "wrong-password"})
    check("密码错误返回 401", r.status_code == 401, r.status_code)
    check("错误信息统一", r.get_json()["error"]["message"] == "账号或密码不正确", r.get_json())
    check("失败信息不泄露账号是否存在", "不存在" not in r.get_json()["error"]["message"], r.get_json())

    r = client.post("/api/auth/login", json={"identifier": ADMIN_USER, "password": ""})
    check("空密码返回 400", r.status_code == 400, r.status_code)
    check("空账号返回 400", client.post("/api/auth/login", json={"password": "x"}).status_code == 400)

    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD})
    check("管理员登录 200（兼容 username 字段）", r.status_code == 200, r.get_json())
    login = r.get_json()
    check("自动识别为管理员身份", login.get("role") == "admin", login)
    check("管理员落点为后台控制台", login.get("redirect") == "/admin", login)
    check("登录返回管理员信息", login["admin"]["username"] == ADMIN_USER, login)
    check("登录返回 CSRF token", bool(login.get("csrf_token")), login)
    api.csrf = login["csrf_token"]

    r = client.get("/api/auth/me")
    check("登录后 /api/auth/me 可用", r.status_code == 200, r.status_code)
    check("/api/auth/me 回带 csrf", bool(r.get_json().get("csrf_token")), r.get_json())
    check(
        "后台回报仍在使用初始口令（横幅提示依据）",
        r.get_json()["security"]["default_admin_password"] is True,
        r.get_json().get("security"),
    )

    r = client.get("/admin")
    check("登录后可进入后台控制台", r.status_code == 200, r.status_code)
    check("后台页面含退出按钮", 'id="logoutBtn"' in r.get_data(as_text=True))

    r = client.get("/")
    check(
        "已登录管理员访问首页直接进后台",
        r.status_code == 302 and "/admin" in r.headers.get("Location", ""),
        r.headers.get("Location"),
    )

    print("\n=== 4B. 旧登录链接收敛到首页 ===")
    anon = app.test_client()
    for legacy in ("/login", "/reader/login"):
        r = anon.get(legacy)
        check(
            f"{legacy} 重定向到统一登录页",
            r.status_code == 302 and r.headers.get("Location", "").startswith("/"),
            r.headers.get("Location"),
        )
    r = anon.get("/login", query_string={"next": "/admin"})
    check("旧登录链接透传 next", "admin" in r.headers.get("Location", ""), r.headers.get("Location"))
    r = anon.get("/login", query_string={"next": "https://evil.example.com"})
    check("拒绝站外 next 参数", "evil" not in r.headers.get("Location", ""), r.headers.get("Location"))

    print("\n=== 5. CSRF 防护 ===")
    r = client.post("/api/books", json={"isbn": "9787115428028", "title": "x", "total_copies": 1})
    check("缺少 CSRF 头返回 403", r.status_code == 403, r.status_code)
    check("CSRF 失败有明确错误码", r.get_json()["error"]["code"] == "csrf_failed", r.get_json())
    r = client.put("/api/books/1", json={})
    check("PUT 缺少 CSRF 头返回 403", r.status_code == 403, r.status_code)
    r = client.post("/api/books/1/shelf", json={"total_copies": 0})
    check("上下架缺少 CSRF 头返回 403", r.status_code == 403, r.status_code)

    print("\n=== 6. 图书 CRUD 与校验 ===")
    book = {
        "isbn": "978-7-115-42802-8",
        "title": "Python 编程：从入门到实践",
        "author": "Eric Matthes",
        "publisher": "人民邮电出版社",
        "category": "计算机",
        "publish_year": 2016,
        "location": "A-01-03",
        "total_copies": 3,
    }
    r = api.post("/api/books", json=book)
    check("创建图书 201", r.status_code == 201, r.get_json())
    created = r.get_json()
    book_id = created["id"]
    check("ISBN 连字符被规整", created["isbn"] == "9787115428028", created["isbn"])
    check("初始可借数 = 总册数", created["available_copies"] == 3, created["available_copies"])

    check("重复 ISBN 返回 409", api.post("/api/books", json=book).status_code == 409)

    r = api.post("/api/books", json={"isbn": "9787115428028", "total_copies": 1})
    check("缺书名返回 400", r.status_code == 400, r.status_code)
    check("错误响应含字段定位", r.get_json()["error"]["details"]["field"] == "title", r.get_json())
    check("非法 ISBN 返回 400", api.post("/api/books", json={**book, "isbn": "123", "title": "短 ISBN"}).status_code == 400)

    r = api.put(f"/api/books/{book_id}", json={**book, "title": "Python 编程（第 3 版）"})
    check("更新图书 200", r.status_code == 200, r.get_json())
    check("书名已更新", r.get_json()["title"] == "Python 编程（第 3 版）", r.get_json())

    check("关键词搜索命中", api.get("/api/books", query_string={"keyword": "Python"}).get_json()["total"] == 1)
    check("分类过滤生效", api.get("/api/books", query_string={"category": "不存在分类"}).get_json()["total"] == 0)
    check("查询不存在图书返回 404", api.get("/api/books/99999").status_code == 404)

    print("\n=== 6B. 馆藏条码与图书简介 ===")
    check("新增图书自动分配条码", created["barcode"] == f"BK{book_id:07d}", created.get("barcode"))

    r = api.post(
        "/api/books",
        json={
            "isbn": "9787111111111",
            "title": "带简介与手工条码的书",
            "author": "某作者",
            "total_copies": 2,
            "barcode": "A-2026-001",
            "description": "这是用于端到端测试的图书简介，应该在门户与后台都能取到。",
        },
    )
    check("创建带条码与简介的图书 201", r.status_code == 201, r.get_json())
    tagged = r.get_json()
    tagged_id = tagged["id"]
    check("手工条码原样保存", tagged["barcode"] == "A-2026-001", tagged["barcode"])
    check("简介已保存", tagged["description"].startswith("这是用于端到端测试"), tagged["description"])
    check("条码自动分配互不重复", tagged["barcode"] != created["barcode"])

    check("重复条码返回 409", api.post("/api/books", json={"isbn": "9787111111112", "title": "撞码", "total_copies": 1, "barcode": "A-2026-001"}).status_code == 409)
    r = api.post("/api/books", json={"isbn": "9787111111113", "title": "非法条码", "total_copies": 1, "barcode": "带中文的条码"})
    check("非法条码返回 400", r.status_code == 400, r.status_code)
    check("非法条码定位到 barcode 字段", r.get_json()["error"]["details"]["field"] == "barcode", r.get_json())
    r = api.post("/api/books", json={"isbn": "9787111111114", "title": "超长简介", "total_copies": 1, "description": "简" * 1001})
    check("简介超长返回 400", r.status_code == 400, r.status_code)
    check("超长简介定位到 description", r.get_json()["error"]["details"]["field"] == "description", r.get_json())

    check("按条码检索命中图书", api.get("/api/books", query_string={"keyword": "A-2026-001"}).get_json()["total"] == 1)
    check("按自动条码检索命中", api.get("/api/books", query_string={"keyword": created["barcode"]}).get_json()["total"] == 1)

    r = api.put(f"/api/books/{tagged_id}", json={"isbn": "9787111111111", "title": "带简介与手工条码的书", "total_copies": 2, "barcode": "", "description": "简介已更新"})
    check("条码留空时沿用原值", r.status_code == 200 and r.get_json()["barcode"] == "A-2026-001", r.get_json())
    check("简介可更新", r.get_json()["description"] == "简介已更新", r.get_json())

    r = client.get("/api/public/search", query_string={"keyword": "A-2026-001"})
    public_items = r.get_json()["items"]
    check("门户可按条码检索", len(public_items) == 1, public_items)
    if public_items:
        check("门户结果含条码", public_items[0].get("barcode") == "A-2026-001", public_items[0].get("barcode"))
        check("门户结果含简介", public_items[0].get("description") == "简介已更新", public_items[0].get("description"))

    r = client.get("/api/public/overview")
    new_arrivals = r.get_json()["new_arrivals"]
    check("门户新书含条码与简介字段", bool(new_arrivals) and all(k in new_arrivals[0] for k in ("barcode", "description")), list(new_arrivals[0].keys()) if new_arrivals else [])

    print("\n=== 7. 读者 CRUD ===")
    member = {"card_no": "2026001", "name": "李思远", "phone": "13800001111", "member_type": "学生", "status": "active"}
    r = api.post("/api/members", json=member)
    check("创建读者 201", r.status_code == 201, r.get_json())
    member_id = r.get_json()["id"]
    check("默认可借上限生效", r.get_json()["max_borrow"] == 5, r.get_json()["max_borrow"])
    check("重复证号返回 409", api.post("/api/members", json=member).status_code == 409)
    check("非法邮箱返回 400", api.post("/api/members", json={**member, "card_no": "2026002", "email": "bad"}).status_code == 400)
    check("非法读者类型返回 400", api.post("/api/members", json={**member, "card_no": "2026002", "member_type": "外星人"}).status_code == 400)

    print("\n=== 8. 借书 / 续借 / 还书 ===")
    r = api.post("/api/loans/borrow", json={"book_id": book_id, "member_id": member_id})
    check("借书 201", r.status_code == 201, r.get_json())
    loan = r.get_json()
    loan_id = loan["id"]
    check("状态为在借", loan["status"] == "borrowed", loan["status"])
    check("可续借", loan["can_renew"] is True, loan)
    check("应还日期 = 借出 + 30 天", loan["due_at"][:10] == (timeutil.now() + timedelta(days=30)).strftime("%Y-%m-%d"), loan["due_at"])
    check("库存扣减为 2", api.get(f"/api/books/{book_id}").get_json()["available_copies"] == 2)
    check("借阅记录带馆藏条码", loan.get("book_barcode") == created["barcode"], loan.get("book_barcode"))
    check("借阅列表可按条码检索", api.get("/api/loans", query_string={"keyword": created["barcode"]}).get_json()["total"] >= 1)
    check("同一读者重复借同书返回 409", api.post("/api/loans/borrow", json={"book_id": book_id, "member_id": member_id}).status_code == 409)

    r = api.post(f"/api/loans/{loan_id}/renew")
    check("续借 200", r.status_code == 200, r.get_json())
    check("续借次数 +1", r.get_json()["renew_count"] == 1, r.get_json())
    check("应还日期顺延", r.get_json()["due_at"][:10] == (timeutil.now() + timedelta(days=60)).strftime("%Y-%m-%d"), r.get_json()["due_at"])
    api.post(f"/api/loans/{loan_id}/renew")
    check("超出续借上限返回 422", api.post(f"/api/loans/{loan_id}/renew").status_code == 422)

    r = api.post(f"/api/loans/{loan_id}/return")
    check("还书 200", r.status_code == 200, r.get_json())
    check("状态为已归还", r.get_json()["status"] == "returned", r.get_json()["status"])
    check("按期内归还无罚金", r.get_json()["fine"] == 0, r.get_json())
    check("库存恢复为 3", api.get(f"/api/books/{book_id}").get_json()["available_copies"] == 3)
    check("重复还书返回 409", api.post(f"/api/loans/{loan_id}/return").status_code == 409)

    print("\n=== 9. 逾期规则 ===")
    with app.app_context():
        past_due = timeutil.to_str(timeutil.now() - timedelta(days=10))
        past_borrow = timeutil.to_str(timeutil.now() - timedelta(days=40))
        db.execute(
            "INSERT INTO loans (book_id, member_id, borrowed_at, due_at, returned_at, renew_count, note) "
            "VALUES (?, ?, ?, ?, NULL, 0, '')",
            [book_id, member_id, past_borrow, past_due],
        )
        db.execute("UPDATE books SET available_copies = available_copies - 1 WHERE id = ?", [book_id])

    overdue_items = api.get("/api/loans", query_string={"status": "overdue"}).get_json()["items"]
    check("逾期筛选命中 1 条", len(overdue_items) == 1, overdue_items)
    check("逾期天数计算正确", overdue_items[0]["overdue_days"] == 10, overdue_items[0])
    check("罚金 = 10 × 0.5", overdue_items[0]["fine"] == 5.0, overdue_items[0]["fine"])
    check("逾期不可续借 422", api.post(f"/api/loans/{overdue_items[0]['id']}/renew").status_code == 422)
    check("有逾期时禁止借书 422", api.post("/api/loans/borrow", json={"book_id": book_id, "member_id": member_id}).status_code == 422)
    check("逾期还书结算罚金", api.post(f"/api/loans/{overdue_items[0]['id']}/return").get_json()["fine"] == 5.0)

    print("\n=== 10. 冻结读者 ===")
    r = api.put(f"/api/members/{member_id}", json={**member, "status": "frozen"})
    check("无在借时可冻结", r.status_code == 200, r.get_json())
    check("冻结读者不可借书 422", api.post("/api/loans/borrow", json={"book_id": book_id, "member_id": member_id}).status_code == 422)
    r = api.put(f"/api/members/{member_id}", json={**member, "status": "active"})
    check("恢复为正常", r.status_code == 200 and r.get_json()["status"] == "active", r.get_json())

    print("\n=== 11. 可借上限 ===")
    with app.app_context():
        ids = []
        for i in range(2, 8):
            ids.append(
                db.execute(
                    "INSERT INTO books (isbn, title, author, publisher, category, total_copies, available_copies) "
                    "VALUES (?, ?, '', '', '测试', 1, 1)",
                    [f"90000000000{i}", f"上限测试书 {i}"],
                )
            )
    borrowed_ok = sum(
        1 for bid in ids if api.post("/api/loans/borrow", json={"book_id": bid, "member_id": member_id}).status_code == 201
    )
    check("达到 5 册上限后拒绝 422", api.post("/api/loans/borrow", json={"book_id": ids[-1], "member_id": member_id}).status_code == 422)
    check("正好借到 5 册", borrowed_ok + 1 == 5 or borrowed_ok == 5, borrowed_ok)

    print("\n=== 12. 删除保护 ===")
    check("有在借图书不可删除 409", api.delete(f"/api/books/{book_id}").status_code == 409)
    check("有在借读者不可删除 409", api.delete(f"/api/members/{member_id}").status_code == 409)

    print("\n=== 12B. 图书下架闭环 ===")
    detail = api.get(f"/api/books/{book_id}").get_json()
    book_title = detail["title"]
    public_before = client.get("/api/public/overview").get_json()["stats"]
    check("在架图书 off_shelf=False", detail.get("off_shelf") is False, detail.get("off_shelf"))
    check("图书列表带 off_shelf 字段", "off_shelf" in api.get("/api/books").get_json()["items"][0])
    check(
        "下架前门户可检索到该书",
        client.get("/api/public/search", query_string={"keyword": book_title}).get_json()["total"] == 1,
    )

    r = api.post(f"/api/books/{book_id}/shelf", json={"total_copies": -1})
    check("总册数为负返回 400", r.status_code == 400, r.status_code)

    r = api.post(f"/api/books/{book_id}/shelf", json={"total_copies": 0})
    check("下架 200", r.status_code == 200, r.get_json())
    check("下架后总册数为 0", r.get_json()["total_copies"] == 0, r.get_json())
    check("下架后可借数为 0", r.get_json()["available_copies"] == 0, r.get_json())
    check("下架后 off_shelf=True", r.get_json()["off_shelf"] is True, r.get_json())

    check(
        "下架后门户检索不到",
        client.get("/api/public/search", query_string={"keyword": book_title}).get_json()["total"] == 0,
    )
    arrivals = client.get("/api/public/overview").get_json()["new_arrivals"]
    check("下架后不在门户新书榜", all(int(b["id"]) != book_id for b in arrivals), [b["id"] for b in arrivals])
    public_after = client.get("/api/public/overview").get_json()["stats"]
    check(
        "下架后门户藏书量减 1",
        int(public_after["total_titles"]) == int(public_before["total_titles"]) - 1,
        (public_before.get("total_titles"), public_after.get("total_titles")),
    )

    r = api.post("/api/loans/borrow", json={"book_id": book_id, "member_id": member_id})
    check("下架图书不可借出 422", r.status_code == 422, r.get_json())
    check("下架借书提示明确", "已下架" in r.get_json()["error"]["message"], r.get_json())

    check(
        "概览统计到已下架种数",
        api.get("/api/dashboard/overview").get_json()["books"]["off_shelf_titles"] >= 1,
    )

    r = api.post(f"/api/books/{ids[0]}/shelf", json={"total_copies": 0})
    check("有在借时下架被拒 409", r.status_code == 409, r.get_json())
    check("下架被拒提示含未归还", "未归还" in r.get_json()["error"]["message"], r.get_json())

    r = api.post(f"/api/books/{book_id}/shelf", json={"total_copies": 2})
    check("重新上架 200", r.status_code == 200, r.get_json())
    check("上架后总册数为 2", r.get_json()["total_copies"] == 2, r.get_json())
    check("上架后可借数恢复为 2", r.get_json()["available_copies"] == 2, r.get_json())
    check("上架后 off_shelf=False", r.get_json()["off_shelf"] is False, r.get_json())
    check(
        "上架后门户重新可见",
        client.get("/api/public/search", query_string={"keyword": book_title}).get_json()["total"] == 1,
    )
    check(
        "概览已下架种数归零",
        api.get("/api/dashboard/overview").get_json()["books"]["off_shelf_titles"] == 0,
    )
    check("查询不存在的图书下架 404", api.post("/api/books/99999/shelf", json={"total_copies": 1}).status_code == 404)
    check(
        "下架闭环后库存对账仍一致",
        db.check_consistency()["ok"],
        db.check_consistency()["issues"],
    )

    print("\n=== 13. 概览统计 ===")
    r = api.get("/api/dashboard/overview")
    check("概览 200", r.status_code == 200, r.status_code)
    data = r.get_json()
    for key in ("books", "members", "loans", "trend", "category_ranking", "hot_books", "recent_loans", "policy"):
        check(f"概览包含 {key}", key in data, list(data.keys()))
    check("趋势序列长度 14", len(data["trend"]) == 14, len(data["trend"]))
    check("在借数量已统计", data["loans"]["open_loans"] >= 5, data["loans"])

    print("\n=== 14. 分页与安全头 ===")
    r = api.get("/api/books", query_string={"page": 1, "page_size": 2})
    check("分页返回 2 条", len(r.get_json()["items"]) == 2)
    check("分页元信息正确", r.get_json()["page_size"] == 2 and r.get_json()["pages"] >= 1, r.get_json())
    check("含 X-Frame-Options", r.headers.get("X-Frame-Options") == "DENY", dict(r.headers))
    check("含请求 ID 回写", bool(r.headers.get("X-Request-ID")))
    check("后台接口禁止缓存", "no-store" in (r.headers.get("Cache-Control") or ""), r.headers.get("Cache-Control"))
    check("非法页码返回 400", api.get("/api/books", query_string={"page": 0}).status_code == 400)

    print("\n=== 15. 会话 Cookie 安全属性 ===")
    fresh = app.test_client()
    check("匿名访问门户不下发会话 Cookie", "Set-Cookie" not in fresh.get("/").headers, fresh.get("/").headers.get("Set-Cookie"))
    r = fresh.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD})
    set_cookie = r.headers.get("Set-Cookie") or ""
    check("登录后下发会话 Cookie", "library_session=" in set_cookie, set_cookie)
    check("Cookie 带 HttpOnly", "HttpOnly" in set_cookie, set_cookie)
    check("Cookie 带 SameSite=Lax", "SameSite=Lax" in set_cookie, set_cookie)
    check("非 HTTPS 环境不加 Secure（可配置）", "Secure" not in set_cookie, set_cookie)
    fresh.post("/api/auth/logout")
    check("登出后可重新登录", fresh.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD}).status_code == 200)

    print("\n=== 16. 修改密码与登出 ===")
    r = api.post("/api/auth/password", json={"old_password": "wrong", "new_password": "newpass123"})
    check("旧密码错误返回 400", r.status_code == 400, r.status_code)
    r = api.post("/api/auth/password", json={"old_password": ADMIN_PWD, "new_password": "newpass123"})
    check("修改密码成功", r.status_code == 200, r.get_json())
    check("改密后会话失效", client.get("/api/auth/me").status_code == 401, client.get("/api/auth/me").status_code)
    check("旧密码已失效", client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD}).status_code == 401)
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": "newpass123"})
    check("新密码可登录", r.status_code == 200, r.get_json())
    api.csrf = r.get_json()["csrf_token"]
    check(
        "改密后不再提示默认口令",
        client.get("/api/auth/me").get_json()["security"]["default_admin_password"] is False,
        client.get("/api/auth/me").get_json().get("security"),
    )
    check("登出接口可用", api.post("/api/auth/logout").status_code == 200)
    check("登出后接口不可用", client.get("/api/books").status_code == 401)

    print("\n=== 17. 登录限流 ===")
    auth_service.login_throttle.reset()
    codes = [client.post("/api/auth/login", json={"identifier": "attacker", "password": "x"}).status_code for _ in range(3)]
    check("连续失败累计计数", codes == [401, 401, 401], codes)
    r = client.post("/api/auth/login", json={"identifier": "attacker", "password": "x"})
    check("超过阈值后锁定返回 422", r.status_code == 422, r.status_code)
    check("锁定提示含剩余时间", "秒后重试" in r.get_json()["error"]["message"], r.get_json())
    auth_service.login_throttle.reset()

    # ---------------------------------------------------------------- 读者门户账号
    from library.reader import service as reader_service  # noqa: PLC0415

    print("\n=== 18. 读者账号开通与登录 ===")
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": "newpass123"})
    api.csrf = r.get_json()["csrf_token"]

    r = api.post(
        "/api/members",
        json={"card_no": "2026002", "name": "王梦琪", "phone": "13800002222", "member_type": "学生", "status": "active"},
    )
    check("创建读者返回初始密码（证号后 6 位）", r.status_code == 201 and r.get_json().get("initial_password") == "026002", r.get_json())
    reader_id = r.get_json()["id"]

    reader = app.test_client()
    check("未登录访问读者接口 401", reader.get("/api/reader/me").status_code == 401)
    check("未登录访问读者借阅 401", reader.get("/api/reader/loans").status_code == 401)
    r = reader.get("/reader")
    check(
        "未登录访问个人中心跳统一登录页",
        r.status_code == 302 and r.headers.get("Location", "").startswith("/?next="),
        r.headers.get("Location"),
    )

    r = reader.post("/api/reader/login", json={"account": "2026002", "password": "wrong"})
    check("读者密码错误 401", r.status_code == 401, r.status_code)
    check("错误信息不区分账号是否存在", r.get_json()["error"]["message"] == "账号或密码不正确", r.get_json())
    check("读者空账号 400", reader.post("/api/reader/login", json={"account": "", "password": "x"}).status_code == 400)

    r = reader.post("/api/reader/login", json={"account": "2026002", "password": "026002"})
    check("借书证号登录成功", r.status_code == 200, r.get_json())
    reader_csrf = r.get_json()["csrf_token"]
    check("读者登录返回本人信息", r.get_json()["member"]["card_no"] == "2026002", r.get_json())

    # 新建读者的口令是「证号后 6 位」，可离线猜解 —— 必须先改掉才能用其他功能
    check(
        "新建读者带「首次登录须改密」标记",
        r.get_json()["member"].get("must_change_password") is True,
        r.get_json()["member"],
    )
    check("未改初始密码时借阅接口被拦 403", reader.get("/api/reader/loans").status_code == 403)
    check(
        "拦截返回明确错误码",
        reader.get("/api/reader/loans").get_json()["error"]["code"] == "password_change_required",
    )
    check("未改密码时概览仍放行（页面要能加载出提示）", reader.get("/api/reader/me").status_code == 200)
    check("未改密码时消息中心仍放行", reader.get("/api/reader/notifications").status_code == 200)
    # 下面各节要验证正常的读者功能，先把这道闸门放过去；
    # 闸门本身的完整覆盖（写操作被拦、改密后即解除）在 §32。
    db.execute("UPDATE members SET must_change_password = 0 WHERE id = ?", [reader_id])
    check("读者登录不下发密码哈希", "password_hash" not in r.get_json()["member"], r.get_json()["member"])

    r = app.test_client().post("/api/reader/login", json={"account": "13800002222", "password": "026002"})
    check("手机号登录成功", r.status_code == 200, r.get_json())

    r = reader.get("/reader")
    check("登录后可进入个人中心", r.status_code == 200, r.status_code)
    check("个人中心页面含我的借阅", "我的借阅" in r.get_data(as_text=True))

    print("\n=== 18B. 统一入口识别读者身份 ===")
    check("管理员与读者共用同一限流器", reader_service.reader_throttle is auth_service.login_throttle)
    uni = app.test_client()
    r = uni.post("/api/auth/login", json={"identifier": "13800002222", "password": "026002"})
    check("统一入口用手机号登录成功", r.status_code == 200, r.get_json())
    body = r.get_json()
    check("自动识别为读者身份", body.get("role") == "reader", body)
    check("读者落点为个人中心", body.get("redirect") == "/reader", body)
    check("统一入口返回读者资料", body["member"]["card_no"] == "2026002", body)
    check("统一入口不下发密码哈希", "password_hash" not in body["member"], body["member"])
    check("读者会话可访问个人中心接口", uni.get("/api/reader/me").status_code == 200)
    check("读者会话不能访问后台接口", uni.get("/api/books").status_code == 401)
    r = uni.get("/")
    check(
        "已登录读者访问首页跳个人中心",
        r.status_code == 302 and "/reader" in r.headers.get("Location", ""),
        r.headers.get("Location"),
    )
    r = uni.get("/admin")
    check(
        "读者访问后台被拦回统一登录页",
        r.status_code == 302 and r.headers.get("Location", "").startswith("/?next="),
        r.headers.get("Location"),
    )

    print("\n=== 19. 管理员与读者身份隔离 ===")
    check("读者不能访问图书管理接口", reader.get("/api/books").status_code == 401)
    check("读者不能访问读者管理接口", reader.get("/api/members").status_code == 401)
    check("读者不能访问概览接口", reader.get("/api/dashboard/overview").status_code == 401)
    r = reader.get("/admin")
    check(
        "读者访问后台页面被拦回统一登录页",
        r.status_code == 302 and r.headers.get("Location", "").startswith("/?next="),
        r.headers.get("Location"),
    )
    check("管理员会话不能访问读者接口", client.get("/api/reader/me").status_code == 401)
    check("读者写操作缺 CSRF 返回 403", reader.post("/api/reader/logout").status_code == 403)
    check("读者登录接口本身免登录可访问", app.test_client().post("/api/reader/login", json={}).status_code == 400)

    print("\n=== 20. 我的借阅与自助续借 ===")
    r = api.post("/api/books", json={"isbn": "9787111123456", "title": "读者端测试书", "author": "测试作者", "category": "测试", "total_copies": 2})
    book2_id = r.get_json()["id"]
    loan2 = api.post("/api/loans/borrow", json={"book_id": book2_id, "member_id": reader_id}).get_json()

    me = reader.get("/api/reader/me").get_json()
    check("读者概览含本人资料", me["member"]["card_no"] == "2026002", me["member"])
    check("读者概览含在借统计", me["stats"]["active_loans"] == 1, me["stats"])
    check("读者概览含剩余可借额度", me["stats"]["quota_left"] == 4, me["stats"])
    check("读者概览含借阅规则", set(me["rules"]) >= {"loan_days", "max_renew", "fine_per_day"}, me["rules"])

    payload = reader.get("/api/reader/loans").get_json()
    check("只能看到自己的借阅记录", all(i["member_id"] == reader_id for i in payload["items"]) and payload["total"] == 1, payload)

    other_loan = api.get("/api/loans", query_string={"member_id": member_id, "page_size": 1}).get_json()["items"][0]
    r = reader.post(f"/api/reader/loans/{other_loan['id']}/renew", headers={"X-CSRF-Token": reader_csrf})
    check("越权续借他人记录 403", r.status_code == 403, r.status_code)
    check("越权被拒有明确错误码", r.get_json()["error"]["code"] == "forbidden", r.get_json())

    r = reader.post(f"/api/reader/loans/{loan2['id']}/renew", headers={"X-CSRF-Token": reader_csrf})
    check("自助续借成功", r.status_code == 200 and r.get_json()["renew_count"] == 1, r.get_json())
    check("续借后应还日期顺延", r.get_json()["due_at"][:10] == (timeutil.now() + timedelta(days=60)).strftime("%Y-%m-%d"), r.get_json()["due_at"])
    check("不存在的借阅记录 404", reader.post("/api/reader/loans/99999/renew", headers={"X-CSRF-Token": reader_csrf}).status_code == 404)
    check("读者可按状态筛选", reader.get("/api/reader/loans", query_string={"status": "open"}).get_json()["total"] == 1)
    check("读者非法状态 400", reader.get("/api/reader/loans", query_string={"status": "bogus"}).status_code == 400)

    print("\n=== 21. 读者资料与密码自助 ===")
    r = reader.put("/api/reader/profile", json={"phone": "13900001234", "email": "newwang@example.com"}, headers={"X-CSRF-Token": reader_csrf})
    check("修改联系方式成功", r.status_code == 200 and r.get_json()["phone"] == "13900001234", r.get_json())
    r = reader.put("/api/reader/profile", json={"phone": "13800001111"}, headers={"X-CSRF-Token": reader_csrf})
    check("手机号被他人占用 409", r.status_code == 409, r.get_json())
    check("占用冲突返回 conflict 错误码", r.get_json()["error"]["code"] == "conflict", r.get_json())
    check("非法邮箱 400", reader.put("/api/reader/profile", json={"email": "bad-email"}, headers={"X-CSRF-Token": reader_csrf}).status_code == 400)
    check("改手机号后可用新号登录", app.test_client().post("/api/reader/login", json={"account": "13900001234", "password": "026002"}).status_code == 200)

    r = reader.post("/api/reader/password", json={"old_password": "bad", "new_password": "reader666"}, headers={"X-CSRF-Token": reader_csrf})
    check("读者旧密码错误 400", r.status_code == 400, r.status_code)
    r = reader.post("/api/reader/password", json={"old_password": "026002", "new_password": "123"}, headers={"X-CSRF-Token": reader_csrf})
    check("读者新密码过短 400", r.status_code == 400, r.status_code)
    r = reader.post("/api/reader/password", json={"old_password": "026002", "new_password": "reader666"}, headers={"X-CSRF-Token": reader_csrf})
    check("读者修改密码成功", r.status_code == 200, r.get_json())
    check("改密后读者会话失效", reader.get("/api/reader/me").status_code == 401)
    check("读者新密码可登录", app.test_client().post("/api/reader/login", json={"account": "2026002", "password": "reader666"}).status_code == 200)
    check("读者旧密码已失效", app.test_client().post("/api/reader/login", json={"account": "2026002", "password": "026002"}).status_code == 401)

    print("\n=== 22. 管理员重置密码与读者登录限流 ===")
    r = api.post(f"/api/members/{reader_id}/reset-password")
    check("管理员重置读者密码", r.status_code == 200 and r.get_json()["initial_password"] == "026002", r.get_json())
    check("重置后初始密码可登录", app.test_client().post("/api/reader/login", json={"account": "2026002", "password": "026002"}).status_code == 200)
    check("重置后旧密码失效", app.test_client().post("/api/reader/login", json={"account": "2026002", "password": "reader666"}).status_code == 401)

    # 重置后的口令同样可猜解，所以「强制改密」必须重新武装
    probe = app.test_client()
    probe.post("/api/reader/login", json={"account": "2026002", "password": "026002"})
    check("重置密码会重新要求改密", probe.get("/api/reader/loans").status_code == 403)
    db.execute("UPDATE members SET must_change_password = 0 WHERE id = ?", [reader_id])

    reader_service.reader_throttle.reset()
    attacker = app.test_client()
    codes = [attacker.post("/api/reader/login", json={"account": "2026002", "password": "x"}).status_code for _ in range(3)]
    check("读者连续失败累计计数", codes == [401, 401, 401], codes)
    r = attacker.post("/api/reader/login", json={"account": "2026002", "password": "x"})
    check("读者超阈值锁定 422", r.status_code == 422, r.status_code)
    check("读者锁定提示含剩余时间", "秒后重试" in r.get_json()["error"]["message"], r.get_json())
    reader_service.reader_throttle.reset()

    print("\n=== 23. 冻结读者仍可查看借阅 ===")
    again = app.test_client()
    r = again.post("/api/reader/login", json={"account": "2026002", "password": "026002"})
    check("重置密码后可重新登录", r.status_code == 200, r.get_json())
    csrf2 = r.get_json()["csrf_token"]

    r = again.put(
        "/api/reader/profile",
        json={"phone": "13900001234", "email": "newwang@example.com"},
        headers={"X-CSRF-Token": csrf2},
    )
    check("读者可维护联系方式", r.status_code == 200 and r.get_json()["card_no"] == "2026002", r.get_json())
    r = again.put(
        "/api/reader/profile",
        json={"phone": "13900001234", "name": "黑客", "max_borrow": 99, "card_no": "9999999", "status": "active"},
        headers={"X-CSRF-Token": csrf2},
    )
    body = r.get_json()
    check("读者无法自行提权改姓名", body.get("name") == "王梦琪", body)
    check("读者无法自行提高可借上限", body.get("max_borrow") == 5, body)
    check("读者无法自行改证号", body.get("card_no") == "2026002", body)

    api.post(f"/api/loans/{loan2['id']}/return")
    r = api.put(
        f"/api/members/{reader_id}",
        json={"card_no": "2026002", "name": "王梦琪", "phone": "13900001234", "member_type": "学生", "status": "frozen"},
    )
    check("还清后可冻结", r.status_code == 200 and r.get_json()["status"] == "frozen", r.get_json())
    frozen = app.test_client()
    check("冻结读者仍可登录查看", frozen.post("/api/reader/login", json={"account": "2026002", "password": "026002"}).status_code == 200)
    me = frozen.get("/api/reader/me").get_json()
    check("概览如实显示已冻结", me["member"]["status"] == "frozen", me["member"])
    check("冻结读者历史记录仍可查", frozen.get("/api/reader/loans").get_json()["total"] >= 1)
    check("读者借阅记录带馆藏条码", all("book_barcode" in item for item in frozen.get("/api/reader/loans").get_json()["items"]))

    print("\n=== 23B. 并发写入与数据一致性（借阅写路径原子性） ===")
    import threading  # noqa: PLC0415 - 仅本段使用

    from library.loans import service as loan_service  # noqa: PLC0415

    def run_concurrently(fn, times):
        """让 times 个线程尽量同时执行 fn(slot)，返回 [(状态, 结果或异常)] 列表。"""

        barrier = threading.Barrier(times)
        results: list = [None] * times

        def worker(slot):
            db.get_conn()  # 先建好线程本地连接，避免并发初始化连接时争用
            try:
                barrier.wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
            try:
                results[slot] = ("ok", fn(slot))
            except Exception as exc:  # noqa: BLE001 - 测试需要观察任意异常
                results[slot] = ("err", exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(times)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    def succeeded(results):
        return [payload for status, payload in results if status == "ok"]

    def failures(results):
        return [str(payload) for status, payload in results if status == "err"]

    with app.app_context():
        con_a = db.execute(
            "INSERT INTO members (card_no, name, member_type, status, max_borrow) "
            "VALUES ('2099001', '并发读者甲', '学生', 'active', 1)"
        )
        con_b = db.execute(
            "INSERT INTO members (card_no, name, member_type, status, max_borrow) "
            "VALUES ('2099002', '并发读者乙', '学生', 'active', 5)"
        )
        con_c = db.execute(
            "INSERT INTO members (card_no, name, member_type, status, max_borrow) "
            "VALUES ('2099003', '并发读者丙', '学生', 'active', 5)"
        )
        con_d = db.execute(
            "INSERT INTO members (card_no, name, member_type, status, max_borrow) "
            "VALUES ('2099004', '并发读者丁', '学生', 'active', 5)"
        )
        con_books = [
            db.execute(
                "INSERT INTO books (isbn, title, category, total_copies, available_copies) "
                "VALUES (?, ?, '并发测试', 3, 3)",
                [f"99000000000{i:02d}", f"并发测试书 {i}"],
            )
            for i in range(1, 6)
        ]
    con_x, con_y, con_z, con_w, con_r = con_books

    # 1) 同一读者（上限 1 册）并发借两本不同的书 —— 只能成功一次
    results = run_concurrently(
        lambda slot: loan_service.borrow({"book_id": [con_x, con_y][slot], "member_id": con_a}), 2
    )
    check("并发借两本书只成功 1 次（不突破可借上限）", len(succeeded(results)) == 1, failures(results))
    with app.app_context():
        open_of_a = int(db.query_scalar("SELECT COUNT(*) FROM loans WHERE member_id = ? AND returned_at IS NULL", [con_a]))
    check("并发后该读者在借数不超过上限 1", open_of_a <= 1, open_of_a)

    # 2) 同一读者并发借同一本书 —— 只能成功一次，且只留一条在借记录
    results = run_concurrently(
        lambda slot: loan_service.borrow({"book_id": con_z, "member_id": con_b}), 6
    )
    check("并发借同一本书只成功 1 次", len(succeeded(results)) == 1, failures(results))
    with app.app_context():
        dup_open = int(
            db.query_scalar(
                "SELECT COUNT(*) FROM loans WHERE book_id = ? AND member_id = ? AND returned_at IS NULL",
                [con_z, con_b],
            )
        )
        stock_z = db.query_one("SELECT total_copies, available_copies FROM books WHERE id = ?", [con_z])
    check("同一读者同一本书只有一条在借记录", dup_open == 1, dup_open)
    check("并发借书后库存与借阅数对得上", stock_z["available_copies"] == stock_z["total_copies"] - 1, stock_z)

    # 3) 并发还同一笔借阅 —— 只能成功一次，库存只回补一册
    loan_c = loan_service.borrow({"book_id": con_w, "member_id": con_c})
    with app.app_context():
        stock_before = db.query_one("SELECT total_copies, available_copies FROM books WHERE id = ?", [con_w])
    results = run_concurrently(lambda slot: loan_service.return_book(loan_c["id"]), 6)
    check("并发还书只成功 1 次", len(succeeded(results)) == 1, failures(results))
    with app.app_context():
        stock_after = db.query_one("SELECT total_copies, available_copies FROM books WHERE id = ?", [con_w])
    check(
        "并发还书库存只回补一次",
        stock_after["available_copies"] == stock_before["available_copies"] + 1,
        (dict(stock_before), dict(stock_after)),
    )
    check("还书后可借数不超过总册数", stock_after["available_copies"] <= stock_after["total_copies"], stock_after)

    # 4) 并发续借同一笔借阅 —— 续借次数不得突破上限（配置为 2）
    loan_d = loan_service.borrow({"book_id": con_r, "member_id": con_d})
    results = run_concurrently(lambda slot: loan_service.renew(loan_d["id"]), 8)
    with app.app_context():
        renew_count = int(db.query_scalar("SELECT renew_count FROM loans WHERE id = ?", [loan_d["id"]]))
    check("并发续借后次数不超过上限 2", renew_count <= 2, renew_count)
    check("并发续借成功次数与记录一致", len(succeeded(results)) == renew_count, (len(succeeded(results)), renew_count))

    # 5) 全局数据一致性自检
    with app.app_context():
        report = db.check_consistency()
    check("库存一致性自检通过（区间 / 重复在借 / 总量对账）", report["ok"] is True, report["issues"])
    check(
        "不存在越界库存与重复在借",
        not any(i["type"] in ("inventory_out_of_range", "duplicate_open_loan") for i in report["issues"]),
        report["issues"],
    )
    check(
        "同一读者同一本书的唯一索引已建立",
        bool(
            db.query_all(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_loans_open_unique'"
            )
        ),
    )

    # 6) 数据库层兜底：CHECK 约束阻止可借数超过总册数
    import sqlite3 as _sqlite3  # noqa: PLC0415 - 仅本段使用

    with app.app_context():
        bounded = False
        try:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE books SET available_copies = total_copies + 1 WHERE id = ?", [con_x]
                )
        except _sqlite3.IntegrityError:
            bounded = True
        else:
            # 约束若失效，这里把数据夹回来，避免污染后续断言
            db.execute("UPDATE books SET available_copies = total_copies WHERE id = ?", [con_x])
    check("新库 CHECK 约束阻止可借数超过总册数", bounded)

    print("\n=== 23C. 会话密钥：自动生成、持久化与弱值拒绝 ===")
    import config as config_module  # noqa: PLC0415 - 仅本段使用
    import sqlite3 as _sqlite3  # noqa: PLC0415 - 仅本段使用

    key_dir = os.path.join(tmp_dir, "secrets")
    key_db = os.path.join(key_dir, "library.db")
    saved_env = {
        name: os.environ.get(name)
        for name in ("LIBRARY_SECRET_KEY", "LIBRARY_DEBUG", "LIBRARY_DB_PATH")
    }
    try:
        os.environ.pop("LIBRARY_SECRET_KEY", None)
        os.environ["LIBRARY_DEBUG"] = "false"
        os.environ["LIBRARY_DB_PATH"] = key_db

        generated = config_module.load_config().secret_key
        check(
            "未配置会话密钥时自动生成强随机密钥",
            len(generated) >= 32 and generated.lower() not in config_module._PLACEHOLDER_SECRETS,
            f"len={len(generated)}",
        )
        key_file = config_module.secret_file_path(key_db)
        check("密钥已落盘到 .secret_key", key_file.exists(), str(key_file))
        check("落盘内容与运行时密钥一致", key_file.read_text(encoding="utf-8").strip() == generated)
        check("再次加载沿用同一密钥（重启不掉登录）", config_module.load_config().secret_key == generated)

        os.environ["LIBRARY_SECRET_KEY"] = "please-change-me"
        check("生产模式拒绝示例会话密钥", config_rejected(config_module))
        os.environ["LIBRARY_SECRET_KEY"] = "short"
        check("生产模式拒绝过短会话密钥", config_rejected(config_module))
        os.environ["LIBRARY_DEBUG"] = "true"
        check("开发模式仍允许示例密钥（便于本地调试）", config_module.load_config().secret_key == "short")
    finally:
        for env_name, env_value in saved_env.items():
            if env_value is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = env_value

    print("\n=== 23D. 登录限流：伪造 X-Forwarded-For 换不了桶 ===")
    auth_service.login_throttle.reset()
    spoof = app.test_client()
    spoof_codes = [
        spoof.post(
            "/api/auth/login",
            json={"identifier": "xff-test", "password": "x"},
            headers={"X-Forwarded-For": f"10.0.0.{i}"},
        ).status_code
        for i in range(3)
    ]
    check("未受信来源下伪造 XFF 仍照常累计失败", spoof_codes == [401, 401, 401], spoof_codes)
    r = spoof.post(
        "/api/auth/login",
        json={"identifier": "xff-test", "password": "x"},
        headers={"X-Forwarded-For": "203.0.113.9"},
    )
    check("换一个伪造 XFF 也不能绕过锁定", r.status_code == 422, r.status_code)
    auth_service.login_throttle.reset()

    # 同一 IP 轮换账号名（密码喷洒）由 IP 维度桶兜底
    spray = app.test_client()
    spray_codes = [
        spray.post("/api/auth/login", json={"identifier": f"spray-{i}", "password": "x"}).status_code
        for i in range(24)
    ]
    check("零星手误不会触发 IP 桶", spray_codes[:10] == [401] * 10, spray_codes[:10])
    check("同一 IP 轮换账号名会被 IP 维度桶兜住", 422 in spray_codes, spray_codes[-5:])
    auth_service.login_throttle.reset()

    from dataclasses import replace as _dc_replace  # noqa: PLC0415 - 仅本段使用

    base_cfg = config_module.get_config()
    config_module.set_config(_dc_replace(base_cfg, trusted_proxies=("127.0.0.1/32",)))
    try:
        auth_service.login_throttle.reset()
        proxied = app.test_client()
        proxied_codes = [
            proxied.post(
                "/api/auth/login",
                json={"identifier": "proxy-test", "password": "x"},
                headers={"X-Forwarded-For": f"198.51.100.{i}"},
            ).status_code
            for i in range(3)
        ]
        check("受信代理下按 XFF 分桶计数", proxied_codes == [401, 401, 401], proxied_codes)
        r = proxied.post(
            "/api/auth/login",
            json={"identifier": "proxy-test", "password": "x"},
            headers={"X-Forwarded-For": "198.51.100.99"},
        )
        check("受信代理下来自新 IP 的尝试不被误锁", r.status_code == 401, r.status_code)
        same_ip = {"X-Forwarded-For": "198.51.100.0"}
        repeat_codes = [
            proxied.post(
                "/api/auth/login", json={"identifier": "proxy-test", "password": "x"}, headers=same_ip
            ).status_code
            for _ in range(2)
        ]
        check("受信代理下同一 XFF 继续累计", repeat_codes == [401, 401], repeat_codes)
        r = proxied.post(
            "/api/auth/login", json={"identifier": "proxy-test", "password": "x"}, headers=same_ip
        )
        check("受信代理下同一 XFF 达到阈值即锁定", r.status_code == 422, r.status_code)
    finally:
        config_module.set_config(base_cfg)
        auth_service.login_throttle.reset()

    print("\n=== 23E. 登录标识唯一：手机号不得撞证号 / 管理员工号 ===")
    sec_admin = app.test_client()
    r = sec_admin.post("/api/auth/login", json={"identifier": ADMIN_USER, "password": "newpass123"})
    check("安全段：管理员登录", r.status_code == 200, r.get_json())
    adm = Api(sec_admin)
    adm.csrf = r.get_json()["csrf_token"]

    r = adm.post(
        "/api/members",
        json={"card_no": "8808001", "name": "安全甲", "phone": "13100008001", "member_type": "学生", "status": "active"},
    )
    check("创建读者甲", r.status_code == 201, r.get_json())
    r = adm.post(
        "/api/members",
        json={"card_no": "8808002", "name": "安全乙", "phone": "13100008002", "member_type": "学生", "status": "active"},
    )
    check("创建读者乙", r.status_code == 201, r.get_json())
    member_b = r.get_json()

    r = adm.post(
        "/api/members",
        json={"card_no": "8808003", "name": "安全丙", "phone": "8808001", "member_type": "学生", "status": "active"},
    )
    check("手机号撞他人证号被拒 409", r.status_code == 409, r.get_json())
    r = adm.post(
        "/api/members",
        json={"card_no": "8808004", "name": "安全丁", "phone": "13100008001", "member_type": "学生", "status": "active"},
    )
    check("手机号与他人重复被拒 409", r.status_code == 409, r.get_json())
    r = adm.post(
        "/api/members",
        json={"card_no": "13100008002", "name": "安全戊", "phone": "13100008999", "member_type": "学生", "status": "active"},
    )
    check("证号撞他人手机号被拒 409", r.status_code == 409, r.get_json())
    r = adm.post(
        "/api/members",
        json={"card_no": "8808005", "name": "安全己", "phone": ADMIN_USER, "member_type": "学生", "status": "active"},
    )
    check("手机号撞管理员工号被拒 409", r.status_code == 409, r.get_json())
    r = adm.post(
        "/api/members",
        json={"card_no": ADMIN_USER, "name": "安全庚", "phone": "13100008998", "member_type": "学生", "status": "active"},
    )
    check("证号撞管理员工号被拒 409", r.status_code == 409, r.get_json())
    r = adm.put(
        f"/api/members/{member_b['id']}",
        json={"card_no": "8808002", "name": "安全乙", "phone": "8808001", "member_type": "学生", "status": "active"},
    )
    check("管理员改档时手机号撞他人证号被拒 409", r.status_code == 409, r.get_json())

    sec_reader = app.test_client()
    r = sec_reader.post("/api/reader/login", json={"account": "8808002", "password": "808002"})
    check("安全段：读者乙登录", r.status_code == 200, r.get_json())
    sec_csrf = r.get_json()["csrf_token"]
    # 本段验证登录标识唯一性，不是强制改密，先把闸门放过去
    db.execute("UPDATE members SET must_change_password = 0 WHERE card_no IN ('8808001', '8808002', '8808003')")
    r = sec_reader.put("/api/reader/profile", json={"phone": "8808001"}, headers={"X-CSRF-Token": sec_csrf})
    check("读者自助改手机号撞他人证号被拒 409", r.status_code == 409, r.get_json())
    r = sec_reader.put("/api/reader/profile", json={"phone": "13100008001"}, headers={"X-CSRF-Token": sec_csrf})
    check("读者自助改手机号与他人重复被拒 409", r.status_code == 409, r.get_json())
    r = sec_reader.put("/api/reader/profile", json={"phone": "13100009999"}, headers={"X-CSRF-Token": sec_csrf})
    check(
        "读者改到未被占用的手机号成功",
        r.status_code == 200 and r.get_json()["phone"] == "13100009999",
        r.get_json(),
    )

    check(
        "手机号唯一索引已建立",
        bool(db.query_all("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_members_phone_unique'")),
    )
    blocked = False
    try:
        db.execute("UPDATE members SET phone = ? WHERE id = ?", ["13100008001", member_b["id"]])
    except _sqlite3.IntegrityError:
        blocked = True
    check("数据库层唯一索引兜住重复手机号", blocked)

    # 自检能力：临时构造脏数据验证 check_consistency() 能识别（随后立即还原）
    db.execute("DROP INDEX IF EXISTS idx_members_phone_unique")
    db.execute("UPDATE members SET phone = ? WHERE id = ?", ["13100008001", member_b["id"]])
    dup_report = db.check_consistency()
    check(
        "自检能发现重复手机号",
        any(issue["type"] == "duplicate_member_phone" for issue in dup_report["issues"]),
        dup_report["issues"],
    )
    db.execute("UPDATE members SET phone = ? WHERE id = ?", ["8808001", member_b["id"]])
    shadow_report = db.check_consistency()
    check(
        "自检能发现手机号抢占他人证号",
        any(issue["type"] == "phone_shadows_card_no" for issue in shadow_report["issues"]),
        shadow_report["issues"],
    )
    db.execute("UPDATE members SET phone = ? WHERE id = ?", ["13100009999", member_b["id"]])
    db.init_db()
    check(
        "还原后唯一索引已重建",
        bool(db.query_all("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_members_phone_unique'")),
    )
    final_report = db.check_consistency()
    check(
        "无重复手机号与身份遮蔽",
        not [
            issue
            for issue in final_report["issues"]
            if issue["type"]
            in {"duplicate_member_phone", "phone_shadows_card_no", "member_identifier_shadows_admin"}
        ],
        final_report["issues"],
    )

    print("\n=== 23F. 重复在借的体检与修复 ===")
    probe_book = db.execute(
        "INSERT INTO books (isbn, title, category, total_copies, available_copies) "
        "VALUES ('8801234567890', '重复在借探针', '测试', 2, 0)"
    )
    probe_member = db.execute(
        "INSERT INTO members (card_no, name, member_type, status, max_borrow) "
        "VALUES ('8808009', '重复在借读者', '学生', 'active', 5)"
    )
    db.execute("DROP INDEX IF EXISTS idx_loans_open_unique")
    near_due = timeutil.to_str(timeutil.now())
    first_loan = db.execute(
        "INSERT INTO loans (book_id, member_id, borrowed_at, due_at) VALUES (?, ?, ?, ?)",
        [probe_book, probe_member, near_due, near_due],
    )
    second_loan = db.execute(
        "INSERT INTO loans (book_id, member_id, borrowed_at, due_at) VALUES (?, ?, ?, ?)",
        [probe_book, probe_member, near_due, near_due],
    )

    dirty = db.check_consistency()
    check(
        "体检能发现重复在借",
        any(
            issue["type"] == "duplicate_open_loan" and issue["book_id"] == probe_book
            for issue in dirty["issues"]
        ),
        dirty["issues"],
    )
    check("存在问题时体检结论为不通过", dirty["ok"] is False)

    fixed = db.repair_consistency()
    check("修复 1 条重复在借", fixed["closed_loans"] == 1, fixed["closed_loans"])
    check("修复后体检通过", fixed["after"]["ok"] is True, fixed["after"]["issues"])
    survivors = db.query_all(
        "SELECT id FROM loans WHERE book_id = ? AND member_id = ? AND returned_at IS NULL",
        [probe_book, probe_member],
    )
    check("保留最早的一条在借记录", [row["id"] for row in survivors] == [first_loan], survivors)
    closed_row = db.query_one("SELECT returned_at FROM loans WHERE id = ?", [second_loan])
    check("多余记录被标记为已归还", closed_row["returned_at"] is not None, closed_row)
    check(
        "库存同步补回 1 册",
        db.query_scalar("SELECT available_copies FROM books WHERE id = ?", [probe_book]) == 1,
    )
    check(
        "修复后重复在借唯一索引已补建",
        bool(db.query_all("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_loans_open_unique'")),
    )
    again_fixed = db.repair_consistency()
    check(
        "重复修复幂等（无事可做也不报错）",
        again_fixed["closed_loans"] == 0 and again_fixed["after"]["ok"] is True,
        again_fixed,
    )

    print("\n=== 24. 老库升级：条码与简介自动补齐 ===")
    import sqlite3  # noqa: PLC0415 - 仅本段使用

    from library import barcode as barcode_util  # noqa: PLC0415
    from library import seed as seed_module  # noqa: PLC0415

    legacy_path = os.path.join(tmp_dir, "legacy.db")
    raw = sqlite3.connect(legacy_path)
    raw.executescript(
        """
        CREATE TABLE books (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            isbn TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            author TEXT NOT NULL DEFAULT '',
            publisher TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '未分类',
            publish_year INTEGER,
            location TEXT NOT NULL DEFAULT '',
            total_copies INTEGER NOT NULL DEFAULT 1,
            available_copies INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        INSERT INTO books (isbn, title, author, category) VALUES ('9787506365437', '活着', '余华', '文学');
        INSERT INTO books (isbn, title, author, category) VALUES ('9787111111111', '库内自有书目', '某作者', '计算机');
        INSERT INTO books (isbn, title, author, category, total_copies, available_copies)
        VALUES ('9787000000001', '库存越界存量书', '某作者', '测试', 2, 5);
        """
    )
    raw.commit()
    raw.close()

    legacy_app = build_app(legacy_path)  # create_app 内含 init_db：自动迁移
    with legacy_app.app_context():
        legacy_rows = db.query_all("SELECT id, isbn, barcode, description FROM books ORDER BY id")
        check("迁移后新列可用", all("barcode" in row and "description" in row for row in legacy_rows), legacy_rows)
        check("存量图书补上条码", all(row["barcode"] == barcode_util.generate(row["id"]) for row in legacy_rows), legacy_rows)
        check(
            "条码唯一索引已创建",
            bool(db.query_all("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_books_barcode'")),
        )
        repaired = db.query_one(
            "SELECT total_copies, available_copies FROM books WHERE isbn = '9787000000001'"
        )
        check(
            "老库越界库存被自动夹回总册数",
            repaired["available_copies"] == repaired["total_copies"],
            repaired,
        )
        after = {row["isbn"]: row for row in db.query_all("SELECT isbn, barcode, description FROM books")}
        check("启动即自动回填演示简介", len(after["9787506365437"]["description"]) > 20, after["9787506365437"])
        check("非演示书保持空简介", after["9787111111111"]["description"] == "", after["9787111111111"])
        check("回填幂等（已有简介不重复写）", seed_module.ensure_content() == 0)
        db.init_db()  # 再次初始化不应改变既有条码
        check(
            "重复初始化条码保持稳定",
            [row["barcode"] for row in db.query_all("SELECT barcode FROM books ORDER BY id")]
            == [row["barcode"] for row in legacy_rows],
        )

    print("\n=== 25. 演示数据生成器：不再造出重复在借 ===")
    seeded_path = os.path.join(tmp_dir, "seeded.db")
    seeded_app = build_app(seeded_path)
    with seeded_app.app_context():
        check("演示数据写入成功", seed_module.seed(force=True) is True)
        seeded_report = db.check_consistency()
        check(
            "演示库体检通过（库存对账一致、无重复在借）",
            seeded_report["ok"],
            seeded_report["issues"],
        )
        dup_pairs = db.query_all(
            "SELECT book_id, member_id, COUNT(*) AS n FROM loans WHERE returned_at IS NULL "
            "GROUP BY book_id, member_id HAVING n > 1"
        )
        check("演示数据无「同一读者同一本书」重复在借", not dup_pairs, dup_pairs)
        check(
            "演示库已建立重复在借唯一索引",
            bool(db.query_all("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_loans_open_unique'")),
        )

    print("\n=== 26. 罚金封顶与逾期口径统一 ===")
    from library import fine as fine_module  # noqa: PLC0415 - 仅本段使用
    from library.auth.passwords import hash_password  # noqa: PLC0415 - 仅本段使用

    cfg_now = get_config()
    per_day = cfg_now.fine_per_day
    cap = cfg_now.fine_cap
    long_days, mid_days = 200, 10
    cap_amount = round(cap, 2)
    raw_long = round(long_days * per_day, 2)
    check(
        "前提：默认配置下 200 天罚金会被封顶截断",
        cap > 0 and raw_long > cap_amount,
        f"cap={cap}, raw={raw_long}",
    )

    with app.app_context():
        probe_member = db.execute(
            "INSERT INTO members (card_no, name, member_type, status, max_borrow) "
            "VALUES ('FINE0001', '罚金探针', '学生', 'active', 5)"
        )
        today_midnight = timeutil.now().replace(hour=0, minute=0, second=0, microsecond=0)
        plan = [
            ("long", timeutil.to_str(timeutil.now() - timedelta(days=long_days))),
            ("mid", timeutil.to_str(timeutil.now() - timedelta(days=mid_days))),
            # 应还「今天」且时刻已过：旧口径会判成「逾期 0 天 / 罚金 ¥0」，新口径应判在借
            ("today", timeutil.to_str(today_midnight)),
        ]
        for index, (tag, due_at) in enumerate(plan):
            probe_book = db.execute(
                "INSERT INTO books (isbn, title, category, total_copies, available_copies) "
                "VALUES (?, ?, '口径测试', 1, 0)",
                [f"9787111999{index:03d}2", f"口径探针 {tag}"],
            )
            db.execute(
                "INSERT INTO loans (book_id, member_id, borrowed_at, due_at, returned_at, renew_count, note) "
                "VALUES (?, ?, ?, ?, NULL, 0, ?)",
                [
                    probe_book,
                    probe_member,
                    timeutil.to_str(timeutil.now() - timedelta(days=300)),
                    due_at,
                    f"probe-{tag}",
                ],
            )

    probe_items = api.get(
        "/api/loans", query_string={"member_id": probe_member, "page_size": 100}
    ).get_json()["items"]
    by_note = {item["note"]: item for item in probe_items}

    long_item = by_note.get("probe-long") or {}
    check("逾期 200 天罚金按上限封顶", abs(float(long_item.get("fine", -1)) - cap_amount) < 1e-6, long_item)
    check("封顶记录带 fine_capped 标记", long_item.get("fine_capped") is True, long_item)
    check("封顶不改变真实逾期天数展示", long_item.get("overdue_days") == long_days, long_item)

    mid_item = by_note.get("probe-mid") or {}
    check(
        "未达上限按实际天数计费",
        abs(float(mid_item.get("fine", -1)) - round(mid_days * per_day, 2)) < 1e-6,
        mid_item,
    )
    check("未封顶不带标记", mid_item.get("fine_capped") is False, mid_item)

    today_item = by_note.get("probe-today") or {}
    check("应还当天不算逾期（整日口径）", today_item.get("status") == "borrowed", today_item)
    check(
        "应还当天逾期天数与罚金均为 0",
        today_item.get("overdue_days") == 0 and today_item.get("fine") == 0,
        today_item,
    )
    check(
        "不再出现「状态=逾期 但 天数=0」的矛盾记录",
        not (today_item.get("status") == "overdue" and today_item.get("overdue_days") == 0),
        today_item,
    )

    probe_overdue = api.get(
        "/api/loans", query_string={"member_id": probe_member, "status": "overdue", "page_size": 100}
    ).get_json()
    overdue_notes = {item["note"] for item in probe_overdue["items"]}
    check("逾期筛选命中封顶与普通逾期两笔", probe_overdue["total"] == 2, probe_overdue["total"])
    check("逾期筛选排除「应还当天」", "probe-today" not in overdue_notes, sorted(overdue_notes))
    check(
        "逾期筛选内每条天数都 ≥ 1（口径自洽）",
        all(item["overdue_days"] >= 1 for item in probe_overdue["items"]),
        probe_overdue["items"],
    )

    overview = api.get("/api/dashboard/overview").get_json()
    global_overdue = api.get("/api/loans", query_string={"status": "overdue", "page_size": 1}).get_json()["total"]
    check(
        "概览「逾期未还」与借阅逾期筛选一致（跨层同口径）",
        int(overview["loans"]["overdue_loans"]) == global_overdue,
        (overview["loans"]["overdue_loans"], global_overdue),
    )
    check(
        "概览暴露罚金封顶策略",
        "fine_cap" in overview["policy"] and "fine_max_days" in overview["policy"],
        overview["policy"],
    )
    check("门户规则暴露罚金封顶", "fine_cap" in client.get("/api/public/overview").get_json()["rules"])

    # 读者中心的汇总必须与列表逐笔相加完全一致（过去三处各算各的）
    with app.app_context():
        db.execute("UPDATE members SET password_hash = ? WHERE id = ?", [hash_password("probe123"), probe_member])
    reader_client = app.test_client()
    reader_login = reader_client.post(
        "/api/reader/login", json={"account": "FINE0001", "password": "probe123"}
    )
    check("探针读者可登录读者中心", reader_login.status_code == 200, reader_login.get_json())
    reader_me = reader_client.get("/api/reader/me").get_json()
    expected_fine = round(sum(float(item["fine"]) for item in probe_items), 2)
    check(
        "读者中心累计罚金 = 列表逐笔罚金之和",
        abs(reader_me["stats"]["fine_total"] - expected_fine) < 1e-6,
        (reader_me["stats"]["fine_total"], expected_fine),
    )
    check("读者中心逾期册数与逾期筛选一致", reader_me["stats"]["overdue_loans"] == 2, reader_me["stats"])

    with app.app_context():
        check("fine：200 天取金额封顶", fine_module.fine_for_days(long_days) == cap_amount, fine_module.fine_for_days(long_days))
        check("fine：cap=0 表示不封顶", fine_module.fine_for_days(long_days, cap=0) == raw_long, fine_module.fine_for_days(long_days, cap=0))
        check(
            "fine：max_days 限制最长计息天数",
            fine_module.fine_for_days(400, max_days=100, cap=0) == round(100 * per_day, 2),
            fine_module.fine_for_days(400, max_days=100, cap=0),
        )
        check("fine：0 天不产生罚金", fine_module.fine_for_days(0) == 0)
        check(
            "fine：is_overdue 与 overdue_days 恒等价",
            all(
                fine_module.is_overdue(d) == (fine_module.overdue_days(d) > 0)
                for d in ("2020-01-01 00:00:00", "2999-01-01 00:00:00")
            ),
        )

    print("\n=== 26B. 罚金配置校验 ===")
    import config as fine_config  # noqa: PLC0415 - 仅本段使用

    saved_fine_env = {
        name: os.environ.get(name)
        for name in (
            "LIBRARY_FINE_CAP",
            "LIBRARY_FINE_MAX_DAYS",
            "LIBRARY_SECRET_KEY",
            "LIBRARY_DB_PATH",
            "LIBRARY_DEBUG",
        )
    }
    try:
        os.environ["LIBRARY_SECRET_KEY"] = "a-sufficiently-long-secret-key"
        os.environ.pop("LIBRARY_DB_PATH", None)
        os.environ["LIBRARY_DEBUG"] = "false"
        os.environ["LIBRARY_FINE_CAP"] = "-1"
        check("负数罚金上限被拒绝启动", config_rejected(fine_config))
        os.environ["LIBRARY_FINE_CAP"] = "50"
        os.environ["LIBRARY_FINE_MAX_DAYS"] = "99999"
        check("超范围的最长计息天数被拒绝启动", config_rejected(fine_config))
        os.environ["LIBRARY_FINE_MAX_DAYS"] = "0"
        check("max_days=0 表示不限制且能正常启动", fine_config.load_config().fine_max_days == 0)
        os.environ["LIBRARY_FINE_CAP"] = "0"
        check("cap=0 表示不封顶且能正常启动", fine_config.load_config().fine_cap == 0.0)
    finally:
        for env_name, env_value in saved_fine_env.items():
            if env_value is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = env_value

    print("\n=== 27. 批量还书 ===")
    r = api.post("/api/members", json={"card_no": "2026999", "name": "批量读者", "member_type": "学生", "status": "active"})
    check("批量用例：创建读者 201", r.status_code == 201, r.get_json())
    batch_member_id = r.get_json()["id"]

    batch_book_ids = []
    for i in range(3):
        r = api.post(
            "/api/books",
            json={"isbn": f"9780000000{i:03d}", "title": f"批量测试书 {i + 1}", "author": "测试", "total_copies": 2},
        )
        batch_book_ids.append(r.get_json()["id"])

    loan_ids = [
        api.post("/api/loans/borrow", json={"book_id": bid, "member_id": batch_member_id}).get_json()["id"]
        for bid in batch_book_ids
    ]
    check("批量用例：准备 3 条未归还借阅", len(loan_ids) == 3 and all(loan_ids), loan_ids)

    # 1) 全部成功
    r = api.post("/api/loans/return-batch", json={"ids": loan_ids})
    check("批量还书 200", r.status_code == 200, r.get_json())
    data = r.get_json()
    check("3 册全部成功", data["succeeded_count"] == 3 and data["failed_count"] == 0, data)
    check("逐条返回结果", len(data["succeeded"]) == 3 and len(data["failed"]) == 0, data)
    check("逐条状态均为已归还", all(item["status"] == "returned" for item in data["succeeded"]), data["succeeded"])
    check("库存全部回补为 2", all(api.get(f"/api/books/{bid}").get_json()["available_copies"] == 2 for bid in batch_book_ids))

    # 2) 幂等：重复提交同一批
    r = api.post("/api/loans/return-batch", json={"ids": loan_ids})
    check("重复批量还书不报错（200）", r.status_code == 200, r.get_json())
    check(
        "重复批量还书全部列为未处理",
        r.get_json()["succeeded_count"] == 0 and r.get_json()["failed_count"] == 3,
        r.get_json(),
    )
    check("重复批量还书库存不再变化", all(api.get(f"/api/books/{bid}").get_json()["available_copies"] == 2 for bid in batch_book_ids))

    # 3) 重复 ID 去重 + 混合提交（部分失败不影响其余）
    fresh = api.post("/api/loans/borrow", json={"book_id": batch_book_ids[0], "member_id": batch_member_id}).get_json()
    dup = api.post("/api/loans/return-batch", json={"ids": [fresh["id"], fresh["id"], fresh["id"]]}).get_json()
    check("重复 ID 去重后只处理一次", dup["requested"] == 1 and dup["succeeded_count"] == 1, dup)

    opened = api.post("/api/loans/borrow", json={"book_id": batch_book_ids[1], "member_id": batch_member_id}).get_json()
    mixed = api.post("/api/loans/return-batch", json={"ids": [fresh["id"], opened["id"]]}).get_json()
    check("混合提交：成功 1 / 未处理 1", mixed["succeeded_count"] == 1 and mixed["failed_count"] == 1, mixed)
    check("未处理项带失败原因", bool(mixed["failed"][0].get("message")), mixed["failed"])

    # 4) 参数校验
    check("空 ids 返回 400", api.post("/api/loans/return-batch", json={"ids": []}).status_code == 400)
    check("ids 非数组返回 400", api.post("/api/loans/return-batch", json={"ids": "1"}).status_code == 400)
    check("ids 含非正整数返回 400", api.post("/api/loans/return-batch", json={"ids": [-1]}).status_code == 400)
    check("超过单次上限返回 400", api.post("/api/loans/return-batch", json={"ids": list(range(1, 203))}).status_code == 400)
    check("不存在的 ID 计入未处理", api.post("/api/loans/return-batch", json={"ids": [999999]}).get_json()["failed_count"] == 1)

    # 5) 罚款合计 = 逐笔之和（逾期记录批量归还）
    with app.app_context():
        overdue_ids = []
        for bid in batch_book_ids[1:]:
            db.execute(
                "INSERT INTO loans (book_id, member_id, borrowed_at, due_at, returned_at, renew_count, note) "
                "VALUES (?, ?, ?, ?, NULL, 0, '')",
                [
                    bid,
                    batch_member_id,
                    timeutil.to_str(timeutil.now() - timedelta(days=40)),
                    timeutil.to_str(timeutil.now() - timedelta(days=10)),
                ],
            )
            db.execute("UPDATE books SET available_copies = available_copies - 1 WHERE id = ?", [bid])
            overdue_ids.append(int(db.query_scalar("SELECT last_insert_rowid()")))
    batch_overdue = api.post("/api/loans/return-batch", json={"ids": overdue_ids}).get_json()
    expected_fine = round(sum(float(x["fine"]) for x in batch_overdue["succeeded"]), 2)
    check(
        "批量罚金合计 = 逐笔罚金之和",
        abs(batch_overdue["fine_total"] - expected_fine) < 1e-6,
        (batch_overdue["fine_total"], expected_fine),
    )
    check("2 笔逾期 10 天罚金合计 ¥10", batch_overdue["fine_total"] == 10.0, batch_overdue["fine_total"])

    # 6) 鉴权与 CSRF（client 已登录；anon_client 全新会话）
    anon_client = app.test_client()
    check("未登录批量还书返回 401", anon_client.post("/api/loans/return-batch", json={"ids": [1]}).status_code == 401)
    check("已登录但缺 CSRF 头返回 403", client.post("/api/loans/return-batch", json={"ids": [1]}).status_code == 403)

    print("\n=== 27B. 前端资源契约（批量还书 / 路由持久化 / 加载态） ===")
    root = Path(__file__).resolve().parent.parent
    html = (root / "templates" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "js" / "app.js").read_text(encoding="utf-8")
    css = (root / "static" / "css" / "app.css").read_text(encoding="utf-8")
    check("借阅工具栏含批量还书按钮", 'id="loanReturnBatch"' in html)
    check("前端调用批量还书端点", "/api/loans/return-batch" in js)
    check("批量还书入口已绑定", "openBatchReturn" in js)
    check("监听 hashchange（前进/后退可切视图）", 'addEventListener("hashchange"' in js)
    check("筛选条件持久化到 URL", "history.replaceState" in js)
    check("空列表提供清除筛选出口", "data-clear" in js and "emptyState" in js)
    check("表单报错聚焦出错字段", "scrollIntoView" in js)
    check("样式含加载态与勾选列", "is-loading" in css and "th.pick" in css)

    print("\n=== 28. 列表排序、续借口径与错误细分 ===")

    # ---- 28.1 列表排序：白名单生效、非法值回落、方向兜底 ----
    def page_items(path, **params):
        payload = api.get(path, query_string={"page_size": 50, **params}).get_json()
        return payload["items"]

    titles_asc = [b["title"] for b in page_items("/api/books", sort="title", order="asc")]
    titles_desc = [b["title"] for b in page_items("/api/books", sort="title", order="desc")]
    check("图书按书名升序生效", len(titles_asc) > 1 and titles_asc == sorted(titles_asc, key=str.lower), titles_asc[:3])
    check("图书按书名降序 = 升序的反向", titles_desc == list(reversed(titles_asc)), titles_desc[:3])

    stock_asc = [int(b["available_copies"]) for b in page_items("/api/books", sort="available_copies", order="asc")]
    check("图书按可借数升序生效", stock_asc == sorted(stock_asc), stock_asc[:5])

    r = api.get("/api/books", query_string={"sort": "id); DROP TABLE books; --", "order": "asc"})
    check("非法排序键被白名单拒绝且不报错", r.status_code == 200 and r.get_json()["total"] > 0, r.status_code)
    r = api.get("/api/books", query_string={"sort": "title", "order": "'; DROP TABLE books; --"})
    check("非法排序方向回落为降序", r.get_json()["items"][0]["title"] == titles_desc[0], r.get_json()["items"][0]["title"])

    names_asc = [m["name"] for m in page_items("/api/members", sort="name", order="asc")]
    check("读者按姓名升序生效", names_asc == sorted(names_asc, key=str.lower), names_asc[:3])
    names_desc = [m["name"] for m in page_items("/api/members", sort="name", order="desc")]
    check("读者按姓名降序生效", names_desc == list(reversed(names_asc)), names_desc[:3])

    dues_asc = [row["due_at"] for row in page_items("/api/loans", sort="due_at", order="asc", status="open")]
    check("借阅按应还日期升序生效", dues_asc == sorted(dues_asc), dues_asc[:3])
    default_items = page_items("/api/loans", status="all")
    check(
        "不传排序参数时保持默认顺序（未归还优先）",
        bool(default_items) and default_items[0]["status"] != "returned",
        default_items[0]["status"] if default_items else None,
    )

    # ---- 28.2 续借口径：列表的 can_renew 与真实闸门一致（冻结读者不可续借） ----
    r = api.post("/api/members", json={"card_no": "2026888", "name": "续借口径读者", "member_type": "学生", "status": "active"})
    freeze_member_id = r.get_json()["id"]
    r = api.post("/api/books", json={"isbn": "9780000000901", "title": "续借口径测试书", "total_copies": 1})
    freeze_book_id = r.get_json()["id"]
    loan = api.post("/api/loans/borrow", json={"book_id": freeze_book_id, "member_id": freeze_member_id}).get_json()
    freeze_loan_id = loan["id"]
    check("新建借阅默认可续借", loan["can_renew"] is True, loan.get("can_renew"))

    # 业务规则：有未还借阅的读者不允许被冻结（接口层拦截，返回 409）。
    r = api.put(f"/api/members/{freeze_member_id}", json={
        "card_no": "2026888", "name": "续借口径读者", "member_type": "学生", "status": "frozen",
    })
    check("有未还借阅时接口拒绝冻结（业务规则 409）", r.status_code == 409, (r.status_code, r.get_json()))

    # 接口拿不到「在借又冻结」的组合，这里直接落库构造该状态，才能真正验证续借闸门。
    with app.app_context():
        db.execute("UPDATE members SET status='frozen' WHERE id=?", [freeze_member_id])
    frozen_row = next(
        (row for row in page_items("/api/loans", keyword="续借口径测试书", status="open") if row["id"] == freeze_loan_id),
        None,
    )
    check("冻结读者后列表 can_renew 变为 False", frozen_row is not None and frozen_row["can_renew"] is False, frozen_row and frozen_row.get("can_renew"))
    check("冻结读者续借返回 422", api.post(f"/api/loans/{freeze_loan_id}/renew").status_code == 422)

    with app.app_context():
        db.execute("UPDATE members SET status='active' WHERE id=?", [freeze_member_id])
    thawed_row = next(
        (row for row in page_items("/api/loans", keyword="续借口径测试书", status="open") if row["id"] == freeze_loan_id),
        None,
    )
    check("恢复后 can_renew 又变回 True", thawed_row is not None and thawed_row["can_renew"] is True, thawed_row and thawed_row.get("can_renew"))
    check("恢复后续借成功", api.post(f"/api/loans/{freeze_loan_id}/renew").status_code == 200)

    # ---- 28.3 IntegrityError 细分：不再一律说「唯一字段重复」 ----
    integrity_handler = None
    for _bp, mapping in app.error_handler_spec.items():
        for _code, exc_map in (mapping or {}).items():
            if sqlite3.IntegrityError in exc_map:
                integrity_handler = exc_map[sqlite3.IntegrityError]
    check("注册了 IntegrityError 处理器", integrity_handler is not None)

    def integrity_response(errorname: str):
        exc = sqlite3.IntegrityError("boom")
        exc.sqlite_errorname = errorname
        with app.test_request_context("/api/books", method="POST"):
            response, status = integrity_handler(exc)
            return status, response.get_json()["error"]

    status, err = integrity_response("SQLITE_CONSTRAINT_UNIQUE")
    check("UNIQUE 违规 → 409 且指向唯一字段", status == 409 and "唯一" in err["message"], (status, err))
    status, err = integrity_response("SQLITE_CONSTRAINT_NOTNULL")
    check("NOT NULL 违规 → 400 且提示必填缺失", status == 400 and "必填" in err["message"], (status, err))
    status, err = integrity_response("SQLITE_CONSTRAINT_CHECK")
    check("CHECK 违规 → 400 且提示一致性约束", status == 400 and "一致性" in err["message"], (status, err))
    status, err = integrity_response("SQLITE_CONSTRAINT_FOREIGNKEY")
    check("外键违规 → 409", status == 409, (status, err))

    # ---- 28.4 ISBN 校验位：只做提示不阻断保存 ----
    check("合法 ISBN-13 判定为 ok", isbn_check("9787506365437") == "ok", isbn_check("9787506365437"))
    check("带连字符的合法 ISBN-13 判定为 ok", isbn_check("978-7-506-36543-7") == "ok")
    check("合法 ISBN-10 判定为 ok", isbn_check("0306406152") == "ok", isbn_check("0306406152"))
    check("以 X 结尾的合法 ISBN-10 判定为 ok", isbn_check("097522980X") == "ok", isbn_check("097522980X"))
    check("校验位错误的 ISBN-13 判定为 invalid_checksum", isbn_check("9787111111111") == "invalid_checksum", isbn_check("9787111111111"))
    check("长度不合法的 ISBN 判定为 unknown_format", isbn_check("123") == "unknown_format", isbn_check("123"))
    r = api.post("/api/books", json={"isbn": "9780000000777", "title": "校验位提示测试书", "total_copies": 1})
    check("校验位不规范的 ISBN 仍可保存（提示而非阻断）", r.status_code == 201, r.get_json())
    check("图书详情回带 isbn_check 字段", r.get_json()["isbn_check"] == "invalid_checksum", r.get_json().get("isbn_check"))

    print("\n=== 28B. 前端资源契约（排序 / 焦点陷阱 / 标签打印 / 移动端 / 错误兜底） ===")
    root2 = Path(__file__).resolve().parent.parent
    js2 = (root2 / "static" / "js" / "app.js").read_text(encoding="utf-8")
    css2 = (root2 / "static" / "css" / "app.css").read_text(encoding="utf-8")
    html2 = (root2 / "templates" / "index.html").read_text(encoding="utf-8")
    public_html = (root2 / "templates" / "public.html").read_text(encoding="utf-8")
    check("表头排序已实现", "sortHeader" in js2 and "data-sort" in js2 and "toggleSort" in js2)
    check("排序状态写入 URL", 'params.set("sort"' in js2 and 'params.get("sort")' in js2)
    check("弹窗焦点陷阱已实现", "trapTab" in js2 and 'e.key === "Tab"' in js2)
    check("关闭弹窗后焦点归位", "lastFocusedElement" in js2)
    check("表单 label 与控件建立关联", "linkLabels" in js2 and 'setAttribute("for"' in js2)
    check("弹窗声明 dialog 语义", 'role="dialog"' in html2 and 'aria-modal="true"' in html2)
    check("标签打印支持份数与尺寸", "LABEL_SIZES" in js2 and "openLabelDialog" in js2)
    check("标签打印按份数平铺", "print-layer" in css2 and "break-inside: avoid" in css2)
    check("移动端媒体查询已就位", "@media (max-width: 900px)" in css2 and "@media (max-width: 560px)" in css2)
    check("全局未捕获错误兜底", "unhandledrejection" in js2 and "reportGlobalError" in js2)
    check("5xx 错误附带后端错误编号", "request_id" in js2 and "错误编号" in js2)
    check("门户不再公开初始密码推导规则", "证号后 6 位" not in public_html and "初始密码由馆员告知" in public_html)
    check("可排序表头有可点击样式", "th.sortable" in css2)

    print("\n=== 28C. 会话到期提醒 ===")
    r = client.get("/api/auth/me")
    check("已登录时 /api/auth/me 回带会话信息", r.status_code == 200 and "session" in r.get_json(), r.get_json())
    sess = (r.get_json() or {}).get("session") or {}
    check("会话时长与配置一致（秒）", sess.get("lifetime_seconds") == 8 * 3600, sess)
    check("会话是滑动续期（前端据此按闲置时长判断）", sess.get("refresh_on_request") is True, sess)

    check("页面含会话过期横幅", 'id="sessionBanner"' in html2 and 'id="sessionBannerBtn"' in html2, None)
    check("会话横幅有独立警示样式", ".alert-warn" in css2)
    check("闲置到期提醒逻辑已实现", "sessionWatch" in js2 and "SESSION_WARN_BEFORE" in js2)
    check("成功请求会刷新闲置计时", "sessionWatch.touch()" in js2)
    check("保持登录 / 重新登录出口齐备", '"keepalive"' in js2 and '"relogin"' in js2)
    check("加载会话后启动到期监视", "sessionWatch.start(" in js2)

    print("\n=== 29. 后台账号、角色权限与操作日志 ===")

    # ---- 29.1 账号管理：新建 / 列表 / 校验 ----
    r = api.post("/api/accounts", json={
        "username": "lib1", "password": "lib12345",
        "display_name": "流通馆员小李", "role": "librarian",
    })
    check("新建流通馆员账号返回 201", r.status_code == 201, (r.status_code, r.get_json()))
    lib_account = r.get_json() or {}
    lib_id = lib_account.get("id")
    check("账号回带角色中文名", lib_account.get("role_label") == "流通馆员", lib_account.get("role_label"))
    check("账号响应不含口令散列", "password_hash" not in lib_account, list(lib_account.keys()))
    lib_perms = set(lib_account.get("permissions") or [])
    check(
        "流通馆员不含账号管理与日志权限",
        "account:manage" not in lib_perms and "log:read" not in lib_perms,
        sorted(lib_perms),
    )
    check(
        "流通馆员含业务写入权限",
        {"book:write", "member:write", "loan:write"} <= lib_perms,
        sorted(lib_perms),
    )

    r = api.get("/api/accounts")
    acc_body = r.get_json() or {}
    check("账号列表可读取", r.status_code == 200 and acc_body.get("total", 0) >= 2, acc_body.get("total"))
    check("账号列表不含口令散列", all("password_hash" not in item for item in acc_body.get("items", [])))
    check("账号列表附带角色选项", any(o["value"] == "admin" for o in acc_body.get("roles", [])))

    r = api.get(f"/api/accounts/{lib_id}")
    check("单个账号可读取", r.status_code == 200 and r.get_json().get("username") == "lib1", r.status_code)

    check(
        "用户名重复返回 409",
        api.post("/api/accounts", json={"username": "lib1", "password": "lib12345", "role": "librarian"}).status_code == 409,
    )
    taken_card = page_items("/api/members")[0]["card_no"]
    check(
        "用户名与读者证号重名返回 409",
        api.post("/api/accounts", json={"username": taken_card, "password": "lib12345", "role": "librarian"}).status_code == 409,
        taken_card,
    )
    check(
        "非法用户名格式返回 400",
        api.post("/api/accounts", json={"username": "bad role", "password": "lib12345", "role": "librarian"}).status_code == 400,
    )
    check(
        "密码过短返回 400",
        api.post("/api/accounts", json={"username": "lib2", "password": "123", "role": "librarian"}).status_code == 400,
    )
    check(
        "非法角色返回 400",
        api.post("/api/accounts", json={"username": "lib2", "password": "lib12345", "role": "root"}).status_code == 400,
    )

    # ---- 29.2 角色权限：馆员能做业务，但碰不到账号与日志 ----
    lib_client = app.test_client()
    lib_api = Api(lib_client)
    r = lib_client.post("/api/auth/login", json={"username": "lib1", "password": "lib12345"})
    check("流通馆员可登录后台", r.status_code == 200 and r.get_json().get("role") == "admin", (r.status_code, r.get_json()))
    lib_api.csrf = (r.get_json() or {}).get("csrf_token")
    check("会话回带角色中文名", lib_client.get("/api/auth/me").get_json().get("role_label") == "流通馆员")

    check("馆员可读图书列表（读不受限）", lib_client.get("/api/books").status_code == 200)
    check("馆员可读读者列表", lib_client.get("/api/members").status_code == 200)
    check("馆员可读概览报表", lib_client.get("/api/dashboard/overview").status_code == 200)

    r = lib_api.post("/api/books", json={"isbn": "9780000000999", "title": "馆员编目测试书", "total_copies": 1})
    check("馆员可新增图书（book:write）", r.status_code == 201, (r.status_code, r.get_json()))
    lib_book_id = (r.get_json() or {}).get("id")
    check("馆员可删除图书", lib_api.delete(f"/api/books/{lib_book_id}").status_code == 200)

    r = lib_client.get("/api/accounts")
    check("馆员读账号列表被拒 403", r.status_code == 403, (r.status_code, r.get_json()))
    check("403 提示说明缺少哪项权限", "权限" in (r.get_json() or {}).get("error", {}).get("message", ""), r.get_json())
    r = lib_api.post("/api/accounts", json={"username": "hack", "password": "hack12345", "role": "admin"})
    check("馆员建账号被拒 403", r.status_code == 403, (r.status_code, r.get_json()))
    check("馆员读操作日志被拒 403", lib_client.get("/api/logs").status_code == 403)
    check("馆员删账号被拒 403", lib_api.delete(f"/api/accounts/{lib_id}").status_code == 403)

    # ---- 29.3 审计留痕：业务操作与越权尝试都进日志 ----
    r = api.get("/api/logs", query_string={"page_size": 50, "action": "permission.denied"})
    denied = r.get_json() or {}
    check("越权尝试被记入日志", r.status_code == 200 and denied.get("total", 0) >= 2, denied.get("total"))
    denied_items = denied.get("items") or []
    if denied_items:
        check("越权日志标记为已拦截", denied_items[0]["status"] == "denied", denied_items[0].get("status"))
        check("越权日志记录了操作者姓名", denied_items[0]["actor_name"] == "流通馆员小李", denied_items[0].get("actor_name"))
        check("越权日志带动作中文名", denied_items[0]["action_label"] == "权限不足（已拦截）", denied_items[0].get("action_label"))

    r = api.get("/api/logs", query_string={"page_size": 20})
    logs = r.get_json() or {}
    check("操作日志可分页读取", r.status_code == 200 and logs.get("total", 0) > 0, logs.get("total"))
    visible = logs.get("items") or []
    check("日志按时间倒序（id 递减）", len(visible) < 2 or visible[0]["id"] > visible[-1]["id"])
    check("日志带操作者类型中文名", bool(visible) and bool(visible[0].get("actor_type_label")), visible[:1])

    r = api.get("/api/logs", query_string={"page_size": 50, "action": "book.create"})
    created = r.get_json() or {}
    check("新增图书已留痕", created.get("total", 0) >= 1, created.get("total"))
    created_items = created.get("items") or []
    if created_items:
        check("日志摘要含具体对象（书名）", "《" in created_items[0]["summary"], created_items[0].get("summary"))

    # 同一动作由不同角色做出时，日志要能区分到人（含角色）——这是分权可追责的前提
    mine = api.get("/api/logs", query_string={
        "page_size": 20, "action": "book.create", "keyword": "馆员编目测试书",
    }).get_json() or {}
    check(
        "馆员自己的操作留痕且角色标注为 librarian",
        mine.get("total", 0) >= 1 and (mine.get("items") or [{}])[0].get("actor_role") == "librarian",
        [i.get("actor_role") for i in (mine.get("items") or [])][:3],
    )
    admins = api.get("/api/logs", query_string={
        "page_size": 20, "action": "book.create", "keyword": "校验位提示测试书",
    }).get_json() or {}
    check(
        "管理员的同类操作标注为 admin",
        admins.get("total", 0) >= 1 and (admins.get("items") or [{}])[0].get("actor_role") == "admin",
        [i.get("actor_role") for i in (admins.get("items") or [])][:3],
    )
    check(
        "日志支持关键词搜索",
        api.get("/api/logs", query_string={"page_size": 10, "keyword": "已拦截"}).get_json().get("total", 0) >= 1,
    )
    check("日志概览可读取", api.get("/api/logs/overview").status_code == 200)

    # ---- 29.4 自锁防护与「最后一个管理员」纵深防御 ----
    admin_id = api.get("/api/auth/me").get_json()["admin"]["id"]
    r = api.put(f"/api/accounts/{lib_id}", json={"is_active": False})
    check("停用普通账号成功", r.status_code == 200 and r.get_json().get("is_active") is False, (r.status_code, r.get_json()))
    check("停用后该账号会话立即失效（401）", lib_client.get("/api/auth/me").status_code == 401)
    check("停用后无法再登录", lib_client.post("/api/auth/login", json={"username": "lib1", "password": "lib12345"}).status_code != 200)

    check(
        "不能停用自己的账号（422）",
        api.put(f"/api/accounts/{admin_id}", json={"is_active": False}).status_code == 422,
    )
    check(
        "不能修改自己的角色（422）",
        api.put(f"/api/accounts/{admin_id}", json={"role": "librarian"}).status_code == 422,
    )
    check("不能删除自己的账号（422）", api.delete(f"/api/accounts/{admin_id}").status_code == 422)

    # 「最后一个在职管理员」是自锁防护之外的纵深防御：即便绕过"不能操作自己"
    # （例如脚本里 actor_id 传空），也不允许把系统改到没人能管后台的状态。
    from library.accounts import service as accounts_service  # 局部导入
    from library.errors import BusinessRuleError  # 局部导入

    with app.app_context():
        try:
            accounts_service.delete_account(admin_id, actor_id=None)
            last_admin_blocked = False
        except BusinessRuleError as exc:
            last_admin_blocked = "最后一个" in str(exc)
    check("最后一个在职管理员不可被删除（纵深防御）", last_admin_blocked)

    # ---- 29.5 重新启用 / 重置密码 / 删除 ----
    r = api.put(f"/api/accounts/{lib_id}", json={
        "is_active": True, "role": "librarian", "display_name": "流通馆员小李",
    })
    check("重新启用账号成功", r.status_code == 200 and r.get_json().get("is_active") is True, (r.status_code, r.get_json()))
    check(
        "重新启用后可再次登录",
        lib_client.post("/api/auth/login", json={"username": "lib1", "password": "lib12345"}).status_code == 200,
    )

    r = api.post(f"/api/accounts/{lib_id}/password", json={"password": "newlib12345"})
    check("重置后台账号密码成功", r.status_code == 200, (r.status_code, r.get_json()))
    check(
        "旧密码失效",
        lib_client.post("/api/auth/login", json={"username": "lib1", "password": "lib12345"}).status_code == 401,
    )
    check(
        "新密码可登录",
        lib_client.post("/api/auth/login", json={"username": "lib1", "password": "newlib12345"}).status_code == 200,
    )

    r = api.delete(f"/api/accounts/{lib_id}")
    check("删除账号成功", r.status_code == 200, (r.status_code, r.get_json()))
    check("删除后账号不存在（404）", api.get(f"/api/accounts/{lib_id}").status_code == 404)

    # ---- 29.6 未登录一律拦在门外 ----
    anon = app.test_client()
    check("未登录读账号列表 401", anon.get("/api/accounts").status_code == 401)
    check("未登录读操作日志 401", anon.get("/api/logs").status_code == 401)
    check("未登录建账号 401", anon.post("/api/accounts", json={"username": "x", "password": "xxxxxx", "role": "admin"}).status_code == 401)

    print("\n=== 29B. 前端资源契约（角色入口 / 账号管理 / 操作日志） ===")
    html_role = (root2 / "templates" / "index.html").read_text(encoding="utf-8")
    js_role = (root2 / "static" / "js" / "app.js").read_text(encoding="utf-8")
    css_role = (root2 / "static" / "css" / "app.css").read_text(encoding="utf-8")
    check("导航含账号管理入口", 'data-view="accounts"' in html_role and 'data-permission="account:manage"' in html_role)
    check("导航含操作日志入口", 'data-view="logs"' in html_role and 'data-permission="log:read"' in html_role)
    check("无权入口默认隐藏（避免点进去才被拒）", 'data-permission="account:manage" hidden' in html_role)
    check("前端按权限收拢入口", "applyPermissions" in js_role and "function can(" in js_role)
    check("越权视图回落概览", "VIEWS[target].permission" in js_role and "VIEWS[view].permission" in js_role)
    check("账号管理调用账号接口", "/api/accounts" in js_role)
    check("操作日志调用日志接口", "/api/logs" in js_role)
    check(
        "行内写操作按钮按权限渲染",
        'can("book:write")' in js_role and 'can("member:write")' in js_role and 'can("loan:write")' in js_role,
    )
    check("停用账号 / 拦截记录有降噪样式", "is-inactive" in css_role and "is-inactive" in js_role)

    print("\n=== 30. 预约队列、滞纳金缴纳与借书证挂失 ===")
    # 用独立库跑这一段：预约/缴费是一次性状态迁移（缴了就没了），
    # 混在主库里会与前后用例互相污染。独立库还能自由回填 due_at 造逾期。
    res_root = Path(tempfile.mkdtemp(prefix="resv-smoke-"))
    res_app = build_app(str(res_root / "resv.db"))
    radmin = Api(res_app.test_client())
    radmin.csrf = (radmin.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD}).get_json() or {}).get("csrf_token")

    def new_member(card: str, name: str) -> int:
        r = radmin.post(
            "/api/members",
            json={"card_no": card, "name": name, "member_type": "学生", "status": "active", "max_borrow": 50},
        )
        assert r.status_code in (200, 201), r.get_json()
        member_id = int(r.get_json()["id"])
        # 建号会带出「首次登录必须改密」，本节验证的是预约/缴费/挂失，先把闸门放过去
        with res_app.app_context():
            db.execute("UPDATE members SET must_change_password = 0 WHERE id = ?", [member_id])
        return member_id

    def new_book(isbn: str, title: str, total: int = 1) -> int:
        r = radmin.post(
            "/api/books",
            json={"isbn": isbn, "title": title, "category": "预约测试", "total_copies": total},
        )
        assert r.status_code in (200, 201), r.get_json()
        return int(r.get_json()["id"])

    def borrow(book_id: int, member_id: int):
        return radmin.post("/api/loans/borrow", json={"book_id": book_id, "member_id": member_id})

    def reader_session(card: str):
        client = res_app.test_client()
        r = client.post("/api/reader/login", json={"account": card, "password": card})
        assert r.status_code == 200, r.get_json()
        return client, {"X-CSRF-Token": (r.get_json() or {}).get("csrf_token")}

    def backdate_due(loan_id: int, days_ago: int) -> None:
        """把某笔在借的应还日期回填到 N 天前（造逾期用）。"""
        with res_app.app_context():
            db.execute(
                "UPDATE loans SET due_at = ? WHERE id = ?",
                [timeutil.to_str(timeutil.now() - timedelta(days=days_ago)), loan_id],
            )

    m_jia = new_member("RS0001", "预约测试甲")
    m_yi = new_member("RS0002", "预约测试乙")
    m_bing = new_member("RS0003", "预约测试丙")

    b_wei = new_book("9787888000001", "预约测试·唯一复本")
    b_avail = new_book("9787888000002", "预约测试·有库存")
    b_ready = new_book("9787888000003", "预约测试·到书")
    b_ful = new_book("9787888000004", "预约测试·借出")
    b_cancel = new_book("9787888000005", "预约测试·取消")
    b_over = new_book("9787888000006", "预约测试·逾期")
    b_cap = new_book("9787888000007", "预约测试·封顶")
    b_today = new_book("9787888000008", "预约测试·当天")
    b_admin = new_book("9787888000009", "预约测试·代收")
    b_wv = new_book("9787888000010", "预约测试·减免")
    b_l3 = new_book("9787888000011", "预约测试·挂失预约")
    b_lost = new_book("9787888000012", "预约测试·挂失在借")
    b_l2 = new_book("9787888000013", "预约测试·挂失后借书")

    # 所有借书必须在「回填逾期」之前完成 —— 有逾期未还的读者不能再借书。
    loan_wei = borrow(b_wei, m_jia).get_json()
    loan_ready = borrow(b_ready, m_jia).get_json()
    loan_cancel = borrow(b_cancel, m_jia).get_json()
    loan_ful = borrow(b_ful, m_jia).get_json()
    loan_over = borrow(b_over, m_jia).get_json()
    loan_cap = borrow(b_cap, m_jia).get_json()
    loan_today = borrow(b_today, m_jia).get_json()
    # 代收 / 减免用丙的书：甲一会儿会「一次结清」，会把甲名下所有待缴都清掉，
    # 馆员侧那两步就无单可结了。
    loan_admin = borrow(b_admin, m_bing).get_json()
    loan_wv = borrow(b_wv, m_bing).get_json()
    loan_l3 = borrow(b_l3, m_jia).get_json()
    loan_lost = borrow(b_lost, m_yi).get_json()
    check(
        "前提：借书成功且库存扣减",
        all(x.get("id") for x in (loan_wei, loan_ready, loan_cancel, loan_ful, loan_over, loan_cap, loan_lost)),
        loan_wei,
    )

    c_yi, h_yi = reader_session("RS0002")
    c_bing, h_bing = reader_session("RS0003")

    # ---- 30.1 预约状态机：排队 → 到书 → 借出 ----
    res = c_yi.post("/api/reader/reservations", json={"book_id": b_wei}, headers=h_yi)
    body = res.get_json() or {}
    check("读者可预约「无可借复本」的图书", res.status_code in (200, 201), body)
    check("预约初始状态为「排队中」", body.get("status") == "waiting" and body.get("status_label") == "排队中", body)
    check("排队位次为第 1 位", body.get("queue_position") == 1, body)
    res_id = int(body.get("id") or 0)

    dup = c_yi.post("/api/reader/reservations", json={"book_id": b_wei}, headers=h_yi)
    check("同一读者重复预约被拒（409）", dup.status_code == 409, dup.get_json())

    avail = c_yi.post("/api/reader/reservations", json={"book_id": b_avail}, headers=h_yi)
    check("有可借复本时不允许预约（422）", avail.status_code == 422, avail.get_json())

    me_yi = c_yi.get("/api/reader/me").get_json()
    check("读者中心暴露预约汇总", me_yi["reservations"]["open_total"] == 1, me_yi.get("reservations"))

    check("前提：甲的《唯一复本》归还成功", radmin.post(f"/api/loans/{loan_wei['id']}/return").status_code == 200)
    mine = next(
        (i for i in c_yi.get("/api/reader/reservations", query_string={"status": "all"}).get_json()["items"] if i["id"] == res_id),
        {},
    )
    check("还书后预约自动转「已到书」（到书通知）", mine.get("status") == "ready" and mine.get("status_label") == "已到书", mine)

    check("前提：乙借走该复本", borrow(b_wei, m_yi).status_code in (200, 201))
    mine = next(
        (i for i in c_yi.get("/api/reader/reservations", query_string={"status": "all"}).get_json()["items"] if i["id"] == res_id),
        {},
    )
    check("读者借出后预约自动结单为「已借出」", mine.get("status") == "fulfilled", mine)

    # 馆员侧手动通知到书
    res_ready = c_bing.post("/api/reader/reservations", json={"book_id": b_ready}, headers=h_bing).get_json() or {}
    notified = radmin.post(f"/api/reservations/{res_ready.get('id')}/ready")
    check("馆员可手动「通知到书」", notified.status_code == 200 and notified.get_json().get("status") == "ready", notified.get_json())
    again = radmin.post(f"/api/reservations/{res_ready.get('id')}/ready")
    check("重复通知到书被拒（422）", again.status_code == 422, again.get_json())

    # 取消 + 越权
    res_cancel = c_bing.post("/api/reader/reservations", json={"book_id": b_cancel}, headers=h_bing).get_json() or {}
    other = c_yi.post(f"/api/reader/reservations/{res_cancel.get('id')}/cancel", headers=h_yi)
    check("取消他人预约被拒（403）", other.status_code == 403, other.get_json())
    own = c_bing.post(f"/api/reader/reservations/{res_cancel.get('id')}/cancel", headers=h_bing)
    check("本人可取消预约（转「已取消」）", own.status_code == 200 and own.get_json().get("status") == "cancelled", own.get_json())

    # 馆员列表 / 关键字 / 状态过滤（关键字要跨表 JOIN，曾因 COUNT 少 JOIN 直接 500）
    all_res = radmin.get("/api/reservations", query_string={"page_size": 100}).get_json()
    check("馆员可查看全部预约", all_res["total"] >= 3, all_res.get("total"))
    check("馆员接口附带「排队中总数」", "waiting_total" in all_res, all_res.get("waiting_total"))
    kw_book = radmin.get("/api/reservations", query_string={"keyword": "到书", "page_size": 100}).get_json()
    check(
        "预约列表按书名过滤（覆盖计数路径的 JOIN）",
        kw_book["total"] >= 1 and all("到书" in i["book_title"] for i in kw_book["items"]),
        kw_book.get("total"),
    )
    kw_member = radmin.get("/api/reservations", query_string={"keyword": "预约测试乙", "page_size": 100}).get_json()
    check(
        "预约列表按读者姓名过滤（覆盖计数路径的 JOIN）",
        kw_member["total"] >= 1 and all(i["member_name"] == "预约测试乙" for i in kw_member["items"]),
        kw_member.get("total"),
    )
    kw_none = radmin.get("/api/reservations", query_string={"keyword": "查无此书XYZ", "page_size": 100}).get_json()
    check("预约关键字无命中时返回空集而非报错", kw_none["total"] == 0 and kw_none["items"] == [], kw_none.get("total"))
    st_cancel = radmin.get("/api/reservations", query_string={"status": "cancelled", "page_size": 100}).get_json()
    check(
        "预约列表按状态过滤",
        st_cancel["total"] >= 1 and all(i["status"] == "cancelled" for i in st_cancel["items"]),
        st_cancel.get("total"),
    )

    anon = res_app.test_client()
    check("未登录读预约列表 401", anon.get("/api/reservations").status_code == 401)
    check("未登录读缴费流水 401", anon.get("/api/fines").status_code == 401)
    check("读者会话调馆员预约接口 401", c_yi.get("/api/reservations").status_code == 401)

    # ---- 30.2 滞纳金：整日口径 + 封顶 + 读者自助缴纳 ----
    backdate_due(loan_over["id"], 10)
    backdate_due(loan_cap["id"], 200)
    backdate_due(loan_admin["id"], 6)
    backdate_due(loan_wv["id"], 4)
    with res_app.app_context():
        # 应还「今天 00:00」：整日口径下逾期 0 天，不应产生罚金
        db.execute(
            "UPDATE loans SET due_at = ? WHERE id = ?",
            [timeutil.to_str(timeutil.now().replace(hour=0, minute=0, second=0, microsecond=0)), loan_today["id"]],
        )

    c_jia, h_jia = reader_session("RS0001")
    fines = c_jia.get("/api/reader/fines").get_json()
    rules = fines["rules"]
    per_day = float(rules["fine_per_day"])
    cap = float(rules["fine_cap"])
    item_over = next((i for i in fines["items"] if i["loan_id"] == loan_over["id"]), None)
    check("逾期在借会进入读者的待缴明细", item_over is not None, fines.get("items"))
    check("逾期 10 天罚金 = 天数 × 日标准", abs(item_over["fine"] - round(10 * per_day, 2)) < 1e-6, item_over)
    check("未结算时「待缴」等于「应缴」", abs(item_over["outstanding"] - item_over["fine"]) < 1e-6, item_over)
    check("在借逾期标注为「在借逾期」", item_over["state"] == "overdue" and item_over["state_label"] == "在借逾期", item_over)
    check(
        "汇总待缴 = 明细逐笔相加（口径统一）",
        abs(fines["total_outstanding"] - round(sum(i["outstanding"] for i in fines["items"]), 2)) < 1e-6,
        fines.get("total_outstanding"),
    )
    item_cap = next((i for i in fines["items"] if i["loan_id"] == loan_cap["id"]), None)
    check("超长逾期按封顶金额计费", item_cap is not None and abs(item_cap["fine"] - cap) < 1e-6, item_cap)
    check("封顶记录带 capped 标记", item_cap.get("capped") is True, item_cap)
    check("封顶不改变真实逾期天数", item_cap["overdue_days"] == 200, item_cap)
    check("应还当天不算逾期、不产生待缴（整日口径）", all(i["loan_id"] != loan_today["id"] for i in fines["items"]), fines.get("items"))

    reader_me = c_jia.get("/api/reader/me").get_json()
    check(
        "读者中心待缴汇总与滞纳金面板一致",
        abs(reader_me["fines"]["total_outstanding"] - fines["total_outstanding"]) < 1e-6,
        (reader_me["fines"], fines["total_outstanding"]),
    )

    paid_before = float(fines["total_paid"])
    pay_one = c_jia.post("/api/reader/fines/pay", json={"loan_id": loan_over["id"]}, headers=h_jia)
    pay_body = pay_one.get_json() or {}
    check("读者可自助缴纳单笔滞纳金", pay_one.status_code == 200, pay_body)
    check("缴纳金额等于该笔待缴", abs(pay_body.get("paid_total", -1) - item_over["outstanding"]) < 1e-6, pay_body)
    check("缴纳后该笔不再出现在待缴明细", all(i["loan_id"] != loan_over["id"] for i in pay_body["overview"]["items"]))
    check(
        "缴纳后累计已缴正确累加",
        abs(pay_body["overview"]["total_paid"] - paid_before - pay_body["paid_total"]) < 1e-6,
        pay_body["overview"].get("total_paid"),
    )
    check("缴纳历史留有流水", len(pay_body["overview"]["history"]) >= 1, len(pay_body["overview"]["history"]))
    repay = c_jia.post("/api/reader/fines/pay", json={"loan_id": loan_over["id"]}, headers=h_jia)
    check("重复缴纳同一笔被拒（404/422）", repay.status_code in (404, 422), repay.status_code)

    pay_all = c_jia.post("/api/reader/fines/pay", json={"all": True}, headers=h_jia)
    check("读者可一次结清全部待缴", pay_all.status_code == 200, pay_all.get_json())
    check("结清后待缴归零", pay_all.get_json()["overview"]["total_outstanding"] == 0, pay_all.get_json()["overview"].get("total_outstanding"))

    # ---- 30.3 馆员代收 / 减免（用丙的账，避免被甲的「一次结清」提前清掉）----
    owed = radmin.get(f"/api/fines/member/{m_bing}").get_json()
    target = next(i for i in owed["items"] if i["loan_id"] == loan_admin["id"])
    over_pay = radmin.post(
        "/api/fines/collect",
        json={"member_id": m_bing, "loan_id": loan_admin["id"], "amount": round(target["outstanding"] + 5, 2)},
    )
    check("馆员代收金额不得超过应缴（422）", over_pay.status_code == 422, over_pay.get_json())
    zero_pay = radmin.post("/api/fines/collect", json={"member_id": m_bing, "loan_id": loan_admin["id"], "amount": 0})
    check("馆员代收金额必须大于 0（400）", zero_pay.status_code == 400, zero_pay.get_json())
    no_loan = radmin.post("/api/fines/collect", json={"member_id": m_bing})
    check("馆员代收缺少借阅记录时 400", no_loan.status_code == 400, no_loan.get_json())
    ok_pay = radmin.post("/api/fines/collect", json={"member_id": m_bing, "loan_id": loan_admin["id"]})
    check(
        "馆员代收（金额缺省=结清该笔）成功",
        ok_pay.status_code == 200 and abs(ok_pay.get_json()["amount"] - target["outstanding"]) < 1e-6,
        ok_pay.get_json(),
    )
    left = radmin.get(f"/api/fines/member/{m_bing}").get_json()
    check("代收后该笔结清", all(i["loan_id"] != loan_admin["id"] for i in left["items"]), left.get("total_outstanding"))

    no_reason_waive = radmin.post("/api/fines/waive", json={"member_id": m_bing, "loan_id": loan_wv["id"]})
    check(
        "减免不写理由被 400 拒（减免是权力，必须留痕）",
        no_reason_waive.status_code == 400 and no_reason_waive.get_json()["error"]["details"]["field"] == "note",
        no_reason_waive.get_json(),
    )
    waived = radmin.post(
        "/api/fines/waive",
        json={"member_id": m_bing, "loan_id": loan_wv["id"], "note": "疫情闭馆期间免收"},
    )
    check("馆员可减免滞纳金", waived.status_code == 200 and waived.get_json()["kind"] == "waiver", waived.get_json())
    left = radmin.get(f"/api/fines/member/{m_bing}").get_json()
    check("减免后该笔同样结清", all(i["loan_id"] != loan_wv["id"] for i in left["items"]), left.get("total_outstanding"))
    check("全部结清后汇总待缴为 0", left["total_outstanding"] == 0, left.get("total_outstanding"))

    # 缴费流水（关键字同样跨表 JOIN）
    pays = radmin.get("/api/fines", query_string={"page_size": 100}).get_json()
    check("馆员可查看缴费流水", pays["total"] >= 3, pays.get("total"))
    check(
        "流水汇总与明细逐笔相加一致",
        abs(pays["totals"]["paid"] - round(sum(i["amount"] for i in pays["items"] if i["kind"] == "payment"), 2)) < 1e-6,
        pays.get("totals"),
    )
    kw_pay_book = radmin.get("/api/fines", query_string={"keyword": "代收", "page_size": 100}).get_json()
    check(
        "缴费流水按书名过滤（覆盖计数路径的 JOIN）",
        kw_pay_book["total"] >= 1 and all("代收" in i["book_title"] for i in kw_pay_book["items"]),
        kw_pay_book.get("total"),
    )
    check(
        "过滤后的汇总只统计命中记录",
        abs(kw_pay_book["totals"]["paid"] - round(sum(i["amount"] for i in kw_pay_book["items"] if i["kind"] == "payment"), 2)) < 1e-6,
        kw_pay_book.get("totals"),
    )
    kw_pay_member = radmin.get("/api/fines", query_string={"keyword": "预约测试甲", "page_size": 100}).get_json()
    check(
        "缴费流水按读者姓名过滤",
        kw_pay_member["total"] >= 1 and all(i["member_name"] == "预约测试甲" for i in kw_pay_member["items"]),
        kw_pay_member.get("total"),
    )
    kw_kind = radmin.get("/api/fines", query_string={"kind": "waiver", "page_size": 100}).get_json()
    check("缴费流水按类型过滤", kw_kind["total"] >= 1 and all(i["kind"] == "waiver" for i in kw_kind["items"]), kw_kind.get("total"))

    # ---- 30.4 借书证挂失：与 borrowed 状态正交，闸门只拦写操作 ----
    lost = c_yi.post("/api/reader/card/lost", headers=h_yi)
    check("读者可自助挂失", lost.status_code == 200 and lost.get_json().get("card_lost") is True, lost.get_json())
    check("挂失后回显挂失时间", bool(c_yi.get("/api/reader/me").get_json()["member"].get("lost_at")))

    r_lost_borrow = radmin.post("/api/loans/borrow", json={"book_id": b_l2, "member_id": m_yi})
    check("挂失后无法借书（422）", r_lost_borrow.status_code == 422, r_lost_borrow.get_json())
    r_lost_renew = c_yi.post(f"/api/reader/loans/{loan_lost['id']}/renew", headers=h_yi)
    check("挂失后无法自助续借（422）", r_lost_renew.status_code == 422, r_lost_renew.get_json())
    r_lost_res = c_yi.post("/api/reader/reservations", json={"book_id": b_l3}, headers=h_yi)
    check("挂失后无法预约（422）", r_lost_res.status_code == 422, r_lost_res.get_json())
    check(
        "挂失后仍可查看借阅记录",
        c_yi.get("/api/reader/loans", query_string={"status": "all"}).status_code == 200,
    )
    mem_yi = radmin.get(f"/api/members/{m_yi}").get_json()
    check("馆员端可见挂失标记", bool(mem_yi.get("card_lost")), mem_yi.get("card_lost"))

    unlost = c_yi.post("/api/reader/card/unlost", headers=h_yi)
    check("可自助解除挂失", unlost.status_code == 200 and unlost.get_json().get("card_lost") is False, unlost.get_json())
    check("解除挂失后恢复借书", borrow(b_l2, m_yi).status_code in (200, 201))

    # ---- 30.5 前端资源契约 ----
    root_res = Path(__file__).resolve().parent.parent
    html_admin = (root_res / "templates" / "index.html").read_text(encoding="utf-8")
    js_admin = (root_res / "static" / "js" / "app.js").read_text(encoding="utf-8")
    html_reader = (root_res / "templates" / "reader.html").read_text(encoding="utf-8")
    js_reader = (root_res / "static" / "js" / "reader.js").read_text(encoding="utf-8")
    css_reader = (root_res / "static" / "css" / "reader.css").read_text(encoding="utf-8")
    css_admin = (root_res / "static" / "css" / "app.css").read_text(encoding="utf-8")
    check(
        "后台导航含「预约管理」入口且按权限隐藏",
        'data-view="reservations"' in html_admin and 'data-permission="loan:write" hidden' in html_admin,
    )
    check(
        "后台预约视图结构完整（页签 / 搜索 / 表格 / 分页）",
        all(x in html_admin for x in ('id="view-reservations"', 'id="reservationTabs"', 'id="reservationKeyword"', 'id="reservationTable"', 'id="reservationPager"')),
    )
    check(
        "后台预约视图调用预约接口并支持通知/取消",
        "/api/reservations" in js_admin and "data-ready" in js_admin and "data-cancel" in js_admin,
    )
    check(
        "后台预约关键字搜索已接入（防回归：计数路径需带 JOIN）",
        "keyword: s.keyword" in js_admin and "loadReservations" in js_admin,
    )
    check(
        "读者门户含我的预约 / 预约图书 / 滞纳金 / 挂失四块",
        all(
            x in html_reader
            for x in ('id="reservationBody"', 'id="reserveSearchForm"', 'id="fineBody"', 'id="payAllBtn"', 'id="lostBtn"', 'id="unlostBtn"')
        ),
    )
    check(
        "读者门户调用预约 / 缴费 / 挂失接口",
        all(
            x in js_reader
            for x in ("/api/reader/reservations", "/api/reader/fines/pay", "/api/reader/card/lost", "/api/reader/card/unlost")
        ),
    )
    check("挂失横幅有独立样式", "reader-lost" in css_reader or "lost-banner" in css_reader)
    check(
        "读者端预约支持取消、缴费支持一次结清",
        "data-cancel-res" in js_reader and 'all: true' in js_reader,
    )

    # ---- 31. 访客（未登录）能力：公告 / 办证预登记 / 借阅速查 / 馆藏分布 ----
    # 同样用独立库：公告与办证申请都是"只增不减"的事实数据，混进主库会污染
    # 前面各段的计数断言；独立库也让本段可以自由构造过期公告等时间场景。
    print("\n=== 31. 访客（未登录）能力：公告、办证预登记、借阅速查与馆藏分布 ===")
    vis_root = Path(tempfile.mkdtemp(prefix="visitor-smoke-"))
    vis_app = build_app(str(vis_root / "visitor.db"))
    vadmin = Api(vis_app.test_client())
    vadmin.csrf = (
        vadmin.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD}).get_json() or {}
    ).get("csrf_token")
    # 这两个限流器是模块级单例，跨用例共享计数；清零后本段才可重复运行
    from library.applications import service as application_service  # noqa: PLC0415

    application_service.submit_throttle.reset()
    application_service.lookup_throttle.reset()

    vadmin.post(
        "/api/books",
        json={
            "isbn": "9787506365437", "title": "活着", "author": "余华", "publisher": "作家出版社",
            "category": "文学", "publish_year": 2012, "location": "A区3排",
            "total_copies": 2, "available_copies": 2, "description": "一个人一生的故事",
        },
    )
    vadmin.post(
        "/api/books",
        json={
            "isbn": "9787115428028", "title": "算法导论", "author": "Cormen", "publisher": "人民邮电",
            "category": "计算机", "publish_year": 2013, "location": "B区1排",
            "total_copies": 1, "available_copies": 1,
        },
    )
    vadmin.post(
        "/api/books",
        json={"isbn": "9787020002207", "title": "未标架位", "category": "文学", "total_copies": 1},
    )

    # ---- 31.1 公告与开馆时间 ----
    r = vadmin.post(
        "/api/announcements",
        json={
            "title": "国庆开馆安排", "body": "10 月 1 日至 3 日闭馆", "kind": "notice",
            "level": "important", "pinned": True, "status": "published",
        },
    )
    check("馆员可新建公告（201）", r.status_code == 201, r.get_data(as_text=True))
    notice = r.get_json()
    check(
        "公告带中文标签",
        notice.get("level_label") == "重要" and notice.get("kind_label") == "公告",
        notice,
    )
    check("无时间窗的公告长期有效", notice.get("window_label") == "长期有效" and notice["effective"] is True, notice)

    r = vadmin.post(
        "/api/announcements",
        json={"title": "开馆时间", "body": "周一至周日 09:00-21:00", "kind": "hours", "status": "published"},
    )
    check("开馆时间用 kind=hours 承载（201）", r.status_code == 201, r.get_data(as_text=True))

    draft = vadmin.post("/api/announcements", json={"title": "草稿公告", "kind": "notice", "status": "draft"}).get_json()
    check("草稿公告可保存且不入门户", draft["status"] == "draft" and draft["effective"] is False, draft)

    r = vadmin.post(
        "/api/announcements",
        json={"title": "已过期公告", "kind": "notice", "status": "published", "expire_at": "2020-01-01"},
    )
    expired = r.get_json()
    check("过期公告被标记为不生效", r.status_code == 201 and expired["effective"] is False, expired)

    r = vadmin.post("/api/announcements", json={"title": "未生效公告", "publish_at": "2099-01-01"})
    check("生效时间在未来则不生效", r.status_code == 201 and r.get_json()["effective"] is False, r.get_json())

    r = vadmin.post(
        "/api/announcements", json={"title": "只给日期", "expire_at": "2099-06-30"}
    ).get_json()
    check(
        "只填日期的失效时间顺延到当天 23:59:59（否则当天凌晨就失效）",
        r["expire_at"] == "2099-06-30 23:59:59" and r["effective"] is True,
        r.get("expire_at"),
    )

    check(
        "失效早于生效被拒（400）",
        vadmin.post("/api/announcements", json={"title": "时间倒挂", "publish_at": "2026-12-01", "expire_at": "2026-01-01"}).status_code == 400,
    )
    check("标题空白被拒（400）", vadmin.post("/api/announcements", json={"title": "   "}).status_code == 400)
    check("非法类型被拒（400）", vadmin.post("/api/announcements", json={"title": "怪类型", "kind": "sos"}).status_code == 400)
    check(
        "时间格式非法被拒（400）",
        vadmin.post("/api/announcements", json={"title": "怪时间", "publish_at": "2026/01/01"}).status_code == 400,
    )

    pub = vis_app.test_client().get("/api/public/overview").get_json()
    titles = [a["title"] for a in pub["announcements"]]
    check("门户只看到生效公告", "国庆开馆安排" in titles and "开馆时间" not in titles, titles)
    check("草稿不进门户", "草稿公告" not in titles, titles)
    check("过期公告不进门户", "已过期公告" not in titles, titles)
    check("未生效公告不进门户", "未生效公告" not in titles, titles)
    check("置顶公告排最前", bool(titles) and titles[0] == "国庆开馆安排", titles)
    check("开馆时间单独返回（不混进公告栏）", (pub.get("hours") or {}).get("body") == "周一至周日 09:00-21:00", pub.get("hours"))

    lst = vadmin.get("/api/announcements", query_string={"status": "draft"}).get_json()
    check("后台按状态筛选草稿", lst["total"] == 1 and lst["items"][0]["title"] == "草稿公告", lst.get("total"))
    kw = vadmin.get("/api/announcements", query_string={"keyword": "国庆"}).get_json()
    check("后台按关键字筛选公告", kw["total"] == 1, kw.get("total"))
    kind_hours = vadmin.get("/api/announcements", query_string={"kind": "hours"}).get_json()
    check("后台按类型筛选开馆时间", kind_hours["total"] == 1, kind_hours.get("total"))
    all_ann = vadmin.get("/api/announcements", query_string={"page_size": 100}).get_json()
    check(
        "后台列表带发布/草稿统计与类型选项",
        all_ann["draft_total"] == 1 and all_ann["published_total"] >= 4 and len(all_ann["kinds"]) == 2,
        (all_ann.get("draft_total"), all_ann.get("published_total")),
    )

    r = vadmin.put(f"/api/announcements/{draft['id']}", json={"title": "草稿公告（改）", "status": "draft"})
    check("公告可编辑（PUT）", r.status_code == 200 and r.get_json()["title"] == "草稿公告（改）", r.get_data(as_text=True))
    check("编辑不存在的公告 404", vadmin.put("/api/announcements/999999", json={"title": "无"}).status_code == 404)

    r = vadmin.post(f"/api/announcements/{draft['id']}/status", json={"status": "published"})
    check("草稿可一键发布", r.status_code == 200 and r.get_json()["status"] == "published", r.get_data(as_text=True))
    check(
        "非法状态被拒（400）",
        vadmin.post(f"/api/announcements/{draft['id']}/status", json={"status": "nope"}).status_code == 400,
    )
    check("删除公告返回 200", vadmin.delete(f"/api/announcements/{draft['id']}").status_code == 200)
    check("重复删除 404", vadmin.delete(f"/api/announcements/{draft['id']}").status_code == 404)
    check(
        "发布中的公告确认已从门户消失",
        "草稿公告（改）" not in [a["title"] for a in vis_app.test_client().get("/api/public/overview").get_json()["announcements"]],
    )

    # ---- 31.2 馆藏分布导览 ----
    vpub = vis_app.test_client()
    locs = {x["location"]: x for x in vpub.get("/api/public/overview").get_json()["locations"]}
    check("按位置聚合出 A区3排", "A区3排" in locs, list(locs))
    check(
        "位置聚合带种数 / 册数 / 可借册数 / 分类",
        locs.get("A区3排", {}).get("titles") == 1
        and locs["A区3排"]["copies"] == 2
        and locs["A区3排"]["available_copies"] == 2
        and locs["A区3排"]["categories"] == ["文学"],
        locs.get("A区3排"),
    )
    check("未标架位的书单独归为一项并标记 pending", any(v["pending"] for v in locs.values()), locs)
    names = vpub.get("/api/public/overview").get_json()["location_names"]
    check("位置清单供检索下拉使用", "A区3排" in names and "B区1排" in names, names)

    r = vpub.get("/api/public/search", query_string={"location": "B区1排"}).get_json()
    check("按位置反查藏书", r["total"] == 1 and r["items"][0]["title"] == "算法导论", r.get("total"))
    check("检索回带当前位置筛选项", r.get("location") == "B区1排", r.get("location"))
    r = vpub.get("/api/public/search", query_string={"location": "__none__"}).get_json()
    check("可单独筛出「未标架位」的书", r["total"] == 1 and r["items"][0]["title"] == "未标架位", r.get("total"))
    r = vpub.get("/api/public/search", query_string={"location": "不存在的库位"}).get_json()
    check("位置无命中返回空集而非报错", r["total"] == 0 and r["items"] == [], r.get("total"))

    # ---- 31.3 在线办证预登记 ----
    options = vpub.get("/api/public/overview").get_json()["application_options"]
    check(
        "门户可取到办证表单选项（前后端同源）",
        options["member_types"] == ["VIP", "学生", "教师", "社会"] and "身份证" in options["id_types"],
        options,
    )

    r = vpub.post(
        "/api/public/member-applications",
        json={
            "name": "新读者", "phone": "13900001111", "email": "new@example.com",
            "id_type": "身份证", "id_tail": "1234", "member_type": "社会", "note": "想借计算机类",
        },
    )
    check("访客可提交办证申请（201）", r.status_code == 201, r.get_data(as_text=True))
    app_code = (r.get_json() or {}).get("code", "")
    check("申请编号形如 AC+日期-随机，并去掉易混字符", app_code.startswith("AC") and "-" in app_code, app_code)
    check("申请初始状态为待审核", r.get_json().get("status") == "pending", r.get_json())

    check(
        "同一手机号重复提交被拒（409）",
        vpub.post("/api/public/member-applications", json={"name": "重名", "phone": "13900001111"}).status_code == 409,
    )
    check("缺手机号被拒（400）", vpub.post("/api/public/member-applications", json={"name": "缺手机", "phone": ""}).status_code == 400)
    check(
        "手机号含非法字符被拒（400）",
        vpub.post("/api/public/member-applications", json={"name": "手机怪", "phone": "abc-def"}).status_code == 400,
    )
    check(
        "证件后 4 位含非法字符被拒（400）",
        vpub.post("/api/public/member-applications", json={"name": "证件怪", "phone": "13900002222", "id_tail": "AB!1"}).status_code == 400,
    )
    check(
        "非法读者类型被拒（400）",
        vpub.post("/api/public/member-applications", json={"name": "类型怪", "phone": "13900002333", "member_type": "神仙"}).status_code == 400,
    )
    check(
        "非法证件类型被拒（400）",
        vpub.post("/api/public/member-applications", json={"name": "证件类型怪", "phone": "13900002444", "id_type": "手写"}).status_code == 400,
    )

    check(
        "查询进度时手机号不符返回 404",
        vpub.post("/api/public/member-applications/lookup", json={"code": app_code, "phone": "13999999999"}).status_code == 404,
    )
    r = vpub.post("/api/public/member-applications/lookup", json={"code": app_code, "phone": "13900001111"})
    check("凭编号 + 手机号可查进度", r.status_code == 200 and r.get_json()["status"] == "pending", r.get_data(as_text=True))
    check("待审核状态不暴露证号", "card_no" not in r.get_json(), r.get_json())
    check("进度查询不回显手机号", "phone" not in r.get_json(), r.get_json())

    vapps = vadmin.get("/api/applications", query_string={"status": "pending"}).get_json()
    check("后台待审核列表可用", vapps["total"] == 1 and vapps["status_counts"]["pending"] == 1, vapps.get("total"))
    check("后台列表手机号脱敏", vapps["items"][0]["phone_masked"] == "139****1111", vapps["items"][0].get("phone_masked"))
    check("后台列表只显示证件后 4 位", vapps["items"][0]["id_tail_display"] == "****1234", vapps["items"][0].get("id_tail_display"))
    check("后台列表带读者类型选项", vapps.get("member_types") == ["VIP", "学生", "教师", "社会"], vapps.get("member_types"))
    app_id = vapps["items"][0]["id"]

    r = vadmin.post(f"/api/applications/{app_id}/approve", json={"member_type": "社会"})
    check("审核通过（200）", r.status_code == 200, r.get_data(as_text=True))
    approved = r.get_json() or {}
    card_no = approved.get("card_no", "")
    check("自动生成 R 开头的 6 位证号", card_no.startswith("R") and len(card_no) == 6, card_no)
    check("返回初始密码 = 证号后 6 位", approved.get("initial_password") == card_no[-6:], approved.get("initial_password"))
    check("申请单回填证号", (approved.get("application") or {}).get("card_no") == card_no, approved.get("application"))
    check("审核留痕审核人", bool((approved.get("application") or {}).get("reviewed_by")), approved.get("application"))
    check("新读者档案为启用状态", (approved.get("member") or {}).get("status") == "active", approved.get("member"))

    check(
        "重复审核被拒（409）",
        vadmin.post(f"/api/applications/{app_id}/approve", json={}).status_code == 409,
    )
    r = vpub.post("/api/public/member-applications/lookup", json={"code": app_code, "phone": "13900001111"})
    check("通过后访客可查到证号", r.status_code == 200 and r.get_json().get("card_no") == card_no, r.get_json())
    check(
        "已办证手机号再申请被拒（409）",
        vpub.post("/api/public/member-applications", json={"name": "又来了", "phone": "13900001111"}).status_code == 409,
    )

    vc_new = vis_app.test_client()
    r = vc_new.post("/api/auth/login", json={"identifier": card_no, "password": approved.get("initial_password")})
    check("新读者可用初始密码登录门户", r.status_code == 200 and r.get_json().get("role") == "reader", r.get_data(as_text=True))

    # 驳回流程
    r = vpub.post("/api/public/member-applications", json={"name": "被驳回", "phone": "13900003333"})
    code2 = (r.get_json() or {}).get("code", "")
    aid2 = vadmin.get("/api/applications", query_string={"status": "pending"}).get_json()["items"][0]["id"]
    check("驳回必须填原因（400）", vadmin.post(f"/api/applications/{aid2}/reject", json={"reason": "  "}).status_code == 400)
    r = vadmin.post(f"/api/applications/{aid2}/reject", json={"reason": "证件信息不完整"})
    check("驳回成功并落状态", r.status_code == 200 and r.get_json()["status"] == "rejected", r.get_data(as_text=True))
    r = vpub.post("/api/public/member-applications/lookup", json={"code": code2, "phone": "13900003333"})
    check("驳回后访客能看到原因", r.get_json().get("reject_reason") == "证件信息不完整", r.get_json())
    check(
        "已驳回的申请不能再次审核（409）",
        vadmin.post(f"/api/applications/{aid2}/approve", json={}).status_code == 409,
    )

    # 不填读者类型也必须能走完：默认取「社会」，否则建档案校验会把它挡下来
    r = vpub.post("/api/public/member-applications", json={"name": "默认类型", "phone": "13900004444"})
    check("不填读者类型也能提交", r.status_code == 201, r.get_data(as_text=True))
    aid3 = vadmin.get("/api/applications", query_string={"keyword": "13900004444"}).get_json()["items"][0]["id"]
    r = vadmin.post(f"/api/applications/{aid3}/approve", json={})
    check(
        "默认读者类型可审核通过（落「社会」）",
        r.status_code == 200 and r.get_json()["member"]["member_type"] == "社会",
        r.get_data(as_text=True),
    )
    check("申请列表可按手机号关键字过滤", vadmin.get("/api/applications", query_string={"keyword": "13900003333"}).get_json()["total"] == 1)

    # ---- 31.4 免登录借阅速查 ----
    q_member = vadmin.post(
        "/api/members",
        json={"card_no": "VS0001", "name": "速查读者", "member_type": "学生", "status": "active", "max_borrow": 5},
    ).get_json()
    qm_id = int(q_member["id"])
    qb_id = vadmin.post(
        "/api/books", json={"isbn": "9787121301234", "title": "速查用书", "category": "计算机", "total_copies": 1}
    ).get_json()["id"]
    check("前提：速查用书借出成功", vadmin.post("/api/loans/borrow", json={"book_id": qb_id, "member_id": qm_id}).status_code in (200, 201))

    qc = vis_app.test_client()  # 全新会话：用来验证速查不会建立任何身份
    check("速查缺账号被拒（400）", qc.post("/api/public/quick-check", json={"password": "x"}).status_code == 400)
    check("速查缺密码被拒（400）", qc.post("/api/public/quick-check", json={"account": "VS0001"}).status_code == 400)
    check("速查密码错误（401）", qc.post("/api/public/quick-check", json={"account": "VS0001", "password": "wrong-pass"}).status_code == 401)
    check("速查账号不存在（401）", qc.post("/api/public/quick-check", json={"account": "查无此人", "password": "x"}).status_code == 401)

    r = qc.post("/api/public/quick-check", json={"account": "VS0001", "password": "VS0001"})
    check("速查成功（200）", r.status_code == 200, r.get_data(as_text=True))
    q = r.get_json() or {}
    qm = q.get("member") or {}
    check("速查姓名脱敏", qm.get("name") == "速*读*者" or ("*" in str(qm.get("name")) and qm["name"].startswith("速")), qm)
    check("速查证号脱敏为前 2 后 2", qm.get("card_no") == "VS***01", qm.get("card_no"))
    check("速查不返回联系方式", "phone" not in qm and "email" not in qm, qm)
    check(
        "速查带汇总字段",
        {"active_loans", "overdue_loans", "quota_left", "fine_outstanding", "reservation_ready"} <= set(q.get("summary") or {}),
        q.get("summary"),
    )
    check("速查汇总反映真实在借", (q.get("summary") or {}).get("active_loans") == 1, q.get("summary"))
    check("速查带最近应还时间", bool(q.get("next_due_at")), q.get("next_due_at"))
    check("速查带回在借条目与状态标签", len(q.get("items") or []) == 1 and q["items"][0]["state_label"] == "在借", q.get("items"))
    check("速查带查询时间戳", bool(q.get("checked_at")), q.get("checked_at"))
    check("速查不建立会话（仍为未登录）", qc.get("/api/auth/me").status_code == 401)

    # 共用登录限流桶：不能把免登录速查当成绕过登录锁定的爆破口
    for _ in range(3):  # build_app 里 login_max_attempts=3
        qc.post("/api/public/quick-check", json={"account": "VS0001", "password": "bad"})
    r = qc.post("/api/public/quick-check", json={"account": "VS0001", "password": "VS0001"})
    check(
        "连续失败后速查被锁定（与登录共用计数，422）",
        r.status_code == 422 and "失败次数过多" in r.get_json()["error"]["message"],
        r.get_json(),
    )
    auth_service.login_throttle.reset()  # 清掉本段造出的锁定，避免影响后续

    # ---- 31.5 权限边界 ----
    check(
        "建馆员账号成功",
        vadmin.post(
            "/api/accounts",
            json={"username": "visclerk", "password": "visclerkpass", "role": "librarian", "display_name": "访客段馆员"},
        ).status_code in (200, 201),
    )
    lc = Api(vis_app.test_client())
    lc.csrf = (
        lc.post("/api/auth/login", json={"username": "visclerk", "password": "visclerkpass"}).get_json() or {}
    ).get("csrf_token")
    check("馆员可发公告（announcement:write）", lc.post("/api/announcements", json={"title": "馆员发的公告", "kind": "notice"}).status_code == 201)
    check("馆员可看待审办证申请（member:write）", lc.get("/api/applications").status_code == 200)
    # 上面已提交 10 次（提交限流的阈值），先清零再走一次真实的发证写路径
    application_service.submit_throttle.reset()
    r = vis_app.test_client().post(
        "/api/public/member-applications", json={"name": "馆员审核样本", "phone": "13900005555"}
    )
    check("前提：访客再提交一份申请", r.status_code == 201, r.get_data(as_text=True))
    clerk_target = vadmin.get("/api/applications", query_string={"keyword": "13900005555"}).get_json()["items"][0]["id"]
    r = lc.post(f"/api/applications/{clerk_target}/approve", json={})
    check(
        "馆员可审核发证（member:write 真正落到写路径）",
        r.status_code == 200 and str(r.get_json().get("card_no", "")).startswith("R"),
        r.get_data(as_text=True),
    )
    check("馆员无权管理后台账号（403）", lc.get("/api/accounts").status_code == 403)
    check("馆员无权查看操作日志（403）", lc.get("/api/logs").status_code == 403)
    check(
        "馆员能力集含 announcement:write（前端据此显示入口）",
        "announcement:write" in (lc.get("/api/auth/me").get_json() or {}).get("permissions", []),
        lc.get("/api/auth/me").get_json(),
    )

    vanon = vis_app.test_client()
    check("未登录读公告列表 401", vanon.get("/api/announcements").status_code == 401)
    check("未登录读办证申请 401", vanon.get("/api/applications").status_code == 401)
    check("未登录发公告 401", vanon.post("/api/announcements", json={"title": "偷偷发"}).status_code == 401)
    check("未登录审核办证 401", vanon.post(f"/api/applications/{app_id}/approve", json={}).status_code == 401)

    # ---- 31.6 前端资源契约 ----
    html_portal = (root_res / "templates" / "public.html").read_text(encoding="utf-8")
    js_portal = (root_res / "static" / "js" / "public.js").read_text(encoding="utf-8")
    css_portal = (root_res / "static" / "css" / "public.css").read_text(encoding="utf-8")
    check(
        "门户含公告栏 / 馆藏分布 / 开馆时间三块的容器",
        all(x in html_portal for x in ('id="noticeBoard"', 'id="noticeList"', 'id="locationBoard"', 'id="locationGrid"', 'id="aboutHours"')),
    )
    check(
        "门户含在线办证与借阅速查两个入口与弹窗",
        all(
            x in html_portal
            for x in ('id="applyCardBtn"', 'id="quickCheckBtn"', 'id="heroApplyBtn"', 'id="heroQuickBtn"', 'id="applyModal"', 'id="quickModal"')
        ),
    )
    check(
        "办证弹窗含提交 / 查进度两个页签与表单元素",
        all(
            x in html_portal
            for x in ('data-pk-tab="form"', 'data-pk-tab="lookup"', 'id="applyForm"', 'id="lookupForm"', 'id="applyMemberType"', 'id="applyIdType"')
        ),
    )
    check(
        "门户 JS 调用免登录写入口并支持按位置筛选",
        all(
            x in js_portal
            for x in ("/api/public/quick-check", "/api/public/member-applications", "filterByLocation", "clearLocationBtn")
        ),
    )
    check(
        "门户未加载后台样式表（新增样式必须自带）",
        "public.css" in html_portal and "app.css" not in html_portal,
    )
    check(
        "门户样式含公告 / 馆藏分布 / 弹窗三组新样式",
        all(x in css_portal for x in (".notice-list", ".location-grid", ".pk-modal", ".pk-tab", ".pk-result")),
    )
    check(
        "后台导航新增「办证审核」「公告管理」并按权限隐藏",
        all(
            x in html_admin
            for x in ('data-view="applications"', 'data-permission="member:write"', 'data-view="announcements"', 'data-permission="announcement:write"')
        ),
    )
    check(
        "后台新增两个视图且结构完整",
        all(
            x in html_admin
            for x in ('id="view-applications"', 'id="applicationTabs"', 'id="applicationTable"', 'id="view-announcements"', 'id="announcementTabs"', 'id="announcementTable"')
        ),
    )
    check(
        "后台 JS 接入公告与办证接口并支持审核/驳回/上下架",
        all(
            x in js_admin
            for x in ("loadAnnouncements", "loadApplications", "doApproveApplication", "doRejectApplication", "doAnnouncementStatus", "/api/announcements", "/api/applications")
        ),
    )

    # ==================================================================
    # 32. 读者互动：强制改密闸门、通知中心、预约保留期与改约、书架、评价、
    #     荐购登记、资料变更申请、电子借书证与 CSV 导出（第 10 批）
    # ==================================================================
    print("\n=== 32. 读者互动：闸门 / 通知 / 保留期 / 书架 / 评价 / 荐购 / 资料变更 ===")
    eng_root = Path(tempfile.mkdtemp(prefix="engagement-smoke-"))
    eng_app = build_app(str(eng_root / "eng.db"))
    eadmin = Api(eng_app.test_client())
    eadmin.csrf = (eadmin.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD}).get_json() or {}).get("csrf_token")

    def e_member(card: str, name: str, mtype: str = "学生") -> int:
        r = eadmin.post(
            "/api/members",
            json={"card_no": card, "name": name, "member_type": mtype, "status": "active", "max_borrow": 50},
        )
        assert r.status_code in (200, 201), r.get_json()
        return int(r.get_json()["id"])

    def e_book(isbn: str, title: str, total: int = 2) -> int:
        r = eadmin.post("/api/books", json={"isbn": isbn, "title": title, "category": "互动测试", "total_copies": total})
        assert r.status_code in (200, 201), r.get_json()
        return int(r.get_json()["id"])

    def e_borrow(book_id: int, member_id: int):
        return eadmin.post("/api/loans/borrow", json={"book_id": book_id, "member_id": member_id})

    def e_release_gate(member_id: int) -> None:
        with eng_app.app_context():
            db.execute("UPDATE members SET must_change_password = 0 WHERE id = ?", [member_id])

    def e_reader(card: str, password: str = ""):
        c = eng_app.test_client()
        r = c.post("/api/reader/login", json={"account": card, "password": password or card})
        assert r.status_code == 200, r.get_json()
        return c, {"X-CSRF-Token": (r.get_json() or {}).get("csrf_token")}

    m_pong = e_member("EN0001", "互动测试甲")
    m_ping = e_member("EN0002", "互动测试乙", "教师")
    b_review = e_book("9787020002207", "红楼梦（互动）")
    b_fav = e_book("9787506365437", "活着（互动）")
    b_hold = e_book("9787115428028", "算法导论（互动）", 1)
    b_ready = e_book("9787108009821", "万历十五年（互动）", 1)

    # ---- 32.1 强制改密闸门 ----
    rpong, hpong = e_reader("EN0001")
    check("闸门期间评价接口被拦", rpong.post("/api/reader/reviews", json={}, headers=hpong).status_code == 403)
    check("闸门期间收藏接口被拦", rpong.post("/api/reader/favorites", json={}, headers=hpong).status_code == 403)
    check("闸门期间荐购接口被拦", rpong.post("/api/reader/book-requests", json={}, headers=hpong).status_code == 403)
    check("闸门期间资料变更接口被拦", rpong.post("/api/reader/profile-requests", json={}, headers=hpong).status_code == 403)
    check("闸门期间改约接口被拦", rpong.post("/api/reader/reservations/1/rebook", json={}, headers=hpong).status_code == 403)
    bad = rpong.post("/api/reader/password", json={"old_password": "错密码", "new_password": "newpass123"}, headers=hpong)
    check("闸门改密填错旧密码被拒并指明字段", bad.status_code == 400 and bad.get_json()["error"]["details"]["field"] == "old_password", bad.get_json())
    same = rpong.post("/api/reader/password", json={"old_password": "EN0001", "new_password": "EN0001"}, headers=hpong)
    check("闸门改密新旧相同被拒", same.status_code == 400 and same.get_json()["error"]["details"]["field"] == "new_password", same.get_json())
    okpwd = rpong.post("/api/reader/password", json={"old_password": "EN0001", "new_password": "newpass123"}, headers=hpong)
    check("闸门改密成功", okpwd.status_code == 200, okpwd.get_json())
    with eng_app.app_context():
        flag = db.query_one("SELECT must_change_password FROM members WHERE id = ?", [m_pong])
    check("改密后解除「首次登录须改密」标记", flag and not int(flag["must_change_password"]), flag)
    check("改密后原会话被强制退出", rpong.get("/api/reader/me").status_code == 401)
    relogin = eng_app.test_client().post("/api/reader/login", json={"account": "EN0001", "password": "newpass123"})
    check("改密后可用新口令登录", relogin.status_code == 200, relogin.get_json())
    check("改密后标记不再出现", relogin.get_json()["member"]["must_change_password"] is False, relogin.get_json()["member"])

    # ---- 32.2 借阅与预约底座 ----
    e_release_gate(m_ping)
    loan_rev = e_borrow(b_review, m_pong)
    eadmin.post(f"/api/loans/{loan_rev.get_json()['id']}/return")
    loan_fav = e_borrow(b_fav, m_pong)
    eadmin.post(f"/api/loans/{loan_fav.get_json()['id']}/return")
    e_borrow(b_hold, m_ping)  # 占满唯一复本 → 甲可预约
    e_borrow(b_ready, m_ping)  # 同上
    rpong, hpong = e_reader("EN0001", "newpass123")
    rping, hping = e_reader("EN0002")

    res_wait = rpong.post("/api/reader/reservations", json={"book_id": b_hold}, headers=hpong)
    check("预约已借完的图书成功", res_wait.status_code in (200, 201), res_wait.get_json())
    res_ready = rpong.post("/api/reader/reservations", json={"book_id": b_ready}, headers=hpong)
    check("第二条预约进入排队", res_ready.status_code in (200, 201), res_ready.get_json())
    ready_id = int(res_ready.get_json()["id"])
    marked = eadmin.post(f"/api/reservations/{ready_id}/ready")
    check("馆员通知到书", marked.status_code == 200 and marked.get_json()["status"] == "ready", marked.get_json())
    decorated = marked.get_json()
    check(
        "已到书预约带取书保留期倒计时",
        decorated.get("expires_at") and decorated.get("hold_left_days") is not None and "剩" in (decorated.get("hold_label") or ""),
        decorated,
    )
    check("保留期来自配置（3 天）", int(decorated.get("hold_days") or 0) == 3, decorated.get("hold_days"))

    # ---- 32.3 我的书架（收藏） ----
    fav = rpong.post("/api/reader/favorites", json={"book_id": b_fav, "note": "寒假读完"}, headers=hpong)
    check("收藏图书成功", fav.status_code == 201 and fav.get_json()["note"] == "寒假读完", fav.get_json())
    check("收藏条目带馆藏状态", fav.get_json().get("state_label", "").startswith("可借"), fav.get_json())
    check("收藏条目不外泄 member_id", "member_id" not in fav.get_json(), list(fav.get_json().keys()))
    dup = rpong.post("/api/reader/favorites", json={"book_id": b_fav}, headers=hpong)
    check("重复收藏被 409 拦下", dup.status_code == 409, dup.get_json())
    # 回归：备注修改与移除都要走 get_favorite()，而它此前漏 SELECT member_id 导致 500
    upd = rpong.put(f"/api/reader/favorites/{fav.get_json()['id']}", json={"note": "改成下学期读"}, headers=hpong)
    check("修改收藏备注成功（回归：member_id 缺列曾致 500）", upd.status_code == 200 and upd.get_json()["note"] == "改成下学期读", upd.get_json())
    listed = rpong.get("/api/reader/favorites", query_string={"keyword": "活着"})
    check("书架支持关键词检索", listed.status_code == 200 and listed.get_json()["total"] == 1, listed.get_json())
    # 他人书架条目按「不存在」处理，不区分 404 语义
    foreign = rpong.put("/api/reader/favorites/999999", json={"note": "x"}, headers=hpong)
    check("修改他人/不存在的收藏返回 404", foreign.status_code == 404, foreign.status_code)

    # ---- 32.4 评价 ----
    denied = rpong.post("/api/reader/reviews", json={"book_id": b_hold, "rating": 5}, headers=hpong)
    check("没借过的书不能评价", denied.status_code == 422 and "才能评价" in denied.get_json()["error"]["message"], denied.get_json())
    rev = rpong.post("/api/reader/reviews", json={"book_id": b_review, "rating": 4, "content": "常读常新"}, headers=hpong)
    check("借过并归还的书可评价", rev.status_code in (200, 201) and rev.get_json()["review"]["rating"] == 4, rev.get_json())
    again = rpong.post("/api/reader/reviews", json={"book_id": b_review, "rating": 5, "content": "再读一遍"}, headers=hpong)
    check("同一本书再次评价为覆盖", again.status_code == 200 and again.get_json()["created"] is False and again.get_json()["review"]["rating"] == 5, again.get_json())
    check("评价覆盖后评分聚合取最新值", again.get_json()["summary"]["avg"] == 5.0, again.get_json()["summary"])
    pend = rpong.get("/api/reader/reviews/pending")
    check("待评价清单来自已归还借阅", pend.status_code == 200 and any(i["book_id"] == b_fav for i in pend.get_json()["items"]), pend.get_json())
    check("已评价的书不再出现在待评价里", all(i["book_id"] != b_review for i in pend.get_json()["items"]), pend.get_json())

    rping, hping = e_reader("EN0002")
    loan_p = e_borrow(b_review, m_ping)
    eadmin.post(f"/api/loans/{loan_p.get_json()['id']}/return")
    rping.post("/api/reader/reviews", json={"book_id": b_review, "rating": 3, "content": "一般"}, headers=hping)
    mylist = rpong.get("/api/reader/reviews", query_string={"page": 1, "page_size": 10})
    check("我的评价只含本人记录", mylist.status_code == 200 and all(i["member_card"] == "EN0001" for i in mylist.get_json()["items"]), mylist.get_json())
    pub = eng_app.test_client().get(f"/api/public/books/{b_review}/reviews")
    displays = [i["member_display"] for i in pub.get_json()["items"]]
    check(
        "门户评价列表只露姓氏（完整姓名不出现在响应里）",
        pub.status_code == 200 and len(displays) == 2 and all("*" in d and len(d) >= 2 for d in displays),
        displays,
    )
    check("门户评价响应不含读者证号", "member_card" not in pub.get_data(as_text=True), None)
    check("门户评价聚合条数正确", pub.get_json()["summary"]["total"] == 2, pub.get_json()["summary"])

    # ---- 32.5 荐购登记 ----
    req = rpong.post(
        "/api/reader/book-requests",
        json={"title": "置身事内", "author": "兰小欢", "isbn": "9787208171789", "reason": "课程参考"},
        headers=hpong,
    )
    check("荐购登记成功", req.status_code == 201 and req.get_json()["status_label"] == "待处理", req.get_json())
    noreply = eadmin.post(f"/api/engagement/book-requests/{req.get_json()['id']}/review", json={"status": "rejected"})
    check("未采纳必须填写答复", noreply.status_code == 400 and noreply.get_json()["error"]["details"]["field"] == "reply", noreply.get_json())
    rejected = eadmin.post(f"/api/engagement/book-requests/{req.get_json()['id']}/review", json={"status": "rejected", "reply": "已有馆藏副本"})
    check("驳回后状态流转并记录答复", rejected.status_code == 200 and rejected.get_json()["status"] == "rejected", rejected.get_json())
    myreqs = rpong.get("/api/reader/book-requests", query_string={"page": 1})
    check("读者能看到处理结论与答复", myreqs.get_json()["items"][0]["reply"] == "已有馆藏副本", myreqs.get_json()["items"])

    # ---- 32.6 资料变更申请 ----
    pr = rpong.post("/api/reader/profile-requests", json={"field": "name", "new_value": "互动测试甲乙", "reason": "改名"}, headers=hpong)
    check("提交姓名变更申请", pr.status_code == 201 and pr.get_json()["status_label"] == "待审核", pr.get_json())
    prdup = rpong.post("/api/reader/profile-requests", json={"field": "name", "new_value": "另一个名字"}, headers=hpong)
    check("同字段重复申请被 409 拦下", prdup.status_code == 409, prdup.get_json())
    prsame = rpong.post("/api/reader/profile-requests", json={"field": "member_type", "new_value": "学生"}, headers=hpong)
    check("新值与当前值相同被拒", prsame.status_code == 400 and prsame.get_json()["error"]["details"]["field"] == "new_value", prsame.get_json())
    badfield = rpong.post("/api/reader/profile-requests", json={"field": "phone", "new_value": "13800000000"}, headers=hpong)
    check("手机号不允许走审批（应自助修改）", badfield.status_code == 400, badfield.get_json())
    check("回归：资料变更返回 status_label（此前缺失导致前端标签为空）", bool(pr.get_json().get("status_label")), pr.get_json())
    no_reason = eadmin.post(f"/api/engagement/profile-requests/{pr.get_json()['id']}/review", json={"approve": False})
    check("驳回资料变更必须说明原因", no_reason.status_code == 400 and no_reason.get_json()["error"]["details"]["field"] == "reply", no_reason.get_json())
    approved = eadmin.post(f"/api/engagement/profile-requests/{pr.get_json()['id']}/review", json={"approve": True, "reply": "已核对"})
    check("通过后状态流转", approved.status_code == 200 and approved.get_json()["status"] == "approved", approved.get_json())
    mem_after = eadmin.get(f"/api/members/{m_pong}").get_json()
    check("通过后读者资料已回写", mem_after["name"] == "互动测试甲乙", mem_after)
    # 回归：收藏的 PUT/DELETE 与资料变更列表都在同一条仓储 SELECT 上
    fav_del = rpong.delete(f"/api/reader/favorites/{fav.get_json()['id']}", headers=hpong)
    check("移除收藏成功（回归：member_id 缺列曾致 500）", fav_del.status_code == 200, fav_del.get_json())

    # ---- 32.7 站内通知中心 ----
    notif = rpong.get("/api/reader/notifications", query_string={"kind": "all", "page": 1})
    check("通知中心可访问且先做惰性扫描", notif.status_code == 200 and "unread" in notif.get_json(), notif.status_code)
    kinds = {i["kind"] for i in notif.get_json()["items"]}
    check("审核结果会写入读者通知", "profile" in kinds and "book_request" in kinds, kinds)
    check("预约到书会写入读者通知", "reservation_ready" in kinds, kinds)
    unread_before = int(notif.get_json()["unread"])
    first_unread = next(i for i in notif.get_json()["items"] if not i["read"])
    mark = rpong.post(f"/api/reader/notifications/{first_unread['id']}/read", headers=hpong)
    check("单条标记已读后未读数递减", mark.status_code == 200 and mark.get_json()["unread"] == unread_before - 1, mark.get_json())
    foreign_notif = rpong.post("/api/reader/notifications/999999/read", headers=hpong)
    check("标记他人/不存在通知返回 404", foreign_notif.status_code == 404, foreign_notif.status_code)
    readall = rpong.post("/api/reader/notifications/read-all", headers=hpong)
    check("全部标记已读后未读清零", readall.status_code == 200 and readall.get_json()["unread"] == 0, readall.get_json())
    sent = eadmin.post(f"/api/members/{m_pong}/notify", json={"title": "请尽快归还", "body": "《算法导论》已逾期", "link_view": "loans"})
    check("馆员可向读者发站内通知", sent.status_code == 201 and sent.get_json()["kind"] == "system", sent.get_json())
    badlink = eadmin.post(f"/api/members/{m_pong}/notify", json={"title": "x", "link_view": "http://evil.example"})
    check("通知跳转目标必须是白名单视图", badlink.status_code == 400 and badlink.get_json()["error"]["details"]["field"] == "link_view", badlink.get_json())
    me_after = rpong.get("/api/reader/me").get_json()
    check("概览返回未读数与保留期配置", "notifications" in me_after and me_after["rules"].get("reservation_hold_days") == 3, me_after.get("rules"))
    check("概览不下发密码哈希", "password_hash" not in me_after["member"], list(me_after["member"].keys()))

    # ---- 32.8 预约改约 ----
    b_swap = e_book("9787532733143", "枪炮病菌与钢铁（互动）", 1)
    loan_s = e_borrow(b_swap, m_ping)  # 占满唯一复本，甲才能改约到它
    rebook_same = rpong.post(f"/api/reader/reservations/{ready_id}/rebook", json={"book_id": b_ready}, headers=hpong)
    check("改约到同一本书被拒", rebook_same.status_code == 409, rebook_same.get_json())
    rebook = rpong.post(f"/api/reader/reservations/{ready_id}/rebook", json={"book_id": b_swap}, headers=hpong)
    check(
        "改约成功并返回新预约（关旧单 + 开新单同事务）",
        rebook.status_code == 200 and rebook.get_json()["closed_id"] == ready_id and rebook.get_json()["reservation"]["book_id"] == b_swap,
        rebook.get_json(),
    )
    with eng_app.app_context():
        old_status = db.query_one("SELECT status FROM reservations WHERE id = ?", [ready_id])
    check("原预约被关闭而不是删除", old_status and old_status["status"] == "cancelled", old_status)
    foreign_res = rping.post(f"/api/reader/reservations/{rebook.get_json()['reservation']['id']}/rebook", json={"book_id": b_hold}, headers=hping)
    check("不能改约他人的预约", foreign_res.status_code in (403, 404, 409), foreign_res.status_code)

    # ---- 32.9 前端资源契约（读者门户 + 后台互动视图） ----
    check(
        "读者页含改密遮罩 / 铃铛 / 消息中心 / 书架 / 评价 / 荐购 / 资料变更 / 借书证",
        all(
            x in html_reader
            for x in ('id="pwGate"', 'id="bellBtn"', 'id="notifyList"', 'id="shelfGrid"', 'id="reviewableList"', 'id="bookRequestForm"', 'id="profileRequestForm"', 'id="eCard"')
        ),
    )
    check(
        "读者页引入自实现的条码与二维码脚本",
        '/static/js/barcode.js' in html_reader and '/static/js/qrcode.js' in html_reader,
    )
    check(
        "读者 JS 接入全部新接口",
        all(
            x in js_reader
            for x in (
                "/api/reader/notifications",
                "/api/reader/favorites",
                "/api/reader/reviews",
                "/api/reader/book-requests",
                "/api/reader/profile-requests",
                "/rebook",
                "downloadCsv",
                "QRCode",
                "Barcode",
            )
        ),
    )
    check("读者样式含遮罩 / 铃铛 / 书架 / 弹窗 / 借书证", all(x in css_reader for x in (".pw-gate", ".bell-badge", ".shelf-card", ".dlg-mask", ".e-card", ".hold-note")))
    check(
        "后台新增「读者互动」入口与视图结构",
        all(
            x in html_admin
            for x in ('data-view="engagement"', 'id="view-engagement"', 'id="engagementKindTabs"', 'id="engagementStatusTabs"', 'id="engagementTable"', 'id="engagementPending"')
        ),
    )
    check(
        "后台 JS 接入互动审核与读者发通知",
        all(x in js_admin for x in ("loadEngagement", "setReviewStatus", "reviewBookRequest", "reviewProfileRequest", "openMemberNotify", "/api/engagement/")),
    )
    check(
        "二维码脚本自检可用（无第三方依赖）",
        "selfTest" in (root_res / "static" / "js" / "qrcode.js").read_text(encoding="utf-8")
        and "QRCode" in (root_res / "static" / "js" / "qrcode.js").read_text(encoding="utf-8"),
    )

    # ==================================================================
    # 33. 流通馆员规模化作业（第 11 批）
    #     批量导入（模板 / 预览 / 逐行报错 / 唯一冲突 / 逐行独立事务）
    #     批量催还（计数与幂等）、六个视图 CSV 导出契约
    #     赔偿处理（登记 / 重复 409 / 结案联动库存与罚金 / 注销）
    #     盘点（开单快照 / 实盘 / 差异 / 调账校验 / 关闭冻结）
    #     采编验收（ISBN 去重加册与新建）、打印排版、读者附件上传、定时释放
    # ==================================================================
    print("\n=== 33. 流通馆员规模化作业：导入 / 催还 / 赔偿 / 盘点 / 采编 / 附件 / 打印 ===")
    import config as b11_config  # noqa: PLC0415 - 仅本段使用

    with b11_env(
        LIBRARY_SWEEP_INTERVAL_SECONDS="900",
        LIBRARY_UPLOAD_DIR=str(Path(tempfile.gettempdir()) / "b11-uploads"),
        LIBRARY_UPLOAD_MAX_MB="8",
    ):
        b11_loaded = b11_config.load_config()
    check(
        "新增三项配置可从环境变量读取",
        b11_loaded.sweep_interval_seconds == 900
        and b11_loaded.upload_max_mb == 8
        and b11_loaded.upload_dir.endswith("b11-uploads"),
        (b11_loaded.sweep_interval_seconds, b11_loaded.upload_max_mb, b11_loaded.upload_dir),
    )
    with b11_env(LIBRARY_SWEEP_INTERVAL_SECONDS="0"):
        check(
            "扫描间隔 0 表示退回纯惰性模式",
            b11_config.load_config().sweep_interval_seconds == 0,
        )
    with b11_env(LIBRARY_SWEEP_INTERVAL_SECONDS="100000"):
        check("扫描间隔超上限被拒绝启动", config_rejected(b11_config))

    b11_root = Path(tempfile.mkdtemp(prefix="batch11-smoke-"))
    b11_cfg = Config(
        db_path=str(b11_root / "b11.db"),
        host="127.0.0.1",
        port=8000,
        debug=False,
        loan_days=30,
        max_renew=2,
        fine_per_day=0.5,
        default_max_borrow=50,
        log_level="WARNING",
        secret_key="test-secret-b11",
        library_name="测试图书馆",
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PWD,
        login_max_attempts=3,
        login_lock_minutes=5,
        cookie_secure=False,
        # 上传目录随临时库走，避免污染工作区；1MB 上限让「超限」用例不必造大文件
        upload_dir=str(b11_root / "uploads"),
        upload_max_mb=1,
    )
    b11_app = create_app(b11_cfg)
    b11 = Api(b11_app.test_client())
    b11.csrf = (
        b11.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD}).get_json() or {}
    ).get("csrf_token")

    with b11_app.app_context():
        b11_tables = {
            row["name"] for row in db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    b11_new_tables = {"compensations", "stocktaking_batches", "stocktaking_items", "acquisitions", "member_files"}
    check("新增五张表（赔偿 / 盘点单 / 盘点明细 / 采编 / 附件）", b11_new_tables <= b11_tables, sorted(b11_new_tables - b11_tables))

    def b11_member(card, name, mtype="学生"):
        r = b11.post(
            "/api/members",
            json={"card_no": card, "name": name, "member_type": mtype, "status": "active", "max_borrow": 50},
        )
        assert r.status_code in (200, 201), r.get_json()
        return int(r.get_json()["id"])

    def b11_book(isbn, title, total=1, location=""):
        r = b11.post(
            "/api/books",
            json={"isbn": isbn, "title": title, "category": "第11批", "total_copies": total, "location": location},
        )
        assert r.status_code in (200, 201), r.get_json()
        return int(r.get_json()["id"])

    def b11_borrow(book_id, member_id):
        r = b11.post("/api/loans/borrow", json={"book_id": book_id, "member_id": member_id})
        assert r.status_code in (200, 201), r.get_json()
        return int(r.get_json()["id"])

    def b11_backdate(loan_id, days=10):
        """把某笔借阅的应还日期回填到 N 天前（造逾期，不动借出时间）。"""
        with b11_app.app_context():
            db.execute(
                "UPDATE loans SET due_at = datetime('now', 'localtime', ?) WHERE id = ?",
                [f"-{int(days)} days", int(loan_id)],
            )

    def b11_total(book_id):
        return int(b11.get(f"/api/books/{book_id}").get_json()["total_copies"])

    def b11_available(book_id):
        return int(b11.get(f"/api/books/{book_id}").get_json()["available_copies"])

    def b11_members_total():
        return int(b11.get("/api/members").get_json()["total"])

    def b11_books_total():
        return int(b11.get("/api/books").get_json()["total"])

    # ---- 33.1 批量导入：模板 ----
    tpl_books = b11.get("/api/import/books/template")
    body_books = tpl_books.get_data()
    check(
        "图书导入模板可下载且是带 BOM 的 CSV",
        tpl_books.status_code == 200
        and body_books.startswith("\ufeff".encode("utf-8"))
        and "text/csv" in tpl_books.headers.get("Content-Type", ""),
        tpl_books.headers.get("Content-Type"),
    )
    check(
        "图书模板表头与解析器一致（服务端生成，不会各说各话）",
        all(x in body_books.decode("utf-8-sig") for x in ("书名", "ISBN", "总册数", "馆藏位置", "馆藏条码")),
        body_books.decode("utf-8-sig")[:60],
    )
    tpl_members = b11.get("/api/import/members/template")
    check(
        "读者导入模板含必需列与示例行",
        tpl_members.status_code == 200
        and tpl_members.get_data().decode("utf-8-sig").startswith("姓名,读者证号")
        and "示例：张三" in tpl_members.get_data().decode("utf-8-sig"),
    )

    # ---- 33.2 批量导入：解析与逐行报错 ----
    empty = b11.post("/api/import/books", json={"csv": "   ", "dry_run": True})
    check(
        "空内容导入被 400 拦下",
        empty.status_code == 400 and empty.get_json()["error"]["details"]["field"] == "csv",
        empty.get_json(),
    )
    missing_col = b11.post("/api/import/books", json={"csv": "书名,作者\n测试,某人\n", "dry_run": True})
    check(
        "缺少必需列（ISBN）时明确点名缺哪一列",
        missing_col.status_code == 400
        and "isbn" in missing_col.get_json()["error"]["message"].lower()
        and missing_col.get_json()["error"]["details"]["columns"] == ["isbn"],
        missing_col.get_json(),
    )

    books_before = b11_books_total()
    csv_preview = (
        "书名,ISBN,作者,总册数,馆藏位置\n"
        "导入测试一,9787000000001,甲,2,A区-9排\n"
        "导入测试坏行,123,乙,1,A区-9排\n"
        "导入测试重复,9787000000001,丙,1,A区-9排\n"
    )
    preview = b11.post("/api/import/books", json={"csv": csv_preview, "dry_run": True})
    prev_json = preview.get_json()
    check(
        "预览返回逐行结果与总计",
        preview.status_code == 200
        and prev_json["total"] == 3
        and prev_json["created"] == 0
        and [r["status"] for r in prev_json["rows"]] == ["ok", "failed", "failed"],
        prev_json,
    )
    check(
        "预览逐行标出行号与原因（ISBN 位数不合法）",
        prev_json["rows"][1]["line"] == 3 and "10 位或 13 位" in prev_json["rows"][1]["message"],
        prev_json["rows"][1],
    )
    check(
        "文件内重复被单独点出（不与「库里已有」混淆）",
        "文件内重复" in prev_json["rows"][2]["message"],
        prev_json["rows"][2],
    )
    check("预览不写库", b11_books_total() == books_before, b11_books_total())

    # ---- 33.3 批量导入：正式写入与逐行独立 ----
    real = b11.post("/api/import/books", json={"csv": csv_preview})
    real_json = real.get_json()
    check(
        "正式导入落库：成功 1 行、失败 2 行",
        real.status_code == 200 and real_json["created"] == 1 and real_json["failed"] == 2 and real_json["ok"] is False,
        real_json,
    )
    check("导入后馆藏总数 +1", b11_books_total() == books_before + 1, b11_books_total())
    dup_db = b11.post(
        "/api/import/books",
        json={"csv": "书名,ISBN\n另起书名,9787000000001\n", "dry_run": False},
    )
    check(
        "与库中已有 ISBN 冲突时报「已存在」（不是 500）",
        dup_db.status_code == 200
        and dup_db.get_json()["failed"] == 1
        and "已存在" in dup_db.get_json()["rows"][0]["message"],
        dup_db.get_json(),
    )
    check("冲突行不落库（总数不变）", b11_books_total() == books_before + 1, b11_books_total())

    members_before = b11_members_total()
    csv_members = (
        "姓名,读者证号,读者类型,手机号\n"
        "导入读者甲,I0001,学生,13900000001\n"
        "导入读者乙,I0001,教师,13900000002\n"
        "导入读者丙,I0003,学生,13900000001\n"
    )
    mem_prev = b11.post("/api/import/members", json={"csv": csv_members, "dry_run": True}).get_json()
    check(
        "读者导入预览：证号重复与手机号重复分别拦截",
        mem_prev["total"] == 3
        and mem_prev["failed"] == 2
        and "读者证号" in mem_prev["rows"][1]["message"]
        and "手机号" in mem_prev["rows"][2]["message"],
        mem_prev["rows"],
    )
    mem_real = b11.post("/api/import/members", json={"csv": csv_members}).get_json()
    check(
        "读者导入正式执行：1 行成功 2 行失败（单行失败不拖垮整批）",
        mem_real["created"] == 1 and mem_real["failed"] == 2,
        mem_real,
    )
    check("读者总数 +1", b11_members_total() == members_before + 1, b11_members_total())

    # ---- 33.4 批量催还 ----
    m_ov1 = b11_member("B11B001", "催还测试甲")
    m_ov2 = b11_member("B11B002", "催还测试乙")
    b_ov1 = b11_book("9787000000011", "催还测试书一", 1)
    b_ov2 = b11_book("9787000000012", "催还测试书二", 1)
    b_ok = b11_book("9787000000013", "催还测试书三", 1)
    b_done = b11_book("9787000000014", "催还测试书四", 1)
    loan_ov1 = b11_borrow(b_ov1, m_ov1)
    loan_ov2 = b11_borrow(b_ov2, m_ov2)
    loan_ok = b11_borrow(b_ok, m_ov1)
    loan_done = b11_borrow(b_done, m_ov2)
    b11.post(f"/api/loans/{loan_done}/return")
    b11_backdate(loan_ov1, 12)
    b11_backdate(loan_ov2, 7)
    b11_backdate(loan_done, 20)  # 已归还，不算「待催」
    overdue_total = int(b11.get("/api/loans", query_string={"status": "overdue", "page_size": 100}).get_json()["total"])
    check("逾期口径：只有未归还且过应还日的计入", overdue_total == 2, overdue_total)

    first_notify = b11.post("/api/loans/notify-overdue", json={})
    notify_json = first_notify.get_json()
    check(
        "批量催还按当前逾期清单发送",
        first_notify.status_code == 200 and notify_json["sent"] == overdue_total and notify_json["members"] == 2,
        notify_json,
    )
    again_notify = b11.post("/api/loans/notify-overdue", json={}).get_json()
    check(
        "重复催还幂等（同一借阅同一应还日不刷屏）",
        again_notify["sent"] == 0 and again_notify["skipped"] == overdue_total,
        again_notify,
    )
    done_notify = b11.post("/api/loans/notify-overdue", json={"ids": [loan_done]})
    check(
        "对已归还借阅催还被 422 拒",
        done_notify.status_code == 422 and "已归还" in done_notify.get_json()["error"]["message"],
        done_notify.get_json(),
    )
    fresh_notify = b11.post("/api/loans/notify-overdue", json={"ids": [loan_ok]})
    check(
        "对未逾期借阅催还被 422 拒",
        fresh_notify.status_code == 422 and "尚未逾期" in fresh_notify.get_json()["error"]["message"],
        fresh_notify.get_json(),
    )
    with b11_app.app_context():
        notif_rows = db.query_all(
            "SELECT COUNT(*) AS n FROM notifications WHERE kind = 'overdue' AND ref_type = 'loan'"
        )
    check("催还通知落库条数与发送数一致", int(notif_rows[0]["n"]) == overdue_total, notif_rows)

    # ---- 33.5 赔偿处理 ----
    m_cp = b11_member("B11C001", "赔偿测试甲")
    b_lost = b11_book("9787000000021", "赔偿测试书（丢失）", 3)
    b_dmg = b11_book("9787000000022", "赔偿测试书（损毁）", 2)
    loan_lost = b11_borrow(b_lost, m_cp)
    loan_dmg = b11_borrow(b_dmg, m_cp)
    # 先借完再回填逾期：读者一旦有逾期未还，后续借书会被业务规则拦下
    loan_ret = b11_borrow(b11_book("9787000000023", "赔偿测试书（已还）", 1), m_cp)
    b11.post(f"/api/loans/{loan_ret}/return")
    b11_backdate(loan_lost, 10)
    b11_backdate(loan_dmg, 4)

    bad_amount = b11.post("/api/compensations", json={"loan_id": loan_lost, "kind": "lost", "amount": 0})
    check(
        "赔偿金额为 0 被 400 拒并指明字段",
        bad_amount.status_code == 400 and bad_amount.get_json()["error"]["details"]["field"] == "amount",
        bad_amount.get_json(),
    )
    bad_kind = b11.post("/api/compensations", json={"loan_id": loan_lost, "kind": "eaten", "amount": 10})
    check("赔偿类型非法被 400 拒", bad_kind.status_code == 400, bad_kind.get_json())

    reg_lost = b11.post(
        "/api/compensations",
        json={"loan_id": loan_lost, "kind": "lost", "amount": 59.8, "note": "读者确认遗失"},
    )
    comp_lost = reg_lost.get_json()
    check(
        "登记丢书赔偿成功并带书名与读者（列表可直接展示）",
        reg_lost.status_code == 201
        and comp_lost["status"] == "pending"
        and comp_lost["amount"] == 59.8
        and "赔偿测试书（丢失）" in comp_lost["book_title"],
        comp_lost,
    )
    dup_reg = b11.post("/api/compensations", json={"loan_id": loan_lost, "kind": "lost", "amount": 20})
    check("同一借阅重复登记赔付被 409 拦下", dup_reg.status_code == 409, dup_reg.get_json())

    ret_lost = b11.post("/api/compensations", json={"loan_id": loan_ret, "kind": "lost", "amount": 30})
    check(
        "已归还的借阅不能按「丢失」登记（提示改选损毁）",
        ret_lost.status_code == 422 and "图书损毁" in ret_lost.get_json()["error"]["message"],
        ret_lost.get_json(),
    )

    reg_dmg = b11.post("/api/compensations", json={"loan_id": loan_dmg, "kind": "damaged", "amount": 18.0})
    comp_dmg = reg_dmg.get_json()
    check("损毁赔偿在借状态下可登记", reg_dmg.status_code == 201 and comp_dmg["kind_label"] == "图书损毁", comp_dmg)

    listed = b11.get("/api/compensations").get_json()
    check(
        "赔偿列表按状态可控且带汇总",
        listed["total"] == 2
        and listed["summaries"]["pending_amount"] == 77.8
        and set(listed["kind_labels"]) == {"lost", "damaged"},
        listed["summaries"],
    )
    over_paid = b11.post(f"/api/compensations/{comp_lost['id']}/settle", json={"paid_amount": 100})
    check(
        "实收超过应赔被 422 拒",
        over_paid.status_code == 422 and "不能超过" in over_paid.get_json()["error"]["message"],
        over_paid.get_json(),
    )

    outstanding = b11.get(f"/api/fines/member/{m_cp}").get_json()["items"]
    owed_lost = next((i["outstanding"] for i in outstanding if i["loan_id"] == loan_lost), 0.0)
    check("逾期未还借阅确实产生待缴滞纳金（供赔偿联动减免）", owed_lost > 0, outstanding)

    total_before = b11_total(b_lost)
    avail_before = b11_available(b_lost)
    settle_lost = b11.post(
        f"/api/compensations/{comp_lost['id']}/settle",
        json={"note": "全额赔付", "close_loan": True, "waive_fine": True},
    )
    settle_json = settle_lost.get_json()
    check(
        "按丢失结案：借阅置为已归还、赔偿单结案",
        settle_lost.status_code == 200 and settle_json["status"] == "settled" and settle_json["closed_loan"] is True,
        settle_json,
    )
    check(
        "按丢失结案：总册数 -1 且可借数不变（书不在架，不能回补库存）",
        b11_total(b_lost) == total_before - 1 and b11_available(b_lost) == avail_before,
        (b11_total(b_lost), b11_available(b_lost), total_before, avail_before),
    )
    check(
        "结案时同步减免该笔逾期滞纳金（金额与应缴一致）",
        abs(float(settle_json["waived_fine"]) - float(owed_lost)) < 0.005,
        (settle_json["waived_fine"], owed_lost),
    )
    after_waive = b11.get(f"/api/fines/member/{m_cp}").get_json()["items"]
    check("减免后该笔不再出现在待缴清单", all(i["loan_id"] != loan_lost for i in after_waive), after_waive)

    re_settle = b11.post(f"/api/compensations/{comp_lost['id']}/settle", json={})
    check("已结案的赔偿单不能重复结案（409）", re_settle.status_code == 409, re_settle.get_json())
    re_cancel = b11.post(f"/api/compensations/{comp_lost['id']}/cancel", json={})
    check("已结案的赔偿单不能注销（409）", re_cancel.status_code == 409, re_cancel.get_json())
    cancel_dmg = b11.post(f"/api/compensations/{comp_dmg['id']}/cancel", json={"note": "读者已修复赔书"})
    check(
        "待结案赔偿单可注销",
        cancel_dmg.status_code == 200 and cancel_dmg.get_json()["status"] == "cancelled",
        cancel_dmg.get_json(),
    )
    missing_cp = b11.get("/api/compensations/999999")
    check("不存在的赔偿单返回 404", missing_cp.status_code == 404, missing_cp.status_code)

    # ---- 33.6 盘点：开单快照 → 实盘 → 差异 → 关闭 → 调账 ----
    b_st1 = b11_book("9787000000031", "盘点书一", 3, location="P区-1排")
    b_st2 = b11_book("9787000000032", "盘点书二", 2, location="P区-1排")
    b_st3 = b11_book("9787000000033", "盘点书三", 1, location="P区-2排")

    empty_scope = b11.post("/api/stocktaking", json={"location": "不存在的馆位"})
    check(
        "空范围开单被 422 拒",
        empty_scope.status_code == 422 and "没有可盘点" in empty_scope.get_json()["error"]["message"],
        empty_scope.get_json(),
    )
    batch = b11.post("/api/stocktaking", json={"location": "P区-1排", "note": "季度盘点"}).get_json()
    check(
        "按馆位开单：单号规范、明细为开单时的快照",
        batch["code"].startswith("PD") and batch["item_count"] == 2 and batch["status"] == "open" and len(batch["items"]) == 2,
        batch,
    )
    check(
        "明细带书名与应盘册数（快照值）",
        sum(i["expected_copies"] for i in batch["items"]) == 5 and all(i["state"] == "pending" for i in batch["items"]),
        batch["items"],
    )
    batch2 = b11.post("/api/stocktaking", json={"location": "P区-1排"})
    check(
        "同一馆位不允许同时开两张盘点单（409）",
        batch2.status_code == 409 and batch2.get_json()["error"]["details"]["field"] == "location",
        batch2.get_json(),
    )
    other_batch = b11.post("/api/stocktaking", json={"location": "P区-2排"}).get_json()
    apply_open = b11.post(f"/api/stocktaking/{other_batch['id']}/apply", json={})
    check(
        "未关闭就调账被 422 拒（先确认差异、再改账）",
        apply_open.status_code == 422 and "先关闭" in apply_open.get_json()["error"]["message"],
        apply_open.get_json(),
    )

    item_a = next(i for i in batch["items"] if i["book_id"] == b_st1)
    item_b = next(i for i in batch["items"] if i["book_id"] == b_st2)
    set_a = b11.put(f"/api/stocktaking/{batch['id']}/items/{item_a['id']}", json={"counted_copies": 2})
    set_json = set_a.get_json()
    check(
        "录入实盘数后差异即时算出并刷新进度",
        set_a.status_code == 200
        and set_json["counted_count"] == 1
        and set_json["progress"] == 50.0
        and next(i for i in set_json["items"] if i["id"] == item_a["id"])["state_label"] == "盘亏 1",
        set_json,
    )
    b11.put(f"/api/stocktaking/{batch['id']}/items/{item_b['id']}", json={"counted_copies": 3})
    batch_detail = b11.get(f"/api/stocktaking/{batch['id']}").get_json()
    check(
        "汇总卡正确区分账实一致 / 盘盈 / 盘亏",
        batch_detail["summary"]["up"] == 1 and batch_detail["summary"]["down"] == 1 and batch_detail["summary"]["diff_total"] == 0,
        batch_detail["summary"],
    )
    filtered = b11.get(f"/api/stocktaking/{batch['id']}", query_string={"state": "up"}).get_json()
    check("可按差异类型筛选明细", len(filtered["items"]) == 1 and filtered["items"][0]["book_id"] == b_st2, filtered["items"])
    bad_count = b11.put(f"/api/stocktaking/{batch['id']}/items/{item_a['id']}", json={"counted_copies": -1})
    check(
        "实盘册数负数被 400 拒",
        bad_count.status_code == 400 and bad_count.get_json()["error"]["details"]["field"] == "counted_copies",
        bad_count.get_json(),
    )
    cleared = b11.put(f"/api/stocktaking/{batch['id']}/items/{item_a['id']}", json={"counted_copies": ""})
    check("留空表示撤销该项盘点", cleared.get_json()["summary"]["pending"] == 1, cleared.get_json()["summary"])
    b11.put(f"/api/stocktaking/{batch['id']}/items/{item_a['id']}", json={"counted_copies": 2})

    closed = b11.post(f"/api/stocktaking/{batch['id']}/close", json={})
    check("关闭盘点单冻结明细", closed.status_code == 200 and closed.get_json()["status"] == "closed", closed.get_json())
    frozen = b11.put(f"/api/stocktaking/{batch['id']}/items/{item_a['id']}", json={"counted_copies": 5})
    check("关闭后不能再录实盘数（409）", frozen.status_code == 409, frozen.get_json())
    del_closed = b11.delete(f"/api/stocktaking/{batch['id']}")
    check("已关闭的盘点单不可删除（留作盘点记录）", del_closed.status_code == 409, del_closed.get_json())

    applied = b11.post(f"/api/stocktaking/{batch['id']}/apply", json={})
    applied_json = applied.get_json()
    check(
        "按差异调账：两处差异都写回库存",
        applied.status_code == 200 and applied_json["applied"] == 2,
        applied_json,
    )
    check(
        "调账后总册数等于实盘数",
        b11_total(b_st1) == 2 and b11_total(b_st2) == 3,
        (b11_total(b_st1), b11_total(b_st2)),
    )
    again_apply = b11.post(f"/api/stocktaking/{batch['id']}/apply", json={}).get_json()
    check(
        "重复调账结果幂等（快照差异不变，写回的仍是同一组数值）",
        again_apply["applied"] == 2 and b11_total(b_st1) == 2 and b11_total(b_st2) == 3,
        again_apply,
    )

    # 调账不得把总册数压到低于在借册数，且校验必须先于任何写入
    m_st = b11_member("B11D001", "盘点测试甲")
    m_st2 = b11_member("B11D002", "盘点测试乙")
    b_st4 = b11_book("9787000000034", "盘点书四", 3, location="P区-3排")
    b_st5 = b11_book("9787000000035", "盘点书五", 2, location="P区-3排")
    b11_borrow(b_st4, m_st)
    b11_borrow(b_st4, m_st2)
    batch3 = b11.post("/api/stocktaking", json={"location": "P区-3排"}).get_json()
    i4 = next(i for i in batch3["items"] if i["book_id"] == b_st4)
    i5 = next(i for i in batch3["items"] if i["book_id"] == b_st5)
    b11.put(f"/api/stocktaking/{batch3['id']}/items/{i4['id']}", json={"counted_copies": 1})
    b11.put(f"/api/stocktaking/{batch3['id']}/items/{i5['id']}", json={"counted_copies": 4})
    b11.post(f"/api/stocktaking/{batch3['id']}/close", json={})
    conflict_apply = b11.post(f"/api/stocktaking/{batch3['id']}/apply", json={})
    check(
        "实盘数低于在借册数时调账被 422 拒",
        conflict_apply.status_code == 422 and "在借" in conflict_apply.get_json()["error"]["message"],
        conflict_apply.get_json(),
    )
    check(
        "校验先于写入：合法项也不会被半截调账（书五仍为 2 册）",
        b11_total(b_st4) == 3 and b11_total(b_st5) == 2,
        (b11_total(b_st4), b11_total(b_st5)),
    )
    reopened = b11.post(f"/api/stocktaking/{batch3['id']}/reopen", json={})
    check("已关闭的盘点单可重新打开补盘", reopened.status_code == 200 and reopened.get_json()["status"] == "open")
    del_open = b11.delete(f"/api/stocktaking/{batch3['id']}")
    check("进行中的盘点单可以删除", del_open.status_code == 200, del_open.get_json())
    b11.delete(f"/api/stocktaking/{other_batch['id']}")

    # ---- 33.7 采编验收 ----
    books_before_acq = b11_books_total()
    acq = b11.post(
        "/api/acquisitions",
        json={
            "title": "采编测试新书",
            "isbn": "9787000000041",
            "author": "采编者",
            "publisher": "测试社",
            "copies": 2,
            "unit_price": 39.9,
            "location": "C区-1排",
            "supplier": "某书商",
        },
    ).get_json()
    check(
        "登记到货单不直接进馆藏（status=pending，金额=册数×单价）",
        acq["status"] == "pending" and acq["amount"] == 79.8 and b11_books_total() == books_before_acq,
        acq,
    )
    check("到货单能识别馆里还没有同 ISBN 的书", acq["existing_book_id"] is None, acq)
    accepted = b11.post(f"/api/acquisitions/{acq['id']}/accept", json={})
    acc_json = accepted.get_json()
    check(
        "验收新 ISBN：自动建档并分配馆藏条码",
        accepted.status_code == 200 and acc_json["mode"] == "created" and acc_json["book_id"],
        acc_json,
    )
    check("建档后馆藏总数 +1，册数等于到货册数", b11_books_total() == books_before_acq + 1 and b11_total(acc_json["book_id"]) == 2)
    barcode = b11.get(f"/api/books/{acc_json['book_id']}").get_json()["barcode"]
    check("自动分配的条码非空", bool(barcode), barcode)
    re_accept = b11.post(f"/api/acquisitions/{acq['id']}/accept", json={})
    check("已验收的单不能重复验收（409）", re_accept.status_code == 409, re_accept.get_json())
    del_accepted = b11.delete(f"/api/acquisitions/{acq['id']}")
    check("已验收的到货单不可删除（留作采购记录）", del_accepted.status_code == 409, del_accepted.get_json())

    acq2 = b11.post(
        "/api/acquisitions",
        json={"title": "采编测试新书（补订）", "isbn": "9787000000041", "copies": 3, "unit_price": 39.9},
    ).get_json()
    check(
        "同 ISBN 再次到货时提示将「加册」而不是新建",
        acq2["existing_book_id"] == acc_json["book_id"],
        acq2,
    )
    total_before_acq2 = b11_total(acc_json["book_id"])
    accepted2 = b11.post(f"/api/acquisitions/{acq2['id']}/accept", json={}).get_json()
    check(
        "同 ISBN 验收走加册分支（不新增书目）",
        accepted2["mode"] == "add_copies" and b11_books_total() == books_before_acq + 1,
        accepted2,
    )
    check(
        "加册后总册数与可借数同步 +3",
        b11_total(acc_json["book_id"]) == total_before_acq2 + 3,
        b11_total(acc_json["book_id"]),
    )

    acq_no_isbn = b11.post("/api/acquisitions", json={"title": "无书号的到货", "copies": 1}).get_json()
    no_isbn = b11.post(f"/api/acquisitions/{acq_no_isbn['id']}/accept", json={})
    check(
        "没有 ISBN 又无同书可加册时验收被 422 拒（不静默丢数据）",
        no_isbn.status_code == 422 and "ISBN" in no_isbn.get_json()["error"]["message"],
        no_isbn.get_json(),
    )
    no_reason = b11.post(f"/api/acquisitions/{acq_no_isbn['id']}/reject", json={})
    check(
        "驳回必须写原因（400 且指明字段）",
        no_reason.status_code == 400 and no_reason.get_json()["error"]["details"]["field"] == "reason",
        no_reason.get_json(),
    )
    rejected = b11.post(f"/api/acquisitions/{acq_no_isbn['id']}/reject", json={"reason": "到货与订单不符"})
    check("驳回后状态流转", rejected.status_code == 200 and rejected.get_json()["status"] == "rejected", rejected.get_json())
    acq_list = b11.get("/api/acquisitions").get_json()
    check(
        "到货单列表带状态汇总",
        acq_list["summaries"]["pending"] == 0
        and acq_list["summaries"]["accepted_copies"] == 5
        and set(acq_list["status_labels"]) == {"pending", "accepted", "rejected"},
        acq_list["summaries"],
    )

    # ---- 33.8 读者附件 ----
    PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64 + b"IEND\xaeB`\x82"
    PDF_BYTES = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + b"\x00" * 32
    m_file = b11_member("B11E001", "附件测试甲")
    up = b11.post(
        "/api/member-files",
        data={"member_id": str(m_file), "kind": "photo", "label": "证件照", "file": (io.BytesIO(PNG_BYTES), "photo.png")},
        content_type="multipart/form-data",
    )
    up_json = up.get_json()
    check(
        "上传读者照片成功并可直接内联展示（url + is_image）",
        up.status_code == 201 and up_json["is_image"] is True and up_json["url"].startswith("/api/member-files/"),
        up_json,
    )
    check(
        "磁盘存储名由服务端随机生成、不含路径分隔符",
        "/" not in up_json["stored_name"] and "\\" not in up_json["stored_name"] and up_json["stored_name"].endswith(".png"),
        up_json["stored_name"],
    )
    with b11_app.app_context():
        from library.memberfiles import service as memberfile_service  # noqa: PLC0415

        stored_path = memberfile_service.path_of(up_json)
    check(
        "落盘位置在「上传根目录/读者 ID」下（路径穿越无效）",
        stored_path.exists() and stored_path.parent.name == str(m_file),
        str(stored_path),
    )

    wrong_ext = b11.post(
        "/api/member-files",
        data={"member_id": str(m_file), "kind": "photo", "file": (io.BytesIO(PNG_BYTES), "evil.txt")},
        content_type="multipart/form-data",
    )
    check(
        "扩展名白名单拦截（.txt 被 400 拒）",
        wrong_ext.status_code == 400 and wrong_ext.get_json()["error"]["details"]["field"] == "file",
        wrong_ext.get_json(),
    )
    spoofed = b11.post(
        "/api/member-files",
        data={"member_id": str(m_file), "kind": "photo", "file": (io.BytesIO(PDF_BYTES), "fake.png")},
        content_type="multipart/form-data",
    )
    check(
        "魔数校验拦截「改扩展名伪装」（PNG 名 + PDF 内容被 400 拒）",
        spoofed.status_code == 400 and "实际内容" in spoofed.get_json()["error"]["message"],
        spoofed.get_json(),
    )
    too_big = b11.post(
        "/api/member-files",
        data={
            "member_id": str(m_file),
            "kind": "photo",
            "file": (io.BytesIO(PNG_BYTES + b"\x00" * (1024 * 1024 + 16)), "big.png"),
        },
        content_type="multipart/form-data",
    )
    check(
        "超过大小上限被 400 拒",
        too_big.status_code == 400 and "上限" in too_big.get_json()["error"]["message"],
        too_big.get_json(),
    )
    bad_kind = b11.post(
        "/api/member-files",
        data={"member_id": str(m_file), "kind": "video", "file": (io.BytesIO(PNG_BYTES), "a.png")},
        content_type="multipart/form-data",
    )
    check("附件类型非法被 400 拒", bad_kind.status_code == 400, bad_kind.get_json())

    pdf_up = b11.post(
        "/api/member-files",
        data={"member_id": str(m_file), "kind": "id_card", "file": (io.BytesIO(PDF_BYTES), "id.pdf")},
        content_type="multipart/form-data",
    ).get_json()
    files_list = b11.get("/api/member-files", query_string={"member_id": m_file}).get_json()
    check(
        "附件列表按类型分组（照片 / 证件）并带上限提示",
        len(files_list["photos"]) == 1
        and len(files_list["id_cards"]) == 1
        and files_list["photos"][0]["kind_label"] == "读者照片"
        and files_list["id_cards"][0]["kind_label"] == "证件影像"
        and files_list["max_size_mb"] == 1,
        files_list,
    )
    check("PDF 附件标记为非图片（前端走占位图标）", pdf_up["is_image"] is False and pdf_up["size_label"].endswith("B"))

    anonymous = b11_app.test_client().get(f"/api/member-files/{up_json['id']}")
    check("未登录不能下载读者附件（401，无公开直链）", anonymous.status_code == 401, anonymous.status_code)
    got = b11.get(f"/api/member-files/{up_json['id']}")
    check(
        "登录后可下载附件且禁止任何中间层缓存",
        got.status_code == 200
        and got.headers.get("Content-Type", "").startswith("image/png")
        and "no-store" in got.headers.get("Cache-Control", ""),
        (got.headers.get("Content-Type"), got.headers.get("Cache-Control")),
    )
    check("下载内容与上传字节一致", got.get_data() == PNG_BYTES, len(got.get_data()))
    # Windows 上未关闭的响应会一直占着文件句柄，导致后续 unlink 报 WinError 32
    got.close()
    deleted = b11.delete(f"/api/member-files/{up_json['id']}")
    check("删除附件返回被删记录", deleted.status_code == 200 and deleted.get_json()["kind"] == "photo", deleted.get_json())
    check("删除后磁盘文件同步清理", not stored_path.exists(), str(stored_path))
    after_del = b11.get("/api/member-files", query_string={"member_id": m_file}).get_json()
    check("删除后列表不再包含该附件", len(after_del["photos"]) == 0, after_del)
    missing_file = b11.get("/api/member-files/999999")
    check("不存在的附件返回 404", missing_file.status_code == 404, missing_file.status_code)

    # ---- 33.9 定时释放与通知全量扫描 ----
    from library import scheduler  # noqa: PLC0415

    b_hold = b11_book("9787000000051", "定时释放测试书", 1)
    m_hold = b11_member("B11F001", "定时测试甲")
    b11_borrow(b_hold, m_hold)  # 占满唯一复本
    m_wait = b11_member("B11F002", "定时测试乙")
    book_hold_id = b_hold
    # 新建读者的初始口令 = 证号后 6 位
    m_wait_pwd = "B11F002"[-6:]
    # 预约读者要走读者端写接口，先解除「首次登录须改密」闸门
    with b11_app.app_context():
        for mid in (m_hold, m_wait):
            db.execute("UPDATE members SET must_change_password = 0 WHERE id = ?", [mid])
    reader_client = b11_app.test_client()
    login = reader_client.post("/api/reader/login", json={"account": "B11F002", "password": m_wait_pwd})
    check("新建读者可用默认初始口令登录读者端", login.status_code == 200, login.get_json())
    rcsrf = (login.get_json() or {}).get("csrf_token")
    booked = reader_client.post(
        "/api/reader/reservations", json={"book_id": book_hold_id}, headers={"X-CSRF-Token": rcsrf}
    )
    check("预约已借完的图书成功", booked.status_code in (200, 201), booked.get_json())
    ready = b11.post(f"/api/reservations/{booked.get_json()['id']}/ready")
    check("馆员标记到书", ready.status_code == 200 and ready.get_json()["status"] == "ready", ready.get_json())
    with b11_app.app_context():
        db.execute(
            "UPDATE reservations SET ready_at = datetime('now', 'localtime', '-10 days') WHERE id = ?",
            [int(booked.get_json()["id"])],
        )
        db.execute(
            "UPDATE loans SET due_at = datetime('now', 'localtime', '-2 days') WHERE book_id = ? AND returned_at IS NULL",
            [book_hold_id],
        )
    sweep = with_ctx(b11_app, scheduler.run_once)
    check(
        "定时扫描返回预约释放 / 通知 / 邮件出站计数",
        isinstance(sweep, dict)
        and set(sweep) == {"expired_reservations", "members", "due_soon", "overdue", "mail"},
        sweep,
    )
    check("超期未取的「已到书」预约被自动释放", sweep["expired_reservations"] >= 1, sweep)
    check("全量扫描覆盖有在借记录的读者", sweep["members"] >= 2 and sweep["overdue"] >= 1, sweep)
    with b11_app.app_context():
        res_row = db.query_one("SELECT status FROM reservations WHERE id = ?", [int(booked.get_json()["id"])])
    check("预约状态落为 expired", res_row and res_row["status"] == "expired", res_row)
    check("重复扫描不重复释放（幂等）", with_ctx(b11_app, scheduler.run_once)["expired_reservations"] == 0)
    check("默认不启动后台线程（测试与嵌入式用法零副作用）", scheduler.is_running() is False)
    started = scheduler.start(b11_app, interval=3600)
    check("显式调用可启动后台线程", started is True and scheduler.is_running() is True)
    check("重复启动被忽略", scheduler.start(b11_app, interval=3600) is False)
    scheduler.stop()
    check("停止后线程退出", scheduler.is_running() is False)
    check("间隔为 0 时不启动线程", scheduler.start(b11_app, interval=0) is False)

    # ---- 33.10 打印排版与前端契约 ----
    check(
        "后台含打印层与两张打印入口（催还单 / 借书凭证）",
        all(x in html_admin for x in ('id="printLayer"', 'id="loanPrintOverdue"', 'id="loanNotifyOverdue"')),
    )
    check(
        "后台 JS 实现催还单、借书凭证与批量催还",
        all(x in js_admin for x in ("printOverdueSlips", "printLoanReceipt", "notifyOverdue")),
    )
    check(
        "打印样式覆盖凭证 / 催还单 / 书标三类版式",
        all(x in css_admin for x in (".print-sheet", ".ps-sign", ".print-label", ".print-layer")),
    )
    check(
        "催还单按条码标签同样的「块级平铺」写法（flex 会让 page-break 失效）",
        "display: inline-block" in css_admin and "@media print" in css_admin,
    )
    check(
        "后台新增五个入口与视图骨架",
        all(
            x in html_admin
            for x in (
                'data-view="fines"',
                'data-view="compensations"',
                'data-view="stocktaking"',
                'data-view="acquisitions"',
                'data-view="imports"',
                'id="view-fines"',
                'id="view-compensations"',
                'id="view-stocktaking"',
                'id="view-acquisitions"',
                'id="view-imports"',
            )
        ),
    )
    check(
        "后台 JS 接入五组新视图与批量导入",
        all(
            x in js_admin
            for x in (
                "loadFines",
                "loadCompensations",
                "loadStocktaking",
                "loadAcquisitions",
                "loadImportsView",
                "runImport",
                "/api/compensations",
                "/api/stocktaking",
                "/api/acquisitions",
                "/api/import/",
            )
        ),
    )
    check(
        "罚金减免走二次确认（金额 + 理由摆在一起再确认）",
        "confirmFineWaiver" in js_admin and ".confirm-block" in css_admin and ".confirm-amount" in css_admin,
    )
    check(
        "六个视图补齐 CSV 导出按钮与导出函数",
        all(
            x in html_admin
            for x in (
                'id="fineExport"',
                'id="compensationExport"',
                'id="acquisitionExport"',
                'id="reservationExport"',
                'id="logExport"',
                'id="announcementExport"',
                'id="applicationExport"',
                'id="engagementExport"',
            )
        )
        and all(
            x in js_admin
            for x in (
                "exportFines",
                "exportCompensations",
                "exportAcquisitions",
                "exportReservations",
                "exportLogs",
                "exportAnnouncements",
                "exportApplications",
                "exportEngagementRows",
            )
        ),
    )
    check(
        "导入结果面板支持逐行标红与失败行样式",
        all(x in html_admin for x in ('id="importPaste"', 'id="importPreview"', 'id="importRun"', 'id="importResultTable"'))
        and all(x in css_admin for x in (".import-panel", ".row-bad", ".import-tip")),
    )
    check(
        "读者附件弹窗与列表样式齐备",
        "openMemberFilesDialog" in js_admin
        and all(x in css_admin for x in (".file-list", ".file-item", ".fi-thumb", ".fi-actions")),
    )
    # ==================== 第 12 批：跨身份系统级缺口 ====================
    import base64 as b12_b64  # noqa: PLC0415
    import io as b12_io  # noqa: PLC0415
    import zipfile as b12_zip  # noqa: PLC0415

    from library import backup as b12_backup  # noqa: PLC0415
    from library import scheduler as b12_scheduler  # noqa: PLC0415
    from library.auth import permissions as b12_perm  # noqa: PLC0415
    from library.importer import xlsx as b12_xlsx  # noqa: PLC0415
    from config import get_config as b12_get_config, set_config as b12_set_config  # noqa: PLC0415
    from app import build_parser as b12_build_parser  # noqa: PLC0415

    print("\n=== 34. 系统级补全：读能力 / 备份恢复 / 调度留痕 / 邮件出站 / 审批流 / xlsx ===")

    # ---- 34.1 读能力矩阵（声明了就必须生效） ----
    check(
        "管理员拥有全部能力（含新增的四项读能力与审批权）",
        set(b12_perm.capabilities_for("admin")) == set(b12_perm.ALL_CAPABILITIES)
        and len(b12_perm.ALL_CAPABILITIES) == 12,
        sorted(b12_perm.ALL_CAPABILITIES),
    )
    check(
        "馆员具备读写业务能力，但不含审批 / 账号 / 日志",
        set(b12_perm.capabilities_for("librarian"))
        == {
            "book:read",
            "book:write",
            "member:read",
            "member:write",
            "loan:read",
            "loan:write",
            "announcement:read",
            "announcement:write",
            "dashboard:read",
        },
        sorted(b12_perm.capabilities_for("librarian")),
    )
    check("未知角色得到空能力集（失败即拒绝）", b12_perm.capabilities_for("auditor") == frozenset())

    b12_root = Path(tempfile.mkdtemp(prefix="batch12-smoke-"))
    b12_cfg = Config(
        db_path=str(b12_root / "b12.db"),
        host="127.0.0.1",
        port=8000,
        debug=False,
        loan_days=30,
        max_renew=2,
        fine_per_day=0.5,
        default_max_borrow=50,
        log_level="WARNING",
        secret_key="test-secret-b12",
        library_name="测试图书馆",
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PWD,
        login_max_attempts=3,
        login_lock_minutes=5,
        cookie_secure=False,
        # 只留惰性扫描，测试不起后台线程
        sweep_interval_seconds=900,
        upload_dir=str(b12_root / "uploads"),
        # 减免 ≥ ¥5 走审批，让阈值开关在这批里真正被跑到
        approval_waiver_threshold=5.0,
        backup_dir=str(b12_root / "backups"),
    )
    b12_app = create_app(b12_cfg)
    b12 = Api(b12_app.test_client())
    b12.csrf = (
        b12.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD}).get_json() or {}
    ).get("csrf_token")

    with b12_app.app_context():
        b12_tables = {
            row["name"] for row in db.query_all("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    b12_new_tables = {"job_runs", "outbound_messages", "approvals"}
    check(
        "新增三张表（调度留痕 / 邮件出站 / 审批工单）",
        b12_new_tables <= b12_tables,
        sorted(b12_new_tables - b12_tables),
    )

    def b12_member(card, name, *, email="", subscribed=1, mtype="学生"):
        r = b12.post(
            "/api/members",
            json={"card_no": card, "name": name, "member_type": mtype, "status": "active", "max_borrow": 50},
        )
        assert r.status_code in (200, 201), r.get_json()
        member_id = int(r.get_json()["id"])
        if email:
            with b12_app.app_context():
                db.execute(
                    "UPDATE members SET email = ?, email_subscribed = ? WHERE id = ?",
                    [email, int(subscribed), member_id],
                )
        return member_id

    def b12_book(isbn, title, total=1):
        r = b12.post(
            "/api/books",
            json={"isbn": isbn, "title": title, "category": "第12批", "total_copies": total},
        )
        assert r.status_code in (200, 201), r.get_json()
        return int(r.get_json()["id"])

    def b12_borrow(book_id, member_id):
        r = b12.post("/api/loans/borrow", json={"book_id": book_id, "member_id": member_id})
        assert r.status_code in (200, 201), r.get_json()
        return int(r.get_json()["id"])

    def b12_backdate(loan_id, days=10):
        with b12_app.app_context():
            db.execute(
                "UPDATE loans SET due_at = datetime('now', 'localtime', ?) WHERE id = ?",
                [f"-{int(days)} days", int(loan_id)],
            )

    def b12_make_xlsx(rows):
        """构造一个最小可用的 xlsx（含 sharedStrings 与 workbook 关系文件）。"""
        shared: list[str] = []
        index_of: dict[str, int] = {}

        def sid(text: str) -> int:
            if text not in index_of:
                index_of[text] = len(shared)
                shared.append(text)
            return index_of[text]

        body = []
        for r, row in enumerate(rows, start=1):
            cells = []
            for c, value in enumerate(row):
                ref = f"{chr(ord('A') + c)}{r}"
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    cells.append(f'<c r="{ref}" t="s"><v>{sid(str(value))}</v></c>')
            body.append(f'<row r="{r}">{"".join(cells)}</row>')
        shared_xml = "".join(f"<si><t>{text}</t></si>" for text in shared)
        sheet_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(body)}</sheetData></worksheet>'
        )
        shared_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"{shared_xml}</sst>"
        )
        workbook_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
        )
        rels_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet1.xml"/></Relationships>'
        )
        buffer = b12_io.BytesIO()
        with b12_zip.ZipFile(buffer, "w", b12_zip.ZIP_DEFLATED) as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")
            zf.writestr("xl/workbook.xml", workbook_xml)
            zf.writestr("xl/_rels/workbook.xml.rels", rels_xml)
            zf.writestr("xl/sharedStrings.xml", shared_xml)
            zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        return buffer.getvalue()

    # 读能力真正生效：临时把馆员角色的**读能力抽掉**（只留写），读接口必须 403 而不是"登录即全读"。
    # （数据库对 role 有 CHECK 约束，塞不进未知角色，所以直接改内存里的能力映射表再还原。）
    b12_saved_librarian = b12_perm.ROLE_CAPABILITIES["librarian"]
    b12_perm.ROLE_CAPABILITIES["librarian"] = frozenset({"book:write", "member:write"})
    try:
        only_write = librarian_client(b12_app, username="smoke-lr-read", password="librarian123")
        read_denied = only_write.get("/api/books")
        overview_denied = only_write.get("/api/dashboard/overview")
    finally:
        b12_perm.ROLE_CAPABILITIES["librarian"] = b12_saved_librarian
    check(
        "抽掉读能力后，后台读接口被 403 拦下（读能力声明真正生效）",
        read_denied.status_code == 403
        and "查看图书与馆藏" in read_denied.get_json()["error"]["message"],
        read_denied.get_json(),
    )
    check("概览接口同受读能力保护", overview_denied.status_code == 403)
    restored_lib = librarian_client(b12_app, username="smoke-lr-read", password="librarian123")
    check(
        "能力还原后馆员读接口恢复正常（日常业务不受影响）",
        restored_lib.get("/api/books").status_code == 200
        and restored_lib.get("/api/dashboard/overview").status_code == 200,
    )
    check("未登录访问后台读接口被 401 拦下", Api(b12_app.test_client()).get("/api/books").status_code == 401)
    check(
        "权限不足的尝试进了操作日志（状态=已拦截）",
        with_ctx(
            b12_app,
            lambda: db.query_one(
                "SELECT COUNT(*) AS n FROM operation_logs WHERE action = 'permission.denied' AND status = 'denied'"
            )["n"],
        )
        >= 2,
    )

    # ---- 34.2 备份与恢复 ----
    backup_res = b12.post("/api/system/backups", json={"with_uploads": False})
    backup_json = backup_res.get_json() or {}
    check(
        "管理员可在后台触发备份，拿到文件名与数据量摘要",
        backup_res.status_code == 200
        and backup_json.get("name", "").endswith(".db")
        and backup_json.get("counts", {}).get("admins", 0) >= 1,
        backup_json,
    )
    listed = b12.get("/api/system/backups").get_json()
    check(
        "备份列表包含刚生成的备份",
        any(item["name"] == backup_json.get("name") for item in listed["items"]),
        [item["name"] for item in listed.get("items", [])],
    )
    downloaded = b12.get(f"/api/system/backups/{backup_json['name']}")
    check(
        "备份文件可下载且是合法 SQLite（按头部魔数校验）",
        downloaded.status_code == 200 and downloaded.get_data()[:16] == b"SQLite format 3\x00",
        downloaded.headers.get("Content-Disposition"),
    )
    check(
        "备份下载做路径穿越校验（返 404 而不是吐文件）",
        b12.get("/api/system/backups/..%2F..%2Fapp.py").status_code == 404,
    )
    zip_backup = b12.post("/api/system/backups", json={"with_uploads": True}).get_json()
    check(
        "可选择「连附件一起打包」生成 zip（内含数据库）",
        str(zip_backup.get("name", "")).endswith(".zip") and zip_backup.get("with_uploads") is True,
        zip_backup,
    )
    check(
        "恢复只接受合法 SQLite 文件（假文件被拒，且不会动目标库）",
        _b12_fake_restore_rejected(b12_root),
    )
    # 真恢复：把备份还原到一个**独立临时库**（不动正在跑的库），验证整条链路
    b12_restore_cfg = Config(
        db_path=str(b12_root / "restored" / "r.db"),
        host="127.0.0.1",
        port=8000,
        debug=False,
        loan_days=30,
        max_renew=2,
        fine_per_day=0.5,
        default_max_borrow=5,
        log_level="WARNING",
        secret_key="t",
        admin_user=ADMIN_USER,
        admin_password=ADMIN_PWD,
        backup_dir=str(b12_root / "backups"),
    )
    saved_cfg = b12_get_config()
    try:
        b12_set_config(b12_restore_cfg)
        restored = b12_backup.restore_backup(Path(backup_json["path"]))
    finally:
        b12_set_config(saved_cfg)
    check(
        "从备份恢复：目标库被写成备份内容并回报表行数",
        Path(b12_restore_cfg.db_path).exists()
        and restored["counts"]["admins"] >= 1
        and set(restored["counts"]) == {"books", "members", "loans", "admins"},
        restored.get("counts"),
    )
    check(
        "恢复前会留一份安全副本（目标已存在时）",
        _b12_restore_keeps_safety_copy(b12_root),
    )
    check(
        "CLI 命令注册齐全（backup / restore 都能被解析）",
        all(
            getattr(b12_build_parser().parse_args(argv), "func", None) is not None
            for argv in (["backup"], ["backup", "--list"], ["restore", "--from", "x.db"])
        ),
    )

    # ---- 34.3 调度可观测（手工触发 + 留痕） ----
    run_now = b12.post("/api/system/jobs/sweep/run", json={})
    check(
        "管理员可手动触发一轮后台任务并拿到五类计数",
        run_now.status_code == 200
        and set(run_now.get_json()["result"])
        == {"expired_reservations", "members", "due_soon", "overdue", "mail"},
        run_now.get_json(),
    )
    jobs = b12.get("/api/system/jobs").get_json()
    check(
        "手动触发写入 job_runs 且标注 kind=manual",
        jobs["total_runs"] >= 1
        and jobs["recent"][0]["kind"] == "manual"
        and jobs["recent"][0]["kind_label"] == "手动触发",
        jobs["recent"][:1],
    )
    check(
        "调度卡片暴露间隔、开关状态与线程状态",
        jobs["interval_seconds"] == 900 and jobs["enabled"] is True and jobs["thread_running"] is False,
        (jobs["interval_seconds"], jobs["enabled"], jobs["thread_running"]),
    )
    auto_sweep = with_ctx(b12_app, b12_scheduler.run_once, kind="auto")
    check("直接调用 run_once 也留痕（kind=auto）", isinstance(auto_sweep, dict) and "mail" in auto_sweep, auto_sweep)
    check(
        "未知任务名被 400 拦下",
        b12.post("/api/system/jobs/nope/run", json={}).status_code == 400,
    )
    check(
        "非系统管理员看不到系统维护接口（403）",
        librarian_client(b12_app).get("/api/system/jobs").status_code == 403,
    )

    # ---- 34.4 邮件出站渠道 ----
    from library.reader import service as b12_reader_service  # noqa: PLC0415
    import config as b12_config  # noqa: PLC0415

    no_mail_member = b12_member("B12M001", "无邮箱读者")
    b12.post(f"/api/members/{no_mail_member}/notify", json={"title": "站内通知", "body": "没有邮箱"})
    with b12_app.app_context():
        queued_none = int(
            db.query_one("SELECT COUNT(*) AS n FROM outbound_messages WHERE member_id = ?", [no_mail_member])["n"]
        )
        notif_none = int(
            db.query_one("SELECT COUNT(*) AS n FROM notifications WHERE member_id = ?", [no_mail_member])["n"]
        )
    check("读者没填邮箱时邮件不入队，但站内通知照发", queued_none == 0 and notif_none >= 1, (queued_none, notif_none))

    mail_member = b12_member("B12M002", "有邮箱读者", email="b12-reader@example.com")
    mail_book = b12_book("9787120000011", "邮件链路测试书", 1)
    mail_loan = b12_borrow(mail_book, mail_member)
    b12_backdate(mail_loan, 5)
    with_ctx(b12_app, b12_scheduler.run_once, kind="auto")
    with b12_app.app_context():
        queued_rows = [dict(r) for r in db.query_all("SELECT * FROM outbound_messages WHERE member_id = ?", [mail_member])]
    check(
        "逾期扫描同时把邮件入队（站内 + 邮件双通道）",
        len(queued_rows) == 1
        and queued_rows[0]["status"] == "pending"
        and queued_rows[0]["to_address"] == "b12-reader@example.com"
        and "应还日期" in queued_rows[0]["body"],
        queued_rows,
    )
    with_ctx(b12_app, b12_scheduler.run_once, kind="auto")
    with b12_app.app_context():
        queued_again = int(
            db.query_one("SELECT COUNT(*) AS n FROM outbound_messages WHERE member_id = ?", [mail_member])["n"]
        )
    check("重复扫描不重复入队（沿用通知的 dedupe_key 幂等）", queued_again == 1, queued_again)

    mail_status = b12.get("/api/system/mail").get_json()
    check(
        "渠道卡片暴露配置状态与队列统计（未配置时明确标注）",
        mail_status["configured"] is False
        and mail_status["counts"]["pending"] >= 1
        and mail_status["counts"]["sent"] == 0
        and mail_status["status_labels"]["pending"] == "待发送",
        mail_status["counts"],
    )
    test_mail = b12.post("/api/system/mail/test", json={"to": "ops@example.com"})
    check(
        "未配置 SMTP 时发测试邮件被 422 拦下并说明缺什么",
        test_mail.status_code == 422 and "SMTP" in test_mail.get_json()["error"]["message"],
        test_mail.get_json(),
    )
    check("测试邮件缺收件人时 400（先校验再连服务器）", b12.post("/api/system/mail/test", json={}).status_code == 400)

    with b12_app.app_context():
        db.execute("UPDATE outbound_messages SET status = 'failed', attempts = 5 WHERE member_id = ?", [mail_member])
    retried = b12.post("/api/system/mail/retry", json={}).get_json()
    check(
        "一键把发送失败的邮件重置回队列",
        retried["reset"] == 1 and retried["status"]["counts"]["pending"] >= 1,
        retried["reset"],
    )

    with b12_app.app_context():
        db.execute("UPDATE members SET email_subscribed = 0 WHERE id = ?", [mail_member])
        db.execute("DELETE FROM notifications WHERE member_id = ?", [mail_member])
    with_ctx(b12_app, b12_scheduler.run_once, kind="auto")
    with b12_app.app_context():
        after_unsub = int(
            db.query_one("SELECT COUNT(*) AS n FROM outbound_messages WHERE member_id = ?", [mail_member])["n"]
        )
    check("读者退订邮件后不再入队（站内通知仍照发）", after_unsub == 1, after_unsub)
    check(
        "读者资料里带邮件订阅开关（默认开启）",
        with_ctx(
            b12_app,
            lambda: b12_reader_service.public_member(
                db.query_one("SELECT * FROM members WHERE id = ?", [no_mail_member])
            ),
        )["email_subscribed"]
        is True,
    )

    # 退订必须**走一遍读者接口再读回来**。只改库、只断言库里的那行，会漏掉「接口回显」
    # 这一段：本批就真栽过一次——public_member 的列清单漏了 email_subscribed，
    # 于是库已退订、接口仍回显 true、页面开关自动又勾回去，读者以为没生效。
    from library.auth import passwords as b12_passwords  # noqa: PLC0415

    sub_card = "B12M004"
    sub_member = b12_member(sub_card, "订阅开关读者", email="b12-sub@example.com")
    with b12_app.app_context():
        db.execute(
            "UPDATE members SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            [b12_passwords.hash_password("sub123456"), sub_member],
        )
    sub_client = Api(b12_app.test_client())
    sub_login = sub_client.post(
        "/api/reader/login", json={"account": sub_card, "password": "sub123456"}
    ).get_json() or {}
    sub_client.csrf = sub_login.get("csrf_token")
    check(
        "读者本人可见资料里带订阅开关（默认开启）",
        ((sub_client.get("/api/reader/me").get_json() or {}).get("member") or {}).get("email_subscribed")
        is True,
    )
    off = sub_client.put(
        "/api/reader/profile",
        json={"phone": "", "email": "b12-sub@example.com", "email_subscribed": False},
    ).get_json() or {}
    check("读者关闭邮件订阅后，接口当场回显为已关闭", off.get("email_subscribed") is False, off.get("email_subscribed"))
    check(
        "重新拉 /api/reader/me 仍是已关闭（不是只在本次响应里改了）",
        ((sub_client.get("/api/reader/me").get_json() or {}).get("member") or {}).get("email_subscribed")
        is False,
    )
    with b12_app.app_context():
        check(
            "退订确实落到了库里（email_subscribed = 0）",
            int(db.query_one("SELECT email_subscribed FROM members WHERE id = ?", [sub_member])["email_subscribed"])
            == 0,
        )

    # ---- 34.5 审批流（阈值 ¥5） ----
    ap_member = b12_member("B12M003", "审批测试读者")
    ap_book = b12_book("9787120000021", "审批测试书", 1)
    ap_loan = b12_borrow(ap_book, ap_member)
    b12_backdate(ap_loan, 30)
    owed = b12.get(f"/api/fines/member/{ap_member}").get_json()
    check("逾期 30 天按统一口径算出应缴 ¥15", owed["total_outstanding"] == 15.0, owed)

    below = b12.post("/api/fines/waive", json={"member_id": ap_member, "loan_id": ap_loan, "amount": 1, "note": "小额减免"})
    check(
        "低于阈值的减免直接生效（不启用审批的老路径保持原样）",
        below.status_code == 200 and below.get_json()["kind"] == "waiver" and "status" not in below.get_json(),
        below.get_json(),
    )
    over = b12.post("/api/fines/waive", json={"member_id": ap_member, "loan_id": ap_loan, "amount": 14, "note": "金额较大"})
    over_json = over.get_json() or {}
    check(
        "达到阈值的减免不生效，转为待审批工单",
        over.status_code == 200
        and over_json.get("status") == "pending_approval"
        and over_json.get("approval_id")
        and over_json.get("threshold") == 5.0,
        over_json,
    )
    left = b12.get(f"/api/fines/member/{ap_member}").get_json()
    check("提交审批期间罚金一分未减（业务数据零改动）", left["total_outstanding"] == 14.0, left["total_outstanding"])

    with b12_app.app_context():
        admin_display = db.query_one("SELECT display_name FROM admins WHERE username = ?", [ADMIN_USER])["display_name"]
    pend = b12.get("/api/approvals").get_json()
    check(
        "审批中心列出待审批工单且描述可读",
        pend["total"] >= 1
        and pend["items"][0]["status"] == "pending"
        and "减免" in pend["items"][0]["entity_label"]
        # 留痕记的是操作人**显示名**（不是登录名）——审计里给人看的是名字
        and pend["items"][0]["requested_by"] == admin_display
        # 分页组件读 payload.pages，漏了会渲染成「第 1/undefined 页」
        and pend.get("pages") == max(1, -(-pend["total"] // pend["page_size"])),
        pend["items"][:1],
    )
    check(
        "待审批角标与列表口径一致",
        b12.get("/api/approvals/pending-count").get_json()["pending"] == pend["pending"],
    )
    approval_id = int(over_json["approval_id"])
    check("驳回必须填写审批意见（400）", b12.post(f"/api/approvals/{approval_id}/reject", json={}).status_code == 400)
    check(
        "未知工单号 404",
        b12.post("/api/approvals/999999/approve", json={}).status_code == 404,
    )
    approved = b12.post(f"/api/approvals/{approval_id}/approve", json={"review_note": "情况属实，同意"})
    check(
        "审批通过后立即执行减免并回报金额",
        approved.status_code == 200
        and approved.get_json()["status"] == "approved"
        and approved.get_json()["waived_amount"] == 14.0,
        approved.get_json(),
    )
    check(
        "减免后该笔已结清（待缴归零）",
        b12.get(f"/api/fines/member/{ap_member}").get_json()["total_outstanding"] == 0.0,
    )
    check(
        "已完成的工单不能重复处理（409）",
        b12.post(f"/api/approvals/{approval_id}/approve", json={}).status_code == 409,
    )
    check("馆员无权查看审批中心（403）", librarian_client(b12_app).get("/api/approvals").status_code == 403)
    check(
        "工单里留住了申请理由与审批意见",
        b12.get(f"/api/approvals?status=approved").get_json()["items"][0]["review_note"] == "情况属实，同意",
    )

    # ---- 34.6 xlsx 导入（零依赖解析） ----
    xlsx_bytes = b12_make_xlsx(
        [
            ["书名", "ISBN", "作者", "总册数"],
            ["Excel 导入测试书", "9787120000031", "某人", 2],
            ["Excel 第二本", "9787120000032", "另一人", 1],
        ]
    )
    xlsx_b64 = b12_b64.b64encode(xlsx_bytes).decode()
    preview = b12.post("/api/import/books", json={"xlsx": xlsx_b64, "dry_run": True})
    preview_json = preview.get_json() or {}
    check(
        "上传 xlsx：解开成行并逐行校验（预览不写库）",
        preview.status_code == 200 and preview_json["total"] == 2 and preview_json["ok"] is True,
        preview_json,
    )
    check(
        "预览确实没写库",
        b12.get("/api/books", query_string={"keyword": "Excel 导入测试书"}).get_json()["total"] == 0,
    )
    real = b12.post("/api/import/books", json={"xlsx": xlsx_b64})
    check(
        "xlsx 正式导入建目成功",
        real.get_json()["created"] == 2 and real.get_json()["failed"] == 0,
        real.get_json(),
    )
    check(
        "导入后的书目可检索到",
        b12.get("/api/books", query_string={"keyword": "Excel 导入测试书"}).get_json()["total"] == 1,
    )
    bad_b64 = b12.post("/api/import/books", json={"xlsx": "not-base64!!!", "dry_run": True})
    check(
        "非法 base64 被 400 拦下并指明字段",
        bad_b64.status_code == 400 and bad_b64.get_json()["error"]["details"]["field"] == "xlsx",
        bad_b64.get_json(),
    )
    broken_zip = b12.post(
        "/api/import/books",
        json={"xlsx": b12_b64.b64encode(b"PK\x03\x04broken payload").decode(), "dry_run": True},
    )
    check("PK 头但不是有效 xlsx 被 400 拦下", broken_zip.status_code == 400, broken_zip.get_json())
    not_zip = b12.post(
        "/api/import/books",
        json={"xlsx": b12_b64.b64encode(b"legacy xls binary content").decode(), "dry_run": True},
    )
    check(
        "上传旧版 .xls 时提示另存为 .xlsx",
        not_zip.status_code == 400 and ".xlsx" in not_zip.get_json()["error"]["message"],
        not_zip.get_json(),
    )
    parsed = b12_xlsx.parse_xlsx(b12_make_xlsx([["列一", "列二"], [2020.0, "文本"]]))
    check("xlsx 解析：整数型浮点去尾（2020.0 → 2020）、文本保真", parsed[1] == ["2020", "文本"], parsed)
    sparse = b12_xlsx.parse_xlsx(b12_make_xlsx([["a"], ["", "b"], ["c", "d"]]))
    check("xlsx 解析：稀疏行按列回填（缺列补空）", sparse[1] == ["", "b"], sparse)
    check("xlsx 解析：尾部的全空行被丢掉", len(b12_xlsx.parse_xlsx(b12_make_xlsx([["x"], ["", " "]]))) == 1)
    check(
        "非 zip 内容不会被误判成 xlsx",
        b12_xlsx.looks_like_xlsx(b"hello") is False and b12_xlsx.looks_like_xlsx(b"PK\x03\x04") is True,
    )

    # ---- 34.7 新增配置项 ----
    with b11_env(
        LIBRARY_SMTP_HOST="smtp.example.com",
        LIBRARY_SMTP_PORT="465",
        LIBRARY_SMTP_SECURE="SSL",
        LIBRARY_APPROVAL_WAIVER_THRESHOLD="20",
        LIBRARY_BACKUP_DIR=str(b12_root / "bk"),
    ):
        loaded12 = b12_config.load_config()
    check(
        "邮件 / 审批 / 备份配置可从环境变量读取",
        loaded12.smtp_host == "smtp.example.com"
        and loaded12.smtp_port == 465
        and loaded12.smtp_secure == "ssl"
        and loaded12.approval_waiver_threshold == 20.0
        and loaded12.backup_dir.endswith("bk"),
        (loaded12.smtp_host, loaded12.smtp_port, loaded12.smtp_secure, loaded12.approval_waiver_threshold),
    )
    check("SMTP 未配置时默认为空（出站队列照常入队但不投递）", b12_config.load_config().smtp_host == "")
    with b11_env(LIBRARY_SMTP_SECURE="starttls"):
        check("SMTP 加密方式非法时拒绝启动", config_rejected(b12_config))
    with b11_env(LIBRARY_APPROVAL_WAIVER_THRESHOLD="-1"):
        check("审批阈值不能为负数", config_rejected(b12_config))
    env12 = (root_res / ".env.example").read_text(encoding="utf-8")
    check(
        ".env.example 补齐第 12 批配置项",
        all(
            x in env12
            for x in (
                "LIBRARY_SMTP_HOST",
                "LIBRARY_SMTP_PORT",
                "LIBRARY_SMTP_USER",
                "LIBRARY_SMTP_PASSWORD",
                "LIBRARY_SMTP_FROM",
                "LIBRARY_SMTP_SECURE",
                "LIBRARY_APPROVAL_WAIVER_THRESHOLD",
                "LIBRARY_BACKUP_DIR",
            )
        ),
    )

    # ---- 34.8 前端契约 ----
    check(
        "后台新增「审批中心」「系统维护」入口与视图",
        all(
            x in html_admin
            for x in (
                'data-view="approvals"',
                'data-view="system"',
                'id="view-approvals"',
                'id="view-system"',
                'id="approvalTable"',
                'id="jobTable"',
                'id="mailTable"',
                'id="backupTable"',
                'id="approvalBadge"',
            )
        ),
    )
    check(
        "前端脚本含审批 / 系统维护 / 备份 / 邮件四组函数",
        all(
            x in js_admin
            for x in (
                "loadApprovals",
                "openApprovalReview",
                "refreshApprovalBadge",
                "loadSystemJobs",
                "runJobNow",
                "loadSystemMail",
                "sendTestMail",
                "retryFailedMail",
                "loadSystemBackups",
                "createBackup",
                "downloadBackup",
            )
        ),
    )
    check(
        "审批空态文案按当前筛选条件判断（不看响应体里没有的字段）",
        'state.approvals.status === "pending" ? "没有待审批的工单"' in js_admin,
    )
    check(
        "导入页支持 .xlsx（accept + base64 提交 + 载入提示）",
        'accept=".csv,.txt,.xlsx' in html_admin
        and "state.imports.xlsx" in js_admin
        and "readAsDataURL" in js_admin,
    )
    check("减免转审批时前端提示等待审批", "pending_approval" in js_admin)
    check(
        "读者端有邮件订阅开关并随资料一起提交",
        'id="pEmailSubscribed"' in html_reader and "email_subscribed" in js_reader,
    )
    check(
        "系统维护页样式齐备（汇总卡 / 提示条复用既有类）",
        all(x in css_admin for x in (".sum-grid", ".import-tip", ".badge.info")),
    )

    # ==================== 第 12 批结束 ====================

    env_example = (root_res / ".env.example").read_text(encoding="utf-8")
    check(
        ".env.example 补齐新增配置项",
        all(
            x in env_example
            for x in ("LIBRARY_SWEEP_INTERVAL_SECONDS", "LIBRARY_UPLOAD_DIR", "LIBRARY_UPLOAD_MAX_MB")
        ),
    )

    print(f"\n{'=' * 46}\n结果：{PASSED} 项通过，{FAILED} 项失败\n{'=' * 46}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
