"""进程内调度：定时拉取模拟雨量/水位，推动预警升级、清单生成与解除复查。"""
import threading
import time

from .db import now_iso
from .service import Service, level_for, DEPLOY_AT, CLEAR_BELOW, RECHECK_TICKS


class Scheduler(threading.Thread):
    def __init__(self, db, adapter, interval=5.0, water_log=True):
        super().__init__(daemon=True, name="weather-scheduler")
        self.db = db
        self.adapter = adapter
        self.interval = interval
        self.water_log = water_log
        self.svc = Service(db)
        self._stop = threading.Event()
        self._prev_water = {}

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick_once()
            except Exception as ex:  # 调度异常不影响现场操作
                self.svc.log("调度异常", str(ex))

    # ------------------------------------------------------------------ 单拍
    def tick_once(self, persist_water=None):
        db = self.db
        mm = self.adapter.rainfall()
        level = level_for(mm)
        w = db.one("SELECT * FROM weather_state WHERE id=1")
        prev_level = w["level"] if w else "NONE"
        prev_tick = (w["tick"] if w else self.adapter.tick)
        rank = {"NONE": 0, "BLUE": 1, "YELLOW": 2, "ORANGE": 3, "RED": 4}
        below = rank[level] < rank[DEPLOY_AT]
        low_ticks = (w["low_ticks"] + 1) if (w and below) else (1 if below else 0)
        if not w:
            db.execute(
                "INSERT INTO weather_state(id,level,rainfall,low_ticks,tick,updated_at) "
                "VALUES (1,?,?,?,?,?)", (level, mm, low_ticks, prev_tick, now_iso()))
        else:
            db.execute(
                "UPDATE weather_state SET level=?,rainfall=?,low_ticks=?,tick=?,updated_at=? "
                "WHERE id=1", (level, mm, low_ticks, prev_tick, now_iso()))
        db.execute(
            "INSERT INTO rainfall_log(ts,tick,mm,level) VALUES (?,?,?,?)",
            (now_iso(), prev_tick, mm, level))

        # 预警升级
        order = ["NONE", "BLUE", "YELLOW", "ORANGE", "RED"]
        if order.index(level) > order.index(prev_level):
            self.svc.log("预警升级", f"{prev_level} → {level}（雨量 {mm:g}mm/h）")

        # 达阈值：每个入口确保有部署清单
        if rank[level] >= rank[DEPLOY_AT]:
            for e in db.q("SELECT * FROM entrance ORDER BY sort_no,id"):
                active = db.one(
                    "SELECT id FROM task WHERE entrance_id=? AND status!='closed'", (e["id"],))
                if not active:
                    try:
                        self.svc.generate_task(e["id"], level, actor="预警调度")
                    except Exception:
                        pass

        # 监测设备水位
        do_water = self.water_log if persist_water is None else persist_water
        if do_water and mm > 0:
            ents = [dict(r) for r in db.q("SELECT id,code FROM entrance")]
            levels = self.adapter.water_levels(ents, self._prev_water)
            for eid, cm in levels.items():
                last = self._prev_water.get(eid, 0.0)
                self._prev_water[eid] = cm
                if abs(cm - last) >= 0.2 or cm >= 10:
                    db.execute(
                        "INSERT INTO water_log(entrance_id,ts,level_cm,source,note,observer) "
                        "VALUES (?,?,?,'监测设备','','监测设备')",
                        (eid, now_iso(), cm))

        # 连续复查低于阈值 → 解除预警（low_ticks 高位会清零，累计到阈值即说明此前在预警中）
        if below and low_ticks >= RECHECK_TICKS:
            n = db.execute(
                "UPDATE task SET warning_cleared=1 WHERE warning_cleared=0 AND status!='closed'"
            ).rowcount
            if n:
                self.svc.log("预警解除", f"连续{low_ticks}次复查低于黄色阈值，{n}个进行中任务预警解除")
        self.adapter.advance()
        return {"mm": mm, "level": level, "low_ticks": low_ticks}
