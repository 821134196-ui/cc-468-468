"""器材排他：同一器材不能被两个入口同时领取；并发领件不重叠。"""
from __future__ import annotations

import threading

from app.services import DomainError
from tests import ServiceCase


class TestEquipmentExclusivity(ServiceCase):
    def _task_equipment(self, task_id: int) -> set[int]:
        rows = self.conn.execute(
            "SELECT board_id id FROM task_boards WHERE task_id=? AND board_id IS NOT NULL "
            "UNION ALL SELECT post_id FROM task_posts WHERE task_id=? AND post_id IS NOT NULL "
            "UNION ALL SELECT seal_id FROM task_boards WHERE task_id=? AND seal_id IS NOT NULL",
            (task_id, task_id, task_id)).fetchall()
        return {r["id"] for r in rows}

    def test_concurrent_claims_assign_disjoint_equipment(self):
        rid = self._round()
        t1 = self._planned(rid, 1)  # 东门 4 段
        t2 = self._planned(rid, 2)  # 西门 3 段
        errors = []
        barrier = threading.Barrier(2)

        def claim(tid, worker):
            barrier.wait()
            try:
                self.svc.claim(tid, worker)
            except DomainError as e:
                errors.append(str(e))

        threads = [threading.Thread(target=claim, args=(t1, 2)),
                   threading.Thread(target=claim, args=(t2, 3))]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        self.assertEqual(errors, [])
        eq1, eq2 = self._task_equipment(t1), self._task_equipment(t2)
        self.assertTrue(eq1 & eq2 == set(), f"器材被重复分配: {eq1 & eq2}")
        self.assertEqual(len(eq1), 4 + 5 + 4)
        self.assertEqual(len(eq2), 3 + 4 + 3)

    def test_same_task_claim_only_once_under_race(self):
        rid = self._round()
        tid = self._planned(rid, 3)  # 北门 2 段
        results = []
        barrier = threading.Barrier(5)

        def claim(worker):
            barrier.wait()
            try:
                self.svc.claim(tid, worker)
                results.append("ok")
            except DomainError:
                results.append("fail")

        threads = [threading.Thread(target=claim, args=(w,)) for w in (2, 3, 2, 3, 6)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(results.count("ok"), 1, "同一任务只允许一次领件成功")
        self.assertEqual(results.count("fail"), 4)

    def test_insufficient_equipment_blocks_and_leaves_no_lock(self):
        rid = self._round()
        t1 = self._planned(rid, 2)  # 西门 3
        self.svc.claim(t1, 2)
        t2 = self._planned(rid, 3)  # 北门 2
        self.svc.claim(t2, 3)       # 已用 5 板 DB-01~05
        # 再报废 5 块备用板（DB-06~10），只剩 DB-11/12 两块可用
        self.conn.execute(
            "UPDATE equipment SET status='repair' WHERE kind='board' "
            "AND code IN ('DB-06','DB-07','DB-08','DB-09','DB-10')")
        self.conn.commit()
        t3 = self.svc.generate_checklist(rid, 1)  # 东门需 4 块
        with self.assertRaises(DomainError):
            self.svc.claim(t3, 2)
        # 失败任务不得锁定任何器材，任务仍停留在 planned
        locked = self.conn.execute(
            "SELECT COUNT(*) c FROM equipment WHERE active_task_id=?", (t3,)).fetchone()["c"]
        self.assertEqual(locked, 0)
        status = self.conn.execute("SELECT status FROM tasks WHERE id=?", (t3,)).fetchone()["status"]
        self.assertEqual(status, "planned")

    def test_equipment_status_after_full_flow(self):
        rid = self._round()
        tid = self._installed(rid, 1)
        rows = self.conn.execute(
            "SELECT DISTINCT status FROM equipment WHERE active_task_id=?", (tid,)).fetchall()
        self.assertEqual([r["status"] for r in rows], ["deployed"])
