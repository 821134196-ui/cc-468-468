"""分段完整：连接/密封逐段确认，未齐不能封控；错段纠正与密封条破损处理。"""
from __future__ import annotations

from app.services import DomainError
from tests import ServiceCase


class TestSegmentCompleteness(ServiceCase):
    def _claimed_task(self, ent=1):
        rid = self._round()
        tid = self._planned(rid, ent)
        self.svc.claim(tid, 2)
        self.svc.begin_install(tid, 2)
        return tid

    def test_closure_blocked_until_all_segments_confirmed(self):
        tid = self._claimed_task(1)  # 4 段
        self.svc.confirm_connection(tid, 0, 2)
        self.svc.confirm_seal(tid, 0, 2)
        self.svc.confirm_connection(tid, 1, 2)
        self.svc.confirm_seal(tid, 1, 2)
        self.svc.confirm_connection(tid, 2, 2)
        self.svc.confirm_seal(tid, 2, 2)
        # 第 4 段（slot 3）未确认 → 保安封控必须被拒
        with self.assertRaises(DomainError):
            self.svc.confirm_closure(tid, 1)
        self.svc.confirm_connection(tid, 3, 2)
        self.svc.confirm_seal(tid, 3, 2)
        self.svc.confirm_closure(tid, 1)  # 全部确认后封控成功
        status = self.conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"]
        self.assertEqual(status, "watch")

    def test_worker_cannot_control_closure_but_guard_can(self):
        tid = self._claimed_task(3)  # 2 段
        for slot in range(2):
            self.svc.confirm_connection(tid, slot, 2)
            self.svc.confirm_seal(tid, slot, 2)
        with self.assertRaises(DomainError):
            self.svc.confirm_closure(tid, 2)  # 防汛员无权封控
        self.svc.confirm_closure(tid, 1)      # 保安可执行

    def test_wrong_segment_swap_and_reconfirm(self):
        tid = self._claimed_task(1)
        original = self.conn.execute(
            "SELECT board_id FROM task_boards WHERE task_id=? AND slot=0", (tid,)).fetchone()["board_id"]
        self.svc.confirm_connection(tid, 0, 2)
        # 用未占用的备用板 DB-09（东门 4 段只占 DB-01~04）纠正错段
        self.svc.fix_wrong_segment(tid, 0, "DB-09", 2)
        row = self.conn.execute(
            "SELECT board_id, conn_confirmed, seal_confirmed FROM task_boards "
            "WHERE task_id=? AND slot=0", (tid,)).fetchone()
        self.assertNotEqual(row["board_id"], original)
        self.assertEqual(row["conn_confirmed"], 0)
        self.assertEqual(row["seal_confirmed"], 0)
        # 原板回到可用池，可被其他入口领取
        self.assertEqual(self.conn.execute(
            "SELECT status FROM equipment WHERE id=?", (original,)).fetchone()["status"], "available")
        # 已锁定的板不能作为替换板
        with self.assertRaises(DomainError):
            self.svc.fix_wrong_segment(tid, 1, "DB-09", 2)

    def test_seal_damage_auto_swaps_spare(self):
        tid = self._claimed_task(1)
        old_seal = self.conn.execute(
            "SELECT seal_id FROM task_boards WHERE task_id=? AND slot=1", (tid,)).fetchone()["seal_id"]
        self.svc.confirm_seal(tid, 1, 2)
        msg = self.svc.report_seal_damage(tid, 1, 2, "胶面开裂")
        self.assertIn("SEAL-05", msg)  # 东门占用 SEAL-01~04，备用第一条为 SEAL-05
        self.assertEqual(self.conn.execute(
            "SELECT status FROM equipment WHERE id=?", (old_seal,)).fetchone()["status"], "repair")
        # 换条后需重新密封确认
        self.assertEqual(self.conn.execute(
            "SELECT seal_confirmed FROM task_boards WHERE task_id=? AND slot=1",
            (tid,)).fetchone()["seal_confirmed"], 0)
        self.svc.confirm_seal(tid, 1, 2)

    def test_seal_damage_without_spare_blocks_confirm(self):
        # 占用全部密封条后再报破损 → 挂缺件，密封不可确认
        rid = self._round()
        t1 = self.svc.generate_checklist(rid, 1)
        self.svc.claim(t1, 2)  # 4 条
        t2 = self.svc.generate_checklist(rid, 2)
        self.svc.claim(t2, 3)  # 3 条
        t3 = self.svc.generate_checklist(rid, 3)
        self.svc.claim(t3, 6)  # 2 条 → 用掉 9 条
        # 报废剩余 3 条备用
        self.conn.execute(
            "UPDATE equipment SET status='repair' WHERE kind='seal' AND status='available'")
        self.conn.commit()
        msg = self.svc.report_seal_damage(t1, 0, 2)
        self.assertIn("挂缺件", msg)
        with self.assertRaises(DomainError):
            self.svc.confirm_seal(t1, 0, 2)
