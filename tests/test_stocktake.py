"""归架差异：盘点缺/损件须主管报损补充；未处理差异不能完成；并发完成只生效一次。"""
from __future__ import annotations

import threading

from app.services import DomainError
from tests import ServiceCase


class TestStocktake(ServiceCase):
    def _dried_task(self, ent=3):
        """走到晾干上架、等待归架盘点的任务。"""
        rid = self._round()
        tid = self._installed(rid, ent)
        self.svc.lift_round(rid, "normal")
        self.svc.dismantle(tid, 2)
        self.svc.mark_cleaned(tid, 2, "清洗")
        self.svc.mark_dried(tid, 2)
        return tid

    def test_cannot_finalize_with_unchecked_items(self):
        tid = self._dried_task(3)
        with self.assertRaises(DomainError):
            self.svc.finalize_stocktake(tid, 2)

    def test_missing_item_must_be_reported_before_finalize(self):
        tid = self._dried_task(3)
        items = self.conn.execute(
            "SELECT si.id, e.code, e.kind FROM stocktake_items si "
            "JOIN equipment e ON e.id=si.equipment_id WHERE si.task_id=? ORDER BY si.id",
            (tid,)).fetchall()
        seal_item = next(i for i in items if i["kind"] == "seal")
        for it in items:
            state = "missing" if it["id"] == seal_item["id"] else "present"
            self.svc.check_stock_item(it["id"], 2, state,
                                      note="归架未找到" if state == "missing" else "")
        # 差异未报损 → 不能完成
        with self.assertRaises(DomainError):
            self.svc.finalize_stocktake(tid, 2)
        # 防汛员不能报损
        with self.assertRaises(DomainError):
            self.svc.report_loss(seal_item["id"], 2, "工人越权")
        # 主管报损 → 开补充单
        oid = self.svc.report_loss(seal_item["id"], 6, "胶条老化撕裂，无法继续使用")
        # 仍不能完成？差异已有补充单但未入库 —— 业务允许以「已开单」状态完成
        summary = self.svc.finalize_stocktake(tid, 2)
        self.assertEqual(summary["differences"], 1)

    def test_damaged_item_reported_and_replacement_received(self):
        tid = self._dried_task(3)
        items = self.conn.execute(
            "SELECT si.id, e.code, e.kind FROM stocktake_items si "
            "JOIN equipment e ON e.id=si.equipment_id WHERE si.task_id=? ORDER BY si.id",
            (tid,)).fetchall()
        board_item = next(i for i in items if i["kind"] == "board")
        for it in items:
            state = "damaged" if it["id"] == board_item["id"] else "present"
            self.svc.check_stock_item(it["id"], 6, state,
                                      note="板面磕碰变形" if state == "damaged" else "")
        oid = self.svc.report_loss(board_item["id"], 6, "挡板撞击变形")
        # 补充新器材入库，恢复可用
        new_id = self.svc.receive_replacement(oid, "DB-99", 6)
        row = self.conn.execute("SELECT status, kind FROM equipment WHERE id=?",
                                (new_id,)).fetchone()
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["kind"], "board")
        # 重号入库被拒
        with self.assertRaises(DomainError):
            self.svc.receive_replacement(oid, "DB-02", 6)
        # 全部在位 + 已报损 → 完成归架
        summary = self.svc.finalize_stocktake(tid, 6)
        self.assertEqual(summary["total"], 2 + 3 + 2)

    def test_equipment_becomes_available_after_stocktake(self):
        tid = self._dried_task(3)
        items = self.conn.execute(
            "SELECT id FROM stocktake_items WHERE task_id=? ORDER BY id", (tid,)).fetchall()
        for it in items:
            self.svc.check_stock_item(it["id"], 2, "present")
        self.svc.finalize_stocktake(tid, 2)
        leftovers = self.conn.execute(
            "SELECT COUNT(*) c FROM equipment WHERE active_task_id=?", (tid,)).fetchone()["c"]
        self.assertEqual(leftovers, 0)
        statuses = {r["status"] for r in self.conn.execute(
            "SELECT DISTINCT status FROM equipment WHERE id IN "
            "(SELECT equipment_id FROM stocktake_items WHERE task_id=?)", (tid,)).fetchall()}
        self.assertEqual(statuses, {"available"})

    def test_concurrent_finalize_succeeds_only_once(self):
        tid = self._dried_task(3)
        items = self.conn.execute(
            "SELECT id FROM stocktake_items WHERE task_id=? ORDER BY id", (tid,)).fetchall()
        for it in items:
            self.svc.check_stock_item(it["id"], 2, "present")
        outcomes = []
        barrier = threading.Barrier(4)

        def finalize(worker):
            barrier.wait()
            try:
                self.svc.finalize_stocktake(tid, worker)
                outcomes.append("ok")
            except DomainError:
                outcomes.append("fail")

        threads = [threading.Thread(target=finalize, args=(w,)) for w in (2, 3, 2, 6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(outcomes.count("ok"), 1, "并发完成归架只允许一次")
        self.assertEqual(outcomes.count("fail"), 3)
        task = self.conn.execute("SELECT status, done_at FROM tasks WHERE id=?",
                                 (tid,)).fetchone()
        self.assertEqual(task["status"], "done")
        self.assertIsNotNone(task["done_at"])
        # 完成后再次核对被拒
        with self.assertRaises(DomainError):
            self.svc.finalize_stocktake(tid, 2)
