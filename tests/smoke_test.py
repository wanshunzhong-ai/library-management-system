"""端到端冒烟测试：用 Flask 测试客户端走完读者门户、登录鉴权与后台业务链路。

运行：python tests/smoke_test.py
"""

from __future__ import annotations

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

    print(f"\n{'=' * 46}\n结果：{PASSED} 项通过，{FAILED} 项失败\n{'=' * 46}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
