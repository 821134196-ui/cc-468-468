"""核心规则测试：
器材排他、分段完整、预警状态、提前拆除审批、归架差异；
另含错段、密封条破损、积水阻拆、并发完成幂等等场景。
"""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flood.db import DB
from flood.service import Service, DomainError
from flood.weather import MockWeatherAdapter
from flood.scheduler import Scheduler


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")
        self.svc = Service(self.db)
        self._seed()

    def _seed(self):
        db = self.db
        db.execute("INSERT INTO entrance(id,code,name,slot_specs,sort_no) VALUES (1,'E','东门',?,1)",
                   (json.dumps([2.0, 1.5]),))
        db.execute("INSERT INTO entrance(id,code,name,slot_specs,sort_no) VALUES (2,'W','西门',?,2)",
                   (json.dumps([2.0]),))
        for i, (n, r) in enumerate([
                ("王主管", "supervisor"), ("李安装", "installer"),
                ("赵安装", "installer"), ("陈保安", "guard")], start=1):
            db.execute("INSERT INTO person(id,name,role) VALUES (?,?,?)", (i, n, r))
        for i in range(1, 5):
            db.execute("INSERT INTO item(code,kind,width,rack_code,rack_pos,status) "
                       "VALUES ('SG20-%02d','segment',2.0,'A','2m-%d','available')" % (i, i))
        for i in range(1, 4):
            db.execute("INSERT INTO item(code,kind,width,rack_code,rack_pos,status) "
                       "VALUES ('SG15-%02d','segment',1.5,'A','15m-%d','available')" % (i, i))
        for i in range(1, 8):
            db.execute("INSERT INTO item(code,kind,status) VALUES ('POST-%02d','post','available')" % i)
        for i in range(1, 8):
            db.execute("INSERT INTO item(code,kind,status) VALUES ('SEAL-%02d','seal','available')" % i)

    # 辅助：领齐一个任务
    def _pick_all(self, tid, pid=2):
        for line in self.db.q("SELECT * FROM task_line WHERE task_id=? AND status='pending' ORDER BY id",
                              (tid,)):
            width = None
            if line["line_type"] == "segment":
                width = self.db.one(
                    "SELECT expected_width FROM task_slot WHERE task_id=? AND slot_no=?",
                    (tid, line["slot_no"]))["expected_width"]
            item = self.svc._available_item(line["line_type"], width)
            self.svc.pick_line(tid, line["id"], item["code"], pid)
        self.svc.finish_picking(tid, pid)

    def _confirm_all(self, tid, installer=2, guard=4):
        n = self.db.one("SELECT COUNT(*) c FROM task_slot WHERE task_id=?", (tid,))["c"]
        for s in range(1, n + 1):
            self.svc.confirm_slot(tid, s, "conn", installer)
            self.svc.confirm_slot(tid, s, "seal", installer)
            self.svc.confirm_slot(tid, s, "ctrl", guard)

    # ----------------------------------------------------------- 1 器材排他
    def test_item_exclusive_across_entrances(self):
        t1 = self.svc.generate_task(1, "YELLOW")
        t2 = self.svc.generate_task(2, "YELLOW")
        # 东门第一件 2.0m 领走 SG20-01
        line1 = self.db.one(
            "SELECT id FROM task_line WHERE task_id=? AND line_type='segment' AND slot_no=1", (t1,))
        self.svc.pick_line(t1, line1["id"], "SG20-01", 2)
        self.assertEqual(self.db.one("SELECT status FROM item WHERE code='SG20-01'")["status"],
                         "locked")
        # 西门试图领取同一件 → 拒绝
        line2 = self.db.one(
            "SELECT id FROM task_line WHERE task_id=? AND line_type='segment' AND slot_no=1", (t2,))
        with self.assertRaises(DomainError) as cx:
            self.svc.pick_line(t2, line2["id"], "SG20-01", 3)
        self.assertIn("不能重复发放", str(cx.exception))
        # 另一件可以正常领
        self.svc.pick_line(t2, line2["id"], "SG20-02", 3)
        self.assertEqual(self.db.one("SELECT holder_task_id FROM item WHERE code='SG20-02'")["holder_task_id"], t2)
        # 同任务内重复领同一行也被拒绝
        with self.assertRaises(DomainError):
            self.svc.pick_line(t1, line1["id"], "SG20-03", 2)

    def test_wrong_segment_rejected(self):
        t = self.svc.generate_task(1, "YELLOW")
        # 东门 2 号槽需 1.5m，扫 2.0m 挡板 → 错段
        line = self.db.one(
            "SELECT id FROM task_line WHERE task_id=? AND line_type='segment' AND slot_no=2", (t,))
        with self.assertRaises(DomainError) as cx:
            self.svc.pick_line(t, line["id"], "SG20-01", 2)
        self.assertIn("挡板错段", str(cx.exception))
        # 记录了错段异常且器材未被锁定
        self.assertTrue(self.db.one(
            "SELECT 1 FROM incident WHERE type='wrong_segment'"))
        self.assertEqual(self.db.one("SELECT status FROM item WHERE code='SG20-01'")["status"],
                         "available")

    # --------------------------------------------------------- 2 分段完整性
    def test_segment_completeness_required(self):
        t = self.svc.generate_task(1, "YELLOW")
        self._pick_all(t)
        # 未确认不能完成
        with self.assertRaises(DomainError) as cx:
            self.svc.complete_install(t, 2)
        self.assertIn("分段安装不完整", str(cx.exception))
        self.svc.confirm_slot(t, 1, "conn", 2)
        self.svc.confirm_slot(t, 1, "seal", 2)
        with self.assertRaises(DomainError):  # 封控需保安
            self.svc.confirm_slot(t, 1, "ctrl", 2)
        self.svc.confirm_slot(t, 1, "ctrl", 4)
        with self.assertRaises(DomainError) as cx:  # 2 号槽未做
            self.svc.complete_install(t, 2)
        self.assertIn("2号槽", str(cx.exception))
        self.svc.confirm_slot(t, 2, "conn", 2)
        self.svc.confirm_slot(t, 2, "seal", 2)
        self.svc.confirm_slot(t, 2, "ctrl", 4)
        self.assertTrue(self.svc.complete_install(t, 2)["ok"])
        self.assertEqual(self.db.one("SELECT status FROM task WHERE id=?", (t,))["status"],
                         "installed")

    def test_concurrent_complete_only_once(self):
        t = self.svc.generate_task(1, "YELLOW")
        self._pick_all(t)
        self._confirm_all(t)
        ok, errs = [], []

        def run():
            try:
                self.svc.complete_install(t, 2)
                ok.append(1)
            except DomainError:
                errs.append(1)
        threads = [threading.Thread(target=run) for _ in range(10)]
        [x.start() for x in threads]
        [x.join() for x in threads]
        self.assertEqual(len(ok), 1)
        self.assertEqual(len(errs), 9)

    def test_seal_damage_blocks_and_replace(self):
        t = self.svc.generate_task(2, "YELLOW")
        self._pick_all(t)
        self.svc.confirm_slot(t, 1, "conn", 2)
        self.svc.mark_seal_damage(t, 1, 2, "撕裂")
        # 破损未更换验收，不能密封确认
        with self.assertRaises(DomainError):
            self.svc.confirm_slot(t, 1, "seal", 2)
        self.svc.seal_replaced_ok(t, 1, 2)
        self.svc.confirm_slot(t, 1, "seal", 2)
        self.svc.confirm_slot(t, 1, "ctrl", 4)
        self.svc.complete_install(t, 2)

    def test_passage_invalidates_confirmation(self):
        t = self.svc.generate_task(2, "YELLOW")
        self._pick_all(t)
        self._confirm_all(t)
        self.svc.complete_install(t, 2)
        pg = self.svc.open_passage(t, 1, 4, "沪A1234")["passage_id"]
        s = self.db.one("SELECT * FROM task_slot WHERE task_id=? AND slot_no=1", (t,))
        self.assertIsNone(s["conn_at"])
        self.assertIsNone(s["ctrl_at"])
        self.assertEqual(self.db.one("SELECT status FROM task WHERE id=?", (t,))["status"],
                         "installing")
        # 安装工无权开启放行
        with self.assertRaises(DomainError):
            self.svc.open_passage(t, 1, 2, "X")
        self.svc.restore_passage(pg, 4)
        with self.assertRaises(DomainError):  # 未重新确认不能完成
            self.svc.complete_install(t, 2)
        self._confirm_all(t)
        self.svc.complete_install(t, 2)

    # ------------------------------------------------------------ 3 预警状态
    def test_warning_levels_generation_and_clear(self):
        adapter = MockWeatherAdapter()
        sch = Scheduler(self.db, adapter, water_log=False)
        adapter.set_manual(5)
        r = sch.tick_once(persist_water=False)
        self.assertEqual(r["level"], "NONE")
        self.assertIsNone(self.db.one("SELECT 1 FROM task"))
        adapter.set_manual(25)
        r = sch.tick_once(persist_water=False)
        self.assertEqual(r["level"], "YELLOW")
        # 达阈值自动为两个入口生成清单
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM task")["c"], 2)
        adapter.set_manual(55)
        self.assertEqual(sch.tick_once(persist_water=False)["level"], "RED")
        # 再拍一次黄警不会重复生成
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM task")["c"], 2)
        # 连续两次低位复查 → 解除
        adapter.set_manual(10)
        self.assertEqual(sch.tick_once(persist_water=False)["low_ticks"], 1)
        r = sch.tick_once(persist_water=False)
        self.assertGreaterEqual(r["low_ticks"], 2)
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) c FROM task WHERE warning_cleared=1")["c"], 2)

    # -------------------------------------------------------- 4 提前拆除审批
    def _installed_task(self, ent=2):
        t = self.svc.generate_task(ent, "ORANGE")
        self._pick_all(t)
        self._confirm_all(t)
        self.svc.complete_install(t, 2)
        return t

    def test_early_removal_approval_flow(self):
        t = self._installed_task()
        # 预警未解除、无批准 → 拒绝拆除
        with self.assertRaises(DomainError) as cx:
            self.svc.remove(t, 2)
        self.assertIn("预警尚未解除", str(cx.exception))
        ap = self.svc.request_removal(t, 2, "紧急出库")["approval_id"]
        # 安装工不能审批
        with self.assertRaises(DomainError):
            self.svc.decide_removal(ap, 2, True)
        # 主管可驳回；驳回后仍不能拆
        self.svc.decide_removal(ap, 1, False, "风险大")
        with self.assertRaises(DomainError):
            self.svc.remove(t, 2)
        # 重新申请，主管批准后可提前拆除
        ap2 = self.svc.request_removal(t, 2, "应急救援")["approval_id"]
        self.svc.decide_removal(ap2, 1, True, "限定一车")
        r = self.svc.remove(t, 2)
        self.assertTrue(r["early"])
        self.assertEqual(self.db.one("SELECT status FROM task WHERE id=?", (t,))["status"],
                         "cleaning")

    def test_water_blocks_removal(self):
        t = self._installed_task()
        self.db.execute("UPDATE task SET warning_cleared=1 WHERE id=?", (t,))
        self.svc.add_water(2, 18.0, 4)
        with self.assertRaises(DomainError) as cx:
            self.svc.remove(t, 2)
        self.assertIn("禁止拆除", str(cx.exception))
        # 水位退下后可拆
        self.svc.add_water(2, 6.0, 4)
        self.svc.remove(t, 2)

    # ----------------------------------------------------------- 5 归架差异
    def test_rack_diff_loss_report_and_reuse(self):
        t = self._installed_task()
        self.db.execute("UPDATE task SET warning_cleared=1 WHERE id=?", (t,))
        self.svc.remove(t, 2)
        self.svc.confirm_clean(t, 2)
        self.svc.confirm_dry(t, 2)  # 自动建立盘点单
        rc = self.db.one("SELECT * FROM rack_check WHERE task_id=?", (t,))
        expected = json.loads(rc["expected"])
        codes = [r["code"] for r in self.db.q(
            "SELECT code FROM item WHERE id IN (%s) ORDER BY id"
            % ",".join("?" * len(expected)), expected)]
        self.assertGreaterEqual(len(codes), 3)
        # 只扫一部分（模拟缺件），核对报差异
        for c in codes[:-1]:
            self.svc.rack_scan(rc["id"], c, 2)
        with self.assertRaises(DomainError) as cx:
            self.svc.finish_rack(rc["id"], 2)
        self.assertIn("归架差异", str(cx.exception))
        res = self.svc.finish_rack(rc["id"], 2, allow_diff=True)
        missing_codes = {m["code"] for m in res["missing"]}
        self.assertEqual(missing_codes, {codes[-1]})
        # 非主管不能报损
        with self.assertRaises(DomainError):
            self.svc.report_loss(t, codes[-1], 2)
        # 主管报损并补件
        rep = self.svc.report_loss(t, codes[-1], 1, "盘点缺失")
        self.assertEqual(self.db.one("SELECT status FROM item WHERE code=?", (codes[-1],))["status"],
                         "written_off")
        new = self.db.one("SELECT * FROM item WHERE code=?", (rep["new_code"],))
        self.assertEqual(new["status"], "available")
        # 任务结案，器材重新可用
        self.assertEqual(self.db.one("SELECT status FROM task WHERE id=?", (t,))["status"],
                         "closed")
        self.assertEqual(self.db.one(
            "SELECT COUNT(*) c FROM item WHERE holder_task_id=? AND status!='written_off'",
            (t,))["c"], 0)

    def test_damaged_item_writeoff(self):
        t = self.svc.generate_task(2, "YELLOW")
        self._pick_all(t)
        seal = self.db.one(
            "SELECT i.code FROM task_line l JOIN item i ON i.id=l.item_id "
            "WHERE l.task_id=? AND l.line_type='seal'", (t,))["code"]
        self.svc.confirm_slot(t, 1, "conn", 2)
        self.svc.mark_seal_damage(t, 1, 2)
        self.svc.seal_replaced_ok(t, 1, 2)
        self.svc.confirm_slot(t, 1, "seal", 2)
        self.svc.confirm_slot(t, 1, "ctrl", 4)
        self.svc.complete_install(t, 2)
        self.db.execute("UPDATE task SET warning_cleared=1 WHERE id=?", (t,))
        self.svc.remove(t, 2)
        self.svc.confirm_clean(t, 2)
        self.svc.confirm_dry(t, 2)
        rc = self.db.one("SELECT * FROM rack_check WHERE task_id=?", (t,))
        for r in self.db.q("SELECT code FROM item WHERE holder_task_id=? ORDER BY id", (t,)):
            self.svc.rack_scan(rc["id"], r["code"], 2)
        res = self.svc.finish_rack(rc["id"], 2, allow_diff=True)
        self.assertIn(seal, {d["code"] for d in res["damaged"]})
        self.svc.report_loss(t, seal, 1, "密封条撕裂")
        self.assertEqual(self.db.one("SELECT status FROM task WHERE id=?", (t,))["status"],
                         "closed")

    def test_replenish_resolves_shortage(self):
        # 新入口需 6 件 2.0m 挡板，库存只有 4 件 → 缺 2 件
        self.db.execute(
            "INSERT INTO entrance(id,code,name,slot_specs,sort_no) VALUES (3,'N','北门',?,3)",
            (json.dumps([2.0] * 6),))
        t3 = self.svc.generate_task(3, "ORANGE")
        r1 = self.svc.pick_auto(t3, 3)
        self.assertEqual(len([s for s in r1["shortage"] if "挡板" in s]), 2)
        # 补件入库前无法齐套
        with self.assertRaises(DomainError):
            self.svc.finish_picking(t3, 3)
        # 主管补充 2 件同规格挡板
        self.svc.replenish("segment", 2.0, 2, 1, ["RP-S1", "RP-S2"])
        r2 = self.svc.pick_auto(t3, 3)
        self.assertEqual(r2["picked"], ["RP-S1", "RP-S2"])
        self.assertEqual(r2["shortage"], [])
        self.assertTrue(self.svc.finish_picking(t3, 3)["ok"])


if __name__ == "__main__":
    unittest.main()
