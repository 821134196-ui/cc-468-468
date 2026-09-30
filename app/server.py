"""HTTP 服务：标准库实现的 JSON API + 静态页面 + 照片上传。"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .db import init_db
from .photos import PHOTO_DIR, make_sample_png
from .queries import LEVEL_LABEL, TASK_LABEL, Queries
from .seed import seed
from .services import DomainError, Service
from .weather import SCENARIOS, WeatherAdapter, WeatherMonitor

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


class App:
    def __init__(self, db_path: str, monitor_interval: float = 15.0,
                 auto_monitor: bool = True, scenario: str = "yellow"):
        init_db(db_path)
        self.svc = Service(db_path)
        self.q = Queries(db_path)
        self.adapter = WeatherAdapter(scenario)
        self.monitor = WeatherMonitor(self.svc, self.adapter, interval=monitor_interval)
        self._io_lock = threading.Lock()
        if auto_monitor:
            self.monitor.start()

    # ---------- 聚合视图 ----------
    def dashboard(self) -> dict:
        rnd = self.q.active_round()
        return {
            "round": rnd,
            "round_label": LEVEL_LABEL.get(rnd["level"]) if rnd else "无预警",
            "overview": self.q.board_overview(),
            "trend": self.adapter.trend(),
            "scenario": self.adapter.scenario,
            "scenarios": list(SCENARIOS.keys()),
            "orders": self.q.replacement_orders(active_only=True),
            "events": self.q.events(limit=18),
            "staff": self.q.staff_list(),
            "task_labels": TASK_LABEL,
            "level_labels": LEVEL_LABEL,
        }

    def task_full(self, task_id: int) -> dict:
        detail = self.q.task_detail(task_id)
        if not detail:
            raise DomainError("任务不存在")
        detail["status_label"] = TASK_LABEL.get(detail["status"], detail["status"])
        detail["round_label"] = LEVEL_LABEL.get(detail["round_level"], detail["round_level"])
        return {
            "detail": detail,
            "boards": self.q.task_boards(task_id),
            "posts": self.q.task_posts(task_id),
            "closure": bool(self.q.conn.execute(
                "SELECT 1 FROM task_closures WHERE task_id=? AND closed=1",
                (task_id,)).fetchone()),
            "passages": self.q.passages(task_id),
            "observations": self.q.observations(task_id),
            "stocktake": self.q.stocktake_items(task_id),
            "dismantle": self.q.dismantle(task_id),
            "approvals": self.q.approvals(task_id),
            "events": self.q.events(task_id, limit=25),
        }


class Handler(BaseHTTPRequestHandler):
    app: App  # 由 make_server 注入到类属性

    def log_message(self, fmt, *args):  # 安静日志
        pass

    # ---------- 工具 ----------
    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            raise DomainError("请求体不是合法 JSON")

    def _static(self, rel: str) -> None:
        rel = rel or "index.html"
        if rel == "photos":
            rel = "photos/index.html"
        path = (WEB_DIR / rel).resolve()
        if not str(path).startswith(str(WEB_DIR.resolve())) or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _photo(self, name: str) -> None:
        path = (PHOTO_DIR / name).resolve()
        if not str(path).startswith(str(PHOTO_DIR.resolve())) or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Cache-Control", "max-age=60")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/dashboard":
                return self._send_json(self.app.dashboard())
            if path == "/api/entrances":
                return self._send_json(self.app.q.entrances())
            if path == "/api/staff":
                return self._send_json(self.app.q.staff_list())
            if path == "/api/equipment":
                return self._send_json(self.app.q.equipment_list())
            if path == "/api/tasks":
                qs = urlparse(self.path).query
                include_all = "all=1" in qs
                return self._send_json(self.app.q.task_list(include_all=include_all))
            if path == "/api/orders":
                return self._send_json(self.app.q.replacement_orders())
            m = re.fullmatch(r"/api/tasks/(\d+)", path)
            if m:
                return self._send_json(self.app.task_full(int(m.group(1))))
            if path.startswith("/photos/"):
                return self._photo(path[len("/photos/"):])
            if path.startswith("/api/"):
                self.send_error(404)
                return
            return self._static(path.lstrip("/"))
        except DomainError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            self._send_json({"error": f"服务器错误: {e}"}, 500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        data = {}
        try:
            with self.app._io_lock:
                # 照片上传：原始字节流
                if path == "/api/photos":
                    return self._upload_photo()
                data = self._read_json()
                result = self._dispatch(path, data)
            self._send_json({"ok": True, **(result or {})})
        except DomainError as e:
            self._send_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"服务器错误: {e}"}, 500)

    def _upload_photo(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 10 * 1024 * 1024:
            raise DomainError("照片为空或超过 10MB")
        raw = self.rfile.read(length)
        name = self.headers.get("X-Photo-Name") or "photo.png"
        name = re.sub(r"[^\w.\-]", "_", name)
        fname = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{name}"
        if not fname.lower().endswith((".png", ".jpg", ".jpeg")):
            fname += ".png"
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        (PHOTO_DIR / fname).write_bytes(raw)
        self._send_json({"ok": True, "path": fname})

    def _dispatch(self, path: str, d: dict) -> dict | None:
        s = self.app.svc
        tid = lambda m: int(m.group(1))

        if path == "/api/rounds":
            rid = s.create_round(float(d.get("rain_rate", 0)), float(d.get("water_level", 0)),
                                 d.get("note", ""))
            return {"round_id": rid}
        if path == "/api/rounds/lift":
            s.lift_round(int(d["round_id"]), d.get("kind", "normal"),
                         d.get("approver_id"), d.get("reason", ""))
            return None
        if path == "/api/checklists":
            tidv = s.generate_checklist(int(d["round_id"]), int(d["entrance_id"]))
            return {"task_id": tidv}
        if path == "/api/weather/poll":
            return self.app.monitor.poll()
        if path == "/api/weather/scenario":
            name = d.get("scenario", "storm")
            if name not in SCENARIOS:
                raise DomainError("未知天气脚本")
            self.app.adapter.reset(name)
            self.app.monitor._safe_streak = 0
            if d.get("advance"):
                self.app.adapter.elapse(float(d["advance"]))
            return {"scenario": name}

        m = re.fullmatch(r"/api/tasks/(\d+)/claim", path)
        if m:
            return s.claim(tid(m), int(d["staff_id"]))
        m = re.fullmatch(r"/api/tasks/(\d+)/begin", path)
        if m:
            s.begin_install(tid(m), int(d["staff_id"])); return None
        m = re.fullmatch(r"/api/tasks/(\d+)/closure", path)
        if m:
            s.confirm_closure(tid(m), int(d["staff_id"]), d.get("photo", "")); return None
        m = re.fullmatch(r"/api/tasks/(\d+)/passages", path)
        if m:
            pid = s.open_passage(tid(m), d.get("plate", ""), int(d["staff_id"]),
                                 d.get("reason", ""))
            return {"passage_id": pid}
        m = re.fullmatch(r"/api/tasks/(\d+)/observations", path)
        if m:
            return s.add_observation(tid(m), float(d["water_level_cm"]),
                                     int(d["staff_id"]), d.get("note", ""), d.get("photo", ""))
        m = re.fullmatch(r"/api/tasks/(\d+)/late", path)
        if m:
            s.record_staff_late(tid(m), int(d["staff_id"]), int(d.get("minutes", 0)),
                                int(d["reporter_id"])); return None
        m = re.fullmatch(r"/api/tasks/(\d+)/early-approve", path)
        if m:
            aid = s.approve_early_dismantle(tid(m), int(d["supervisor_id"]), d.get("reason", ""))
            return {"approval_id": aid}
        m = re.fullmatch(r"/api/tasks/(\d+)/dismantle", path)
        if m:
            s.dismantle(tid(m), int(d["staff_id"])); return None
        m = re.fullmatch(r"/api/tasks/(\d+)/cleaned", path)
        if m:
            s.mark_cleaned(tid(m), int(d["staff_id"]), d.get("note", "")); return None
        m = re.fullmatch(r"/api/tasks/(\d+)/dried", path)
        if m:
            s.mark_dried(tid(m), int(d["staff_id"])); return None
        m = re.fullmatch(r"/api/tasks/(\d+)/finalize", path)
        if m:
            return s.finalize_stocktake(tid(m), int(d["staff_id"]))

        m = re.fullmatch(r"/api/segments/(\d+)/(\d+)/(connection|seal)", path)
        if m:
            task_id, slot = int(m.group(1)), int(m.group(2))
            photo, note, staff = d.get("photo", ""), d.get("note", ""), int(d["staff_id"])
            if m.group(3) == "connection":
                s.confirm_connection(task_id, slot, staff, photo, note)
            else:
                s.confirm_seal(task_id, slot, staff, photo, note)
            return None
        m = re.fullmatch(r"/api/segments/(\d+)/(\d+)/fix-segment", path)
        if m:
            s.fix_wrong_segment(int(m.group(1)), int(m.group(2)),
                                d["board_code"], int(d["staff_id"])); return None
        m = re.fullmatch(r"/api/segments/(\d+)/(\d+)/seal-damage", path)
        if m:
            msg = s.report_seal_damage(int(m.group(1)), int(m.group(2)),
                                       int(d["staff_id"]), d.get("note", "密封条破损"))
            return {"message": msg}

        m = re.fullmatch(r"/api/passages/(\d+)/reseal", path)
        if m:
            s.reseal_passage(int(m.group(1)), int(d["staff_id"]), d.get("photo", "")); return None

        m = re.fullmatch(r"/api/stock/(\d+)/check", path)
        if m:
            s.check_stock_item(int(m.group(1)), int(d["staff_id"]), d["state"],
                               d.get("rack_id"), d.get("note", "")); return None
        m = re.fullmatch(r"/api/stock/(\d+)/loss", path)
        if m:
            oid = s.report_loss(int(m.group(1)), int(d["supervisor_id"]), d.get("reason", ""))
            return {"order_id": oid}
        m = re.fullmatch(r"/api/orders/(\d+)/receive", path)
        if m:
            nid = s.receive_replacement(int(m.group(1)), d["new_code"],
                                        int(d["supervisor_id"]))
            return {"new_equipment_id": nid}

        raise DomainError(f"未知接口: {path}")


def make_server(host: str, port: int, db_path: str, reseed: bool = False,
                scenario: str = "yellow", monitor_interval: float = 15.0) -> ThreadingHTTPServer:
    if reseed:
        seed(db_path)
    app = App(db_path, monitor_interval=monitor_interval, scenario=scenario)
    Handler.app = app
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.app = app
    return httpd
