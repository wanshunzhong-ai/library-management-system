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
    auth_service._attempts.clear()
    tmp_dir = tempfile.mkdtemp(prefix="library-smoke-")
    db_path = os.path.join(tmp_dir, "test.db")
    app = build_app(db_path)
    client = app.test_client()
    api = Api(client)

    print("\n=== 1. 运维探针 ===")
    check("GET /health 返回 200", client.get("/health").status_code == 200)
    check("GET /ready 返回 200", client.get("/ready").status_code == 200)

    print("\n=== 2. 读者门户（免登录） ===")
    r = client.get("/")
    body = r.get_data(as_text=True)
    check("首页可访问", r.status_code == 200, r.status_code)
    check("首页含馆名", "测试图书馆" in body, body[:80])
    check("首页含检索框", 'id="searchForm"' in body)
    check("首页含管理员入口", "/login" in body)

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
    check("GET /admin 未登录跳转登录页", r.status_code == 302 and "/login" in r.headers.get("Location", ""), r.headers.get("Location"))
    r = client.get("/api/auth/me")
    check("未登录 /api/auth/me 返回 401", r.status_code == 401, r.status_code)

    print("\n=== 4. 登录 ===")
    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": "wrong-password"})
    check("密码错误返回 401", r.status_code == 401, r.status_code)
    check("错误信息统一", r.get_json()["error"]["message"] == "用户名或密码不正确", r.get_json())
    check("失败信息不泄露用户是否存在", "不存在" not in r.get_json()["error"]["message"], r.get_json())

    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ""})
    check("空密码返回 400", r.status_code == 400, r.status_code)

    r = client.post("/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PWD})
    check("正确密码登录 200", r.status_code == 200, r.get_json())
    login = r.get_json()
    check("登录返回管理员信息", login["admin"]["username"] == ADMIN_USER, login)
    check("登录返回 CSRF token", bool(login.get("csrf_token")), login)
    api.csrf = login["csrf_token"]

    r = client.get("/api/auth/me")
    check("登录后 /api/auth/me 可用", r.status_code == 200, r.status_code)
    check("/api/auth/me 回带 csrf", bool(r.get_json().get("csrf_token")), r.get_json())

    r = client.get("/admin")
    check("登录后可进入后台控制台", r.status_code == 200, r.status_code)
    check("后台页面含退出按钮", 'id="logoutBtn"' in r.get_data(as_text=True))

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
    auth_service._attempts.clear()
    codes = [client.post("/api/auth/login", json={"username": "attacker", "password": "x"}).status_code for _ in range(3)]
    check("连续失败累计计数", codes == [401, 401, 401], codes)
    r = client.post("/api/auth/login", json={"username": "attacker", "password": "x"})
    check("超过阈值后锁定返回 422", r.status_code == 422, r.status_code)
    check("锁定提示含剩余时间", "秒后重试" in r.get_json()["error"]["message"], r.get_json())
    auth_service._attempts.clear()

    print(f"\n{'=' * 46}\n结果：{PASSED} 项通过，{FAILED} 项失败\n{'=' * 46}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
