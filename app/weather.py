"""模拟天气适配器与进程内监控：返回雨量/水位，驱动预警升级与回落复查。"""
from __future__ import annotations

import threading
import time
from collections import deque

from .services import LEVEL_RANK, Service, level_for, now

# 演示脚本：(相对秒, 雨强 mm/h, 水位 cm, 说明)
SCENARIOS = {
    # 完整过程：蓝 → 黄 → 橙 → 红 → 回落解除
    "storm": [
        (0, 8, 3, "云层加厚，零星降雨"),
        (45, 30, 16, "雨势加大，坡道开始积水"),
        (120, 55, 26, "强降雨，排水口高负荷"),
        (200, 85, 38, "暴雨红色风险，全面封控"),
        (300, 40, 20, "雨势减弱"),
        (390, 12, 6, "小雨，积水退去"),
        (480, 0, 0, "雨停，复查水位归零"),
    ],
    # 快速达到黄色后维持，用于部署演示
    "yellow": [
        (0, 12, 4, "阵雨"),
        (30, 32, 18, "黄色预警：准备挡板"),
        (300, 28, 15, "持续黄色"),
        (420, 5, 2, "回落"),
    ],
    # 已回落到安全区，用于解除/拆除演示
    "recede": [
        (0, 5, 2, "天气转好"),
        (60, 0, 0, "连续复查无雨无积水"),
    ],
}


class WeatherAdapter:
    """按时间轴返回模拟雨量与水位；轮询历史保留最近 60 条。"""

    def __init__(self, scenario: str = "storm"):
        self._scenario = scenario
        self._t0 = time.time()
        self.history: deque[dict] = deque(maxlen=60)
        self._lock = threading.Lock()

    def reset(self, scenario: str | None = None) -> None:
        with self._lock:
            if scenario:
                self._scenario = scenario
            self._t0 = time.time()
            self.history.clear()

    def elapse(self, seconds: float) -> None:
        """演示用：把时间轴向前拨快。"""
        with self._lock:
            self._t0 -= seconds

    @property
    def scenario(self) -> str:
        return self._scenario

    def read(self, elapsed: float | None = None) -> dict:
        if elapsed is None:
            elapsed = time.time() - self._t0
        script = SCENARIOS[self._scenario]
        rain, water, note = script[-1][1], script[-1][2], script[-1][3]
        for t, r, w, n in script:
            if elapsed >= t:
                rain, water, note = r, w, n
            else:
                break
        return {"rain_rate": float(rain), "water_level": float(water),
                "note": note, "elapsed": round(elapsed)}

    def record(self, reading: dict, level: str) -> dict:
        point = {"at": now(), **reading, "level": level}
        with self._lock:
            self.history.append(point)
        return point

    def trend(self) -> list[dict]:
        with self._lock:
            return list(self.history)


class WeatherMonitor:
    """进程内后台任务：定时取数 → 升级预警/生成清单 → 连续复查安全后解除。"""

    def __init__(self, svc: Service, adapter: WeatherAdapter,
                 interval: float = 15.0, recheck_need: int = 3,
                 auto_checklists: bool = True):
        self.svc = svc
        self.adapter = adapter
        self.interval = interval
        self.recheck_need = recheck_need
        self.auto_checklists = auto_checklists
        self._safe_streak = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="weather-monitor")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll()
            except Exception as exc:  # 监控线程不应因单次异常退出
                print(f"[monitor] poll failed: {exc}")

    def poll(self) -> dict:
        """执行一次复查。可由后台线程或 API 手动触发。"""
        reading = self.adapter.read()
        result = self.svc.update_round_reading(
            reading["rain_rate"], reading["water_level"], reading["note"])
        self.adapter.record(reading, result["level"])

        if result["changed"] and LEVEL_RANK[result["level"]] >= LEVEL_RANK["yellow"]:
            self._safe_streak = 0
            if self.auto_checklists:
                self._ensure_checklists(result["round_id"])

        # 安全区复查：雨强与水位都低于蓝色阈值
        safe = reading["rain_rate"] < 10 and reading["water_level"] < 5
        if safe:
            self._safe_streak += 1
        else:
            self._safe_streak = 0

        lifted = False
        if self._safe_streak >= self.recheck_need:
            rnd = self.svc._row("SELECT * FROM rounds WHERE status='active' ORDER BY id DESC LIMIT 1")
            if rnd:
                self.svc.lift_round(rnd["id"], kind="normal",
                                    reason=f"连续{self._safe_streak}次复查雨量水位低于阈值")
                lifted = True
            self._safe_streak = 0

        return {**result, "reading": reading, "lifted": lifted,
                "safe_streak": self._safe_streak}

    def _ensure_checklists(self, round_id: int) -> None:
        entrances = self.svc._all("SELECT id FROM entrances ORDER BY layout_order, id")
        for ent in entrances:
            self.svc.generate_checklist(round_id, ent["id"])
