"""HTTP 服务：JSON API + 静态前端，照片保存到本地目录。

本地启动：
    python -m flood.app --demo            # 载入部署/缺件/提前解除样例并启动
    python -m flood.app                   # 空白库启动
    python -m flood.app --demo --tick 4   # 自定义调度节拍秒数
"""
import argparse
import json
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .db import DB
from .service import Service, DomainError
from .weather import MockWeatherAdapter
from .scheduler import Scheduler
from . import seed

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class App:
    def __init__(self, db_path="data/flood.db", photo_dir="data/photos",
                 demo=False, interval=5.0, start_scheduler=True):
        self.db = DB(db_path)
        self.photo_dir = photo_dir
        os.makedirs(photo_dir, exist_ok=True)
        self.svc = Service(self.db, photo_dir)
        self.adapter = MockWeatherAdapter()
        self.demo = demo
        if demo:
            seed.load_demo(self.db, self.adapter)
        self.scheduler = None
        if start_scheduler:
            self.scheduler = Scheduler(self.db, self.adapter, interval=interval)
            self.scheduler.start()

    def save_photo(self, filename, data: bytes):
        ext = os.path.splitext(filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
            ext = ".jpg"
        name = f"{uuid.uuid4().hex}{ext}"
        path = os.path.join(self.photo_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return name


# ---------------------------------------------------------------- multipart 解析
def parse_multipart(body: bytes, boundary: bytes):
    """极简 multipart/form-data 解析 → [(name, filename, content_type, data)]。"""
    parts = []
    delim = b"--" + boundary
    for chunk in body.split(delim):
        if not chunk or chunk in (b"--", b"--\r\n", b"\r\n"):
            continue
        chunk = chunk.strip(b"\r\n")
        header_end = chunk.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers = chunk[:header_end].decode("utf-8", "replace")
        data = chunk[header_end + 4:]
        name = None
        filename = None
        ctype = "application/octet-stream"
        m = re.search(r'name="([^"]*)"', headers)
        if m:
            name = m.group(1)
        mf = re.search(r'filename="([^"]*)"', headers)
        if mf:
            filename = mf.group(1)
        mc = re.search(r"Content-Type:\s*(\S+)", headers, re.I)
        if mc:
            ctype = mc.group(1)
        parts.append((name, filename, ctype, data))
    return parts


class Handler(BaseHTTPRequestHandler):
    app: App = None

    def log_message(self, fmt, *args):
        pass  # 静默；业务事件已落 event_log

    # -------------------------------------------------------------- 响应工具
    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    def _file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -------------------------------------------------------------- 入口路由
    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                return self._file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
            if p.startswith("/static/"):
                name = p[len("/static/"):]
                ctype = {"app.js": "application/javascript; charset=utf-8",
                         "styles.css": "text/css; charset=utf-8"}.get(
                    os.path.basename(name), "application/octet-stream")
                return self._file(os.path.join(STATIC_DIR, name), ctype)
            if p.startswith("/photos/"):
                return self._file(os.path.join(self.app.photo_dir, os.path.basename(p)),
                                  "image/jpeg")
            if p == "/api/dashboard":
                return self._json(self.app.svc.dashboard())
            if p == "/api/entrance":
                return self._json(self.app.svc.entrance_detail(int(q["id"][0])))
            if p == "/api/task":
                return self._json(self.app.svc.task_detail(int(q["id"][0])))
            if p == "/api/items":
                rows = self.app.db.q(
                    "SELECT * FROM item ORDER BY kind, rack_code, rack_pos, id")
                return self._json([dict(r) for r in rows])
            if p == "/api/people":
                return self._json([dict(r) for r in self.app.db.q(
                    "SELECT * FROM person ORDER BY role,id")])
            if p == "/api/events":
                return self._json(self.app.svc.events())
            if p == "/api/incidents":
                return self._json(self.app.svc.incidents(
                    q.get("all", ["0"])[0] == "1"))
            if p == "/api/losses":
                return self._json(self.app.svc.pending_losses(int(q["task_id"][0])))
            self.send_error(404)
        except DomainError as e:
            self._err(str(e))
        except Exception as e:
            self._err(f"服务器错误：{e}", 500)

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(n) if n else b""

    def _params(self):
        """支持 application/json 与 multipart（含照片）。"""
        ctype = self.headers.get("Content-Type", "")
        body = self._read_body()
        if ctype.startswith("multipart/form-data"):
            b = ctype.split("boundary=", 1)[1].encode()
            params, files = {}, {}
            for name, filename, _ct, data in parse_multipart(body, b):
                if name is None:
                    continue
                if filename:
                    files[name] = (filename, data)
                else:
                    params[name] = data.decode("utf-8", "replace").strip()
            return params, files
        params = json.loads(body or b"{}")
        return params, {}

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        try:
            params, files = self._params()

            def gi(k, default=None):
                v = params.get(k, default)
                return int(v) if v not in (None, "") else default

            svc = self.app.svc
            photo_name = None
            if "photo" in files and files["photo"][1]:
                photo_name = self.app.save_photo(files["photo"][0], files["photo"][1])

            if p == "/api/pick":
                return self._json(svc.pick_line(gi("task_id"), gi("line_id"),
                                                params["code"], gi("person_id")))
            if p == "/api/pick-auto":
                return self._json(svc.pick_auto(gi("task_id"), gi("person_id")))
            if p == "/api/finish-picking":
                return self._json(svc.finish_picking(gi("task_id"), gi("person_id")))
            if p == "/api/replenish":
                codes = params.get("codes")
                if isinstance(codes, str):
                    codes = json.loads(codes)
                return self._json(svc.replenish(params["kind"],
                                                float(params.get("width") or 0) or None,
                                                gi("qty", 1), gi("person_id"), codes))
            if p == "/api/confirm":
                return self._json(svc.confirm_slot(gi("task_id"), gi("slot"),
                                                   params["kind"], gi("person_id"),
                                                   photo_name))
            if p == "/api/seal-damage":
                return self._json(svc.mark_seal_damage(gi("task_id"), gi("slot"),
                                                       gi("person_id"),
                                                       params.get("note", "")))
            if p == "/api/seal-replaced":
                return self._json(svc.seal_replaced_ok(gi("task_id"), gi("slot"),
                                                       gi("person_id")))
            if p == "/api/complete":
                return self._json(svc.complete_install(gi("task_id"), gi("person_id")))
            if p == "/api/passage-open":
                return self._json(svc.open_passage(gi("task_id"), gi("slot"),
                                                   gi("person_id"),
                                                   params.get("plate", "")))
            if p == "/api/passage-restore":
                return self._json(svc.restore_passage(gi("passage_id"), gi("person_id")))
            if p == "/api/signin":
                return self._json(svc.signin(gi("assignment_id"), gi("person_id")))
            if p == "/api/water":
                return self._json(svc.add_water(gi("entrance_id"),
                                                float(params["cm"]), gi("person_id"),
                                                params.get("source", "人工"),
                                                params.get("note", "")))
            if p == "/api/removal-request":
                return self._json(svc.request_removal(gi("task_id"), gi("person_id"),
                                                      params.get("reason", "")))
            if p == "/api/removal-decide":
                return self._json(svc.decide_removal(
                    gi("approval_id"), gi("person_id"),
                    params["approve"] in (True, "true", "1", "yes"),
                    params.get("note", "")))
            if p == "/api/remove":
                return self._json(svc.remove(gi("task_id"), gi("person_id"), photo_name))
            if p == "/api/clean":
                return self._json(svc.confirm_clean(gi("task_id"), gi("person_id"),
                                                    photo_name))
            if p == "/api/dry":
                return self._json(svc.confirm_dry(gi("task_id"), gi("person_id"),
                                                  photo_name))
            if p == "/api/rack-start":
                return self._json(svc.start_rack(gi("task_id"), gi("person_id")))
            if p == "/api/rack-scan":
                return self._json(svc.rack_scan(gi("check_id"), params["code"],
                                                gi("person_id")))
            if p == "/api/rack-finish":
                return self._json(svc.finish_rack(
                    gi("check_id"), gi("person_id"),
                    params.get("allow_diff") in (True, "true", "1", "yes")))
            if p == "/api/loss-report":
                return self._json(svc.report_loss(
                    gi("task_id"), params["item_code"], gi("person_id"),
                    params.get("note", ""), params.get("new_code") or None))
            if p == "/api/losses":
                return self._json(svc.pending_losses(gi("task_id")))
            if p == "/api/weather/set":
                mm = params.get("mm")
                self.app.adapter.set_manual(None if mm in (None, "") else float(mm))
                if self.app.scheduler:
                    return self._json(self.app.scheduler.tick_once())
                return self._json({"ok": True})
            if p == "/api/weather/tick":
                self.app.adapter.set_manual(None)
                r = self.app.scheduler.tick_once() if self.app.scheduler else {}
                return self._json(r)
            if p == "/api/weather/auto":
                self.app.adapter.set_manual(None)
                return self._json({"ok": True})
            self.send_error(404)
        except DomainError as e:
            self._err(str(e))
        except (KeyError, ValueError) as e:
            self._err(f"参数错误：{e}")
        except Exception as e:
            self._err(f"服务器错误：{e}", 500)


def main():
    ap = argparse.ArgumentParser(description="社区地下车库防汛挡板部署与归位系统")
    ap.add_argument("--demo", action="store_true", help="载入部署/缺件/提前解除样例")
    ap.add_argument("--db", default="data/flood.db")
    ap.add_argument("--photos", default="data/photos")
    ap.add_argument("--tick", type=float, default=5.0, help="天气调度节拍秒数")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()

    app = App(db_path=args.db, photo_dir=args.photos, demo=args.demo,
              interval=args.tick)
    Handler.app = app
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    mode = "演示样例" if args.demo else "空白数据"
    print(f"防汛挡板系统已启动（{mode}）  http://{args.host}:{args.port}")
    print("调度节拍：每 %.0f 秒一次雨量/水位复查" % args.tick)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app.scheduler:
            app.scheduler.stop()


if __name__ == "__main__":
    main()
