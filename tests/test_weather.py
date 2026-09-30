"""预警状态：阈值升级、只升不降、复查解除、积水警戒与临时放行重封。"""
from __future__ import annotations

from app.services import DomainError, level_for
from app.weather import WeatherAdapter, WeatherMonitor
from tests import ServiceCase


class TestLevelThreshold(ServiceCase):
    def test_level_for_thresholds(self):
        self.assertEqual(level_for(0, 0), None)
        self.assertEqual(level_for(12, 0), "blue")
        self.assertEqual(level_for(25, 0), "yellow")
        self.assertEqual(level_for(0, 15), "yellow")
        self.assertEqual(level_for(50, 0), "orange")
        self.assertEqual(level_for(80, 35), "red")
        self.assertEqual(level_for(79, 34.9), "orange")

    def test_level_escalation_only_up(self):
        rid = self.svc.create_round(12, 4, "小雨")
        self.assertEqual(self.svc.update_round_reading(55, 26)["level"], "orange")
        # 读数回落不自动降级（等级粘性，解除须复查）
        r = self.svc.update_round_reading(8, 2)
        self.assertFalse(r["changed"])
        row = self.conn.execute("SELECT level FROM rounds WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["level"], "orange")

    def test_cannot_lift_twice(self):
        rid = self._round()
        self.svc.lift_round(rid, "normal")
        with self.assertRaises(DomainError):
            self.svc.lift_round(rid, "normal")

    def test_checklist_requires_active_round(self):
        rid = self._round()
        self.svc.lift_round(rid, "normal")
        with self.assertRaises(DomainError):
            self.svc.generate_checklist(rid, 1)


class TestMonitorRecheck(ServiceCase):
    def test_monitor_escalates_and_lifts_after_rechecks(self):
        rid = self.svc.create_round(0, 0, "监测中")
        adapter = WeatherAdapter("storm")
        mon = WeatherMonitor(self.svc, adapter, recheck_need=3, auto_checklists=False)
        # 45 秒：黄色阈值 → 升级
        adapter.elapse(45)
        r1 = mon.poll()
        self.assertTrue(r1["changed"])
        self.assertEqual(r1["level"], "yellow")
        # 200 秒：红色
        adapter.elapse(200 - 45)
        r2 = mon.poll()
        self.assertEqual(r2["level"], "red")
        # 480 秒：雨停水退，连续 3 次安全复查解除
        adapter.elapse(480 - 200)
        mon.poll()
        mon.poll()
        r4 = mon.poll()
        self.assertTrue(r4["lifted"])
        row = self.conn.execute("SELECT status FROM rounds WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["status"], "lifted")

    def test_monitor_auto_generates_checklists(self):
        self.svc.create_round(0, 0, "监测中")
        adapter = WeatherAdapter("storm")
        mon = WeatherMonitor(self.svc, adapter, recheck_need=3, auto_checklists=True)
        adapter.elapse(45)
        mon.poll()
        count = self.conn.execute("SELECT COUNT(*) c FROM tasks").fetchone()["c"]
        self.assertEqual(count, 3)  # 三个入口各一份清单


class TestObservationAndPassage(ServiceCase):
    def test_water_observation_alert_threshold(self):
        rid = self._round()
        tid = self._installed(rid, 3)
        r1 = self.svc.add_observation(tid, 8, 1, "正常")
        self.assertFalse(r1["alert"])
        r2 = self.svc.add_observation(tid, 12, 1, "上涨")
        self.assertTrue(r2["alert"])

    def test_passage_must_reseal(self):
        rid = self._round()
        tid = self._installed(rid, 3)
        pid = self.svc.open_passage(tid, "沪A12345", 1, "120 急救")
        # 守看中可放行；重复重封被拒
        self.svc.reseal_passage(pid, 1)
        with self.assertRaises(DomainError):
            self.svc.reseal_passage(pid, 1)
        # 非守看状态不能放行：拆除后拒绝
        self.svc.lift_round(rid, "normal")
        self.svc.dismantle(tid, 2)
        with self.assertRaises(DomainError):
            self.svc.open_passage(tid, "沪B99999", 1)

    def test_staff_late_does_not_block_task(self):
        rid = self._round()
        tid = self._planned(rid, 3)
        self.svc.claim(tid, 2)
        self.svc.record_staff_late(tid, 3, 20, 6)  # 防汛员乙迟到
        self.svc.begin_install(tid, 2)              # 在岗防汛员甲继续
        self.svc.confirm_connection(tid, 0, 2)
