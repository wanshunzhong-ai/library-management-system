"""安全默认值与身份唯一性对照实验：在「第 2 批修复前 / 修复后」两套代码上跑同样的场景。

用法：
  git stash push -- config.py library/    # 回到修复前（第 1 批已完成的状态）
  python tests/security_probe.py
  git stash pop                           # 回到修复后
  python tests/security_probe.py

四个场景对应评审文档里的 P0-4 ~ P0-6：
  1. 伪造 X-Forwarded-For 能不能换一个新的限流桶（锁定是否形同虚设）；
  2. 同一手机号能否被多个读者持有；
  3. 读者能否把自己的手机号设成别人的借书证号；
  4. 上面那个状态下，「证号」这个登录标识到底解析到哪位读者；
  5. 两套未显式配置密钥的部署，会话密钥是否可预测（可预测即可伪造管理员会话）。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as config_module  # noqa: E402
from config import Config, set_config  # noqa: E402
from library import db  # noqa: E402
from library.auth import service as auth_service  # noqa: E402
from library.members import repository as member_repo  # noqa: E402
from library.members import service as member_service  # noqa: E402
from library.web import create_app  # noqa: E402

ADMIN_PWD = "probe-admin-pw"
MAX_ATTEMPTS = 3


def make_member(card_no: str, phone: str, name: str) -> dict:
    return member_service.create_member(
        {
            "card_no": card_no,
            "name": name,
            "phone": phone,
            "member_type": "学生",
            "status": "active",
        }
    )


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="security-probe-")
    cfg = Config(
        db_path=os.path.join(tmp, "probe.db"),
        host="127.0.0.1",
        port=8000,
        debug=False,
        loan_days=30,
        max_renew=2,
        fine_per_day=0.5,
        default_max_borrow=5,
        log_level="ERROR",
        secret_key="probe-secret-key-0123456789",
        admin_user="admin",
        admin_password=ADMIN_PWD,
        login_max_attempts=MAX_ATTEMPTS,
        login_lock_minutes=5,
    )
    app = create_app(cfg)
    client = app.test_client()
    verdicts: list[bool] = []

    # ---------------------------------------------------------------- 场景 1
    auth_service.login_throttle.reset()
    codes = [
        client.post(
            "/api/auth/login",
            json={"identifier": "victim", "password": "x"},
            headers={"X-Forwarded-For": f"10.9.9.{i}"},
        ).status_code
        for i in range(MAX_ATTEMPTS + 4)
    ]
    locked = 422 in codes
    verdicts.append(locked)
    print("[场景1] 每次换一个伪造的 X-Forwarded-For 去撞同一个账号")
    print(f"        共 {len(codes)} 次尝试的状态码 = {codes}   期望：超过 {MAX_ATTEMPTS} 次后出现 422（锁定）")
    print(f"        {'✅ 正确：伪造请求头换不了限流桶，锁定生效' if locked else '❌ 漏洞：换个伪造头就是一个新桶，锁定形同虚设'}")

    # ---------------------------------------------------------------- 场景 2
    dup_rejected = False
    try:
        make_member("9900001", "13500000001", "探针甲")
        make_member("9900002", "13500000001", "探针乙")
    except Exception as exc:  # noqa: BLE001
        dup_rejected = True
        print(f"        （第 2 位读者被拒：{type(exc).__name__}）")
    holders = int(
        db.query_scalar("SELECT COUNT(*) FROM members WHERE phone = '13500000001'") or 0
    )
    good = dup_rejected and holders == 1
    verdicts.append(good)
    print("[场景2] 两个读者用同一个手机号建档")
    print(f"        该手机号的持有者数量 = {holders}   期望：1（登录标识必须唯一）")
    print(f"        {'✅ 正确：重复手机号被拒' if good else '❌ 漏洞：同一手机号被 ' + str(holders) + ' 个读者持有，登录身份无法判定'}")

    # ---------------------------------------------------------------- 场景 3 / 4
    intruder = make_member("9900003", "13500000003", "探针丙")
    victim = make_member("9900004", "13500000004", "探针丁")
    hijack_rejected = False
    try:
        member_service.update_member(
            int(intruder["id"]),
            {
                "card_no": "9900003",
                "name": "探针丙",
                "phone": "9900004",  # 抢占下一位读者的借书证号
                "member_type": "学生",
                "status": "active",
            },
        )
    except Exception as exc:  # noqa: BLE001
        hijack_rejected = True
        print(f"        （抢占被拒：{type(exc).__name__}）")
    verdicts.append(hijack_rejected)
    print("[场景3] 读者把手机号改成另一位读者的借书证号")
    print(f"        {'✅ 正确：被拒绝' if hijack_rejected else '❌ 漏洞：允许，两个登录标识指向同一段字符串'}")

    resolved = member_repo.get_for_auth_by_account("9900004")
    resolved_card = resolved["card_no"] if resolved else None
    good = resolved is None or resolved_card == "9900004"
    verdicts.append(good)
    print("[场景4] 用「9900004」这个账号字符串查登录身份")
    print(f"        解析到证号 = {resolved_card}   期望：9900004（或不存在）")
    print(f"        {'✅ 正确：标识归属确定' if good else '❌ 漏洞：证号 9900004 的登录被解析到读者 ' + str(resolved_card)}")

    # ---------------------------------------------------------------- 场景 5
    saved_env = {
        name: os.environ.get(name)
        for name in ("LIBRARY_SECRET_KEY", "LIBRARY_DEBUG", "LIBRARY_DB_PATH")
    }
    try:
        os.environ.pop("LIBRARY_SECRET_KEY", None)
        os.environ["LIBRARY_DEBUG"] = "false"
        os.environ["LIBRARY_DB_PATH"] = os.path.join(tmp, "install-a", "library.db")
        key_a = config_module.load_config().secret_key
        os.environ["LIBRARY_DB_PATH"] = os.path.join(tmp, "install-b", "library.db")
        key_b = config_module.load_config().secret_key
    finally:
        for env_name, env_value in saved_env.items():
            if env_value is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = env_value

    same_key = key_a == key_b
    good = not same_key and len(key_a) >= 32
    verdicts.append(good)
    print("[场景5] 两套都没配 LIBRARY_SECRET_KEY 的部署各自启动")
    print(f"        两次密钥是否相同 = {same_key}（长度 {len(key_a)} / {len(key_b)}）   期望：不同")
    print(f"        {'✅ 正确：各部署独立随机密钥，猜不到' if good else '❌ 漏洞：默认密钥固定可预测，可据此伪造管理员会话'}")

    print(f"\n小结：{sum(verdicts)}/5 个场景通过")
    return 0 if all(verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
