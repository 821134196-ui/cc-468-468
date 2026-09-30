"""提前拆除审批：未解除禁止拆除；主管审批后可拆；防汛员/保安无权审批。"""
from __future__ import annotations

from app.services import DomainError
from tests import ServiceCase


class TestEarlyDismantle(ServiceCase):
    def test_dismantle_blocked_while_round_active(self):
        rid = self._round()
        tid = self._installed(rid, 1)
        with self.assertRaises(DomainError):
            self.svc.dismantle(tid, 2)
        task = self.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(task["status"], "watch")

    def test_only_supervisor_may_approve_early(self):
        rid = self._round()
        tid = self._installed(rid, 1)
        with self.assertRaises(DomainError):
            self.svc.approve_early_dismantle(tid, 2, "工人越权")  # 防汛员
        with self.assertRaises(DomainError):
            self.svc.approve_early_dismantle(tid, 1, "保安越权")  # 保安
        with self.assertRaises(DomainError):
            self.svc.approve_early_dismantle(tid, 6, "")         # 缺原因
        self.svc.approve_early_dismantle(tid, 6, "雨带偏离，恢复通行")
        # 审批唯一，重复审批被拒
        with self.assertRaises(DomainError):
            self.svc.approve_early_dismantle(tid, 6, "再次审批")

    def test_approved_early_dismantle_flow(self):
        rid = self._round()
        tid = self._installed(rid, 1)
        self.svc.approve_early_dismantle(tid, 6, "雨带偏离")
        self.svc.dismantle(tid, 2)
        task = self.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(task["status"], "removed_early")
        # 清洗 → 晾干 → 盘点 → 闭环
        self.svc.mark_cleaned(tid, 2, "冲洗")
        self.svc.mark_dried(tid, 2)
        items = self.conn.execute(
            "SELECT id FROM stocktake_items WHERE task_id=? ORDER BY id", (tid,)).fetchall()
        for it in items:
            self.svc.check_stock_item(it["id"], 2, "present")
        summary = self.svc.finalize_stocktake(tid, 2)
        self.assertEqual(summary["differences"], 0)
        # 预警仍未解除（提前拆除），主管事后解除
        rnd = self.conn.execute("SELECT status FROM rounds WHERE id=?", (rid,)).fetchone()
        self.assertEqual(rnd["status"], "active")
        self.svc.lift_round(rid, "early", 6, "现场复查无积水")

    def test_normal_dismantle_requires_lifted(self):
        rid = self._round()
        tid = self._installed(rid, 3)
        with self.assertRaises(DomainError):
            self.svc.dismantle(tid, 6)  # 主管也不能无审批直接拆
        self.svc.lift_round(rid, "normal")
        self.svc.dismantle(tid, 6)      # 解除后即可拆
        task = self.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(task["status"], "cleaning")
