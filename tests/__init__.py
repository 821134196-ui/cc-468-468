"""测试公共夹具：最小化器材/入口/人员数据。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.db import get_conn, init_db
from app.services import Service


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test.db")
        init_db(self.db)
        self.svc = Service(self.db)
        self.conn = get_conn(self.db)
        self._fixture()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _fixture(self) -> None:
        c = self.conn
        c.executemany("INSERT INTO racks(id,name,zone) VALUES(?,?,?)",
                      [(1, "挡板架", "A"), (2, "立柱条架", "B")])
        c.executemany(
            "INSERT INTO entrances(id,name,code,width_slots,post_count,ramp_desc,layout_order) "
            "VALUES(?,?,?,?,?,?,?)",
            [(1, "东门", "E", 4, 5, "坡道A", 1),
             (2, "西门", "W", 3, 4, "坡道B", 2),
             (3, "北门", "N", 2, 3, "坡道C", 3)])
        rows = ([(f"DB-{i:02d}", "board", 100, 1) for i in range(1, 13)]
                + [(f"POST-{i:02d}", "post", 120, 2) for i in range(1, 15)]
                + [(f"SEAL-{i:02d}", "seal", 100, 2) for i in range(1, 13)])
        c.executemany(
            "INSERT INTO equipment(code,kind,length_cm,rack_id) VALUES(?,?,?,?)", rows)
        c.executemany(
            "INSERT INTO staff(id,name,role,phone) VALUES(?,?,?,?)",
            [(1, "保安甲", "guard", "110"), (2, "防汛员甲", "worker", "120"),
             (3, "防汛员乙", "worker", "121"), (6, "周主管", "supervisor", "119")])
        c.commit()

    # ---- 流程辅助 ------
    def _round(self, rain=30.0, water=16.0):
        rid = self.svc.create_round(rain, water, "测试雨")
        return rid

    def _planned(self, rid, ent=1):
        return self.svc.generate_checklist(rid, ent)

    def _installed(self, rid=1, ent=1, worker=2, seal_damage_slot=None):
        """生成一个安装完成并封控的任务，返回 task_id。"""
        from app.db import get_conn
        tid = self.svc.generate_checklist(rid, ent)
        self.svc.claim(tid, worker)
        self.svc.begin_install(tid, worker)
        ent_row = self.conn.execute("SELECT width_slots FROM entrances WHERE id=?", (ent,)).fetchone()
        for slot in range(ent_row["width_slots"]):
            self.svc.confirm_connection(tid, slot, worker)
            self.svc.confirm_seal(tid, slot, worker)
        self.svc.confirm_closure(tid, 1)  # 保安封控
        return tid
