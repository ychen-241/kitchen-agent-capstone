"""MVP 验收测试：三条验收路径 + 登录 + 知识库问答。

运行（项目根目录）：  python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["KITCHEN_AGENT_DB"] = os.path.join(tempfile.mkdtemp(prefix="kag_test_"), "test.db")
os.environ["KITCHEN_AGENT_JWT_SECRET"] = "unittest-secret"

from app.models import db  # noqa: E402
from app.core import security  # noqa: E402
from app import services  # noqa: E402
from app.agents import orchestrator  # noqa: E402
from app.llm import base as llm_base  # noqa: E402

db.init_db()
CONN = db.get_conn()


class TestAuth(unittest.TestCase):
    def test_password_and_jwt(self):
        self.assertTrue(security.verify_password("admin123",
                        security.hash_password("admin123")))
        self.assertFalse(security.verify_password("wrong",
                        security.hash_password("admin123")))
        token = security.jwt_encode({"sub": "admin", "uid": 1, "role": "admin"})
        payload = security.jwt_decode(token)
        self.assertEqual(payload["role"], "admin")
        self.assertIsNone(security.jwt_decode(token[:-2] + "xx"))  # 篡改拒绝

    def test_roles_seeded(self):
        rows = CONN.execute("SELECT username, role FROM users").fetchall()
        roles = {r["username"]: r["role"] for r in rows}
        self.assertEqual(roles["admin"], "admin")
        self.assertEqual(roles["qianting"], "employee")


class TestPath1Blocked(unittest.TestCase):
    """验收路径一：过敏冲突 BLOCK 拦截 + 食安签核放行。"""

    def test_allergy_block_and_signoff(self):
        raw = "3号桌，一份宫保鸡丁对花生过敏，一份蒜蓉西兰花"
        result = services.create_order(CONN, raw, "system", "", "qianting")
        self.assertEqual(result["status"], "blocked")
        self.assertIn("过敏冲突", result["reason"])
        # 修复队列有记录，责任人 = 食安主管
        repair = CONN.execute(
            "SELECT * FROM repair_queue WHERE biz_no=? ORDER BY id DESC LIMIT 1", (result["biz_no"],)
        ).fetchone()
        self.assertEqual(repair["assigned_to"], "食安主管")
        # 被拦截条目状态正确
        blocked = CONN.execute(
            "SELECT i.dish_name FROM order_items i JOIN orders o ON o.id=i.order_id "
            "WHERE o.biz_no=? AND i.status='blocked'", (result["biz_no"],)
        ).fetchall()
        self.assertEqual([r["dish_name"] for r in blocked], ["宫保鸡丁"])
        # 事件留痕
        events = services.audit_query(CONN, result["biz_no"])
        self.assertTrue(any(e["event_type"] == "order.blocked" for e in events))
        # 食安签核放行：剔除宫保鸡丁，其余进看板
        release = services.signoff(CONN, result["biz_no"], "admin")
        self.assertEqual(release["removed"], ["宫保鸡丁"])
        self.assertEqual(release["scheduled"], 1)
        order = CONN.execute("SELECT status FROM orders WHERE biz_no=?", (result["biz_no"],)).fetchone()
        self.assertEqual(order["status"], "scheduled")


class TestPath2Normal(unittest.TestCase):
    """验收路径二：正常下单 + 催菜重排带理由 + 全流程状态机。"""

    def test_order_urge_close(self):
        result = services.create_order(CONN, "5号桌，一份麻婆豆腐，一份番茄蛋汤", "system", "", "qianting")
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(result["scheduled"], 2)
        board = services.board(CONN)
        tasks = [t for t in board if t["biz_no"] == result["biz_no"]]
        self.assertEqual(len(tasks), 2)
        for t in tasks:
            self.assertTrue(t["reason"], "每条排序必须带理由")

        # 催菜：分数上升、理由更新
        before = {t["dish_name"]: t for t in tasks}
        urge = services.urge(CONN, result["biz_no"], "qianting")
        self.assertIn("重排", urge["notice"])
        board2 = services.board(CONN)
        after = {t["dish_name"]: t for t in board2 if t["biz_no"] == result["biz_no"]}
        self.assertGreater(after["麻婆豆腐"]["score"], before["麻婆豆腐"]["score"])
        self.assertIn("催菜 1 次", after["麻婆豆腐"]["reason"])

        # 同单催菜去重
        urge2 = services.urge(CONN, result["biz_no"], "qianting")
        self.assertIn("去重", urge2.get("notice", ""))

        # 状态机：开工 → 完工 → 上桌结单
        self.assertEqual(services.cook(CONN, result["biz_no"], "chushi")["status"], "cooking")
        self.assertEqual(services.complete(CONN, result["biz_no"], "chushi")["status"], "ready")
        closed = services.serve(CONN, result["biz_no"], "qianting")
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["missing_items"], 0)
        events = services.audit_query(CONN, result["biz_no"])
        types = [e["event_type"] for e in events]
        for expected in ("order.scheduled", "order.urge", "order.cooking", "order.ready", "order.closed"):
            self.assertIn(expected, types)


class TestPath3Degraded(unittest.TestCase):
    """验收路径三：LLM 挂掉 → 规则兜底，置信度封顶 0.5，转人工队列。"""

    def test_llm_down_fallback(self):
        class Broken:
            name = "broken-llm"
            def parse(self, *a, **k):
                raise RuntimeError("api down")
            def ask(self, *a, **k):
                raise RuntimeError("api down")

        with mock.patch.object(llm_base, "get_provider", lambda: Broken()):
            result = services.create_order(CONN, "7号桌一份红烧肉", "voice", "", "qianting")
        self.assertEqual(result["status"], "scheduled")
        self.assertEqual(result["provider"], "rule-stub")
        self.assertLessEqual(result["confidence"], 0.5)
        # 低置信度进修复队列
        repair = CONN.execute(
            "SELECT * FROM repair_queue WHERE biz_no=? AND reason LIKE '%置信度%'", (result["biz_no"],)
        ).fetchone()
        self.assertIsNotNone(repair)


class TestKnowledgeBase(unittest.TestCase):
    """知识库：向量化检索问答 + 管理员上传新文档。"""

    def test_qa_on_seed_docs(self):
        out = services.qa(CONN, "红烧肉的标准制作时间是多少？")
        self.assertIn("25", out["answer"])
        self.assertTrue(out["citations"])

    def test_allergen_doc_qa(self):
        out = services.qa(CONN, "宫保鸡丁含什么过敏原？")
        self.assertIn("花生", out["answer"])

    def test_admin_upload_new_doc(self):
        doc = services.add_document(CONN, "TestDoc", "工艺卡",
                                    "冬阴功汤：海鲜档，标准制作时间12分钟，含虾与香茅。", "admin")
        self.assertEqual(doc["status"], "published")
        out = services.qa(CONN, "冬阴功汤制作时间")
        self.assertIn("12", out["answer"])


class TestParserRules(unittest.TestCase):
    def test_rule_parse_without_key(self):
        # 未配 Key：get_provider 返回 stub，桌号/份数/备注解析正确
        parsed = orchestrator.parser_agent.parse(CONN, "2号桌，两份清蒸鲈鱼不要辣", "paper")
        self.assertEqual(parsed["table_no"], "2")
        self.assertEqual(parsed["items"][0]["dish_name"], "清蒸鲈鱼")
        self.assertEqual(parsed["items"][0]["qty"], 2)
        self.assertEqual(parsed["items"][0]["notes"], "不要辣")


class TestPatrolAgent(unittest.TestCase):
    """巡检 Agent：自主发现超时修复单 / 档口积压，只发预警不改数据，且去重。"""

    def test_repair_overdue_detected_once(self):
        from app.agents import patrol_agent
        now = __import__("time").time()
        # 造一张早已超时的修复单
        CONN.execute(
            "INSERT INTO repair_queue (biz_no, reason, detail, assigned_to, deadline_at) VALUES (?,?,?,?,?)",
            ("RTESTOVERDUE", "低置信度解析", "测试超时", "值班经理", now - 3600),
        )
        CONN.commit()
        warnings = patrol_agent.run_once(CONN, now=now)
        overdue = [w for w in warnings if w["type"] == "patrol.repair_overdue"]
        self.assertTrue(overdue, "超时修复单应被巡检发现")
        # 只追加事件流里有 patrol 事件，业务表未被修改
        events = services.audit_query(CONN)
        self.assertTrue(any(e["event_type"] == "patrol.repair_overdue" for e in events))
        repair = CONN.execute("SELECT status FROM repair_queue WHERE biz_no='RTESTOVERDUE'").fetchone()
        self.assertEqual(repair["status"], "open", "巡检是只读的，不得改修复单状态")
        # 去重：同窗口内第二轮不再报
        warnings2 = [w for w in patrol_agent.run_once(CONN, now=now + 1)
                     if w["type"] == "patrol.repair_overdue" and w["biz_no"] == "RTESTOVERDUE"]
        self.assertEqual(warnings2, [])

    def test_station_backlog_detected(self):
        from app.agents import patrol_agent
        import time as _t
        result = services.create_order(CONN, "9号桌，一份红烧肉", "system", "", "qianting")
        item = CONN.execute(
            "SELECT i.id, d.station, i.dish_name FROM order_items i JOIN dishes d ON d.id=i.dish_id "
            "WHERE i.order_id=(SELECT id FROM orders WHERE biz_no=?)", (result["biz_no"],)
        ).fetchone()
        # 灌到阈值：同档口再加 5 条排队任务
        for _ in range(5):
            CONN.execute(
                "INSERT INTO board_tasks (order_item_id, order_id, station, dish_name, status) VALUES (?,?,?,?,'queued')",
                (item["id"], CONN.execute("SELECT id FROM orders WHERE biz_no=?", (result["biz_no"],)).fetchone()["id"],
                 item["station"], item["dish_name"]),
            )
        CONN.commit()
        warnings = patrol_agent.run_once(CONN, now=_t.time())
        backlog = [w for w in warnings if w["type"] == "patrol.station_backlog" and w["payload"]["station"] == item["station"]]
        self.assertTrue(backlog, f"档口 {item['station']} 积压应被巡检发现")


if __name__ == "__main__":
    unittest.main(verbosity=2)
