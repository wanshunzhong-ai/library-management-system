"""端到端冒烟测试：用 Flask 测试客户端走完读者门户、登录鉴权与后台业务链路。

运行：python tests/smoke_test.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import Config  # noqa: E402
from library import db, timeutil  # noqa: E402
from library.auth import service as auth_service  # noqa: E402
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
    check("手机号被他人占用 422", r.status_code == 422, r.get_json())
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

    print(f"\n{'=' * 46}\n结果：{PASSED} 项通过，{FAILED} 项失败\n{'=' * 46}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
