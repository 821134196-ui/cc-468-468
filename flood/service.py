"""业务规则：排他领用、分段安装、异常处置、拆除审批、清洗归架盘点。"""
import json

from .db import LEVELS, now_iso


class DomainError(Exception):
    """业务规则冲突，消息可直接展示给现场人员。"""


# 雨量阈值(mm/h) 与预警等级
def level_for(mm: float) -> str:
    if mm >= 50:
        return "RED"
    if mm >= 30:
        return "ORANGE"
    if mm >= 20:
        return "YELLOW"
    if mm >= 8:
        return "BLUE"
    return "NONE"


DEPLOY_AT = "YELLOW"     # 达到黄警生成部署清单
CLEAR_BELOW = "YELLOW"   # 持续复查低于黄警即解除
RECHECK_TICKS = 2        # 连续 2 次低位复查才解除
WATER_WARN_CM = 10.0
WATER_BLOCK_CM = 15.0

ROLE_NAME = {"supervisor": "物业主管", "installer": "安装工",
             "guard": "保安", "observer": "巡查员"}


class Service:
    def __init__(self, db, photo_dir="photos"):
        self.db = db
        self.photo_dir = photo_dir

    # ------------------------------------------------------------------ 工具
    def log(self, action, detail="", actor="系统"):
        self.db.execute(
            "INSERT INTO event_log(ts, actor, action, detail) VALUES (?,?,?,?)",
            (now_iso(), actor, action, detail))

    def _person(self, pid):
        p = self.db.one("SELECT * FROM person WHERE id=?", (pid,))
        if not p:
            raise DomainError("人员不存在")
        return p

    def _require_role(self, pid, roles, action):
        p = self._person(pid)
        if p["role"] not in roles:
            raise DomainError(f"{action}需由{'/'.join(ROLE_NAME[r] for r in roles)}执行，"
                              f"当前为{ROLE_NAME[p['role']]}")
        return p

    def _task(self, tid):
        t = self.db.one("SELECT * FROM task WHERE id=?", (tid,))
        if not t:
            raise DomainError("任务不存在")
        return t

    def _incident(self, type_, t=None, eid=None, detail="", resolution=""):
        self.db.execute(
            "INSERT INTO incident(task_id,entrance_id,type,detail,created_at,resolution) "
            "VALUES (?,?,?,?,?,?)",
            (t["id"] if t else None, eid, type_, detail, now_iso(),
             resolution if resolution else None))

    # ------------------------------------------------------------------ 看板
    def dashboard(self):
        w = self.db.one("SELECT * FROM weather_state WHERE id=1")
        entrances = []
        for e in self.db.q("SELECT * FROM entrance ORDER BY sort_no, id"):
            t = self.db.one(
                "SELECT * FROM task WHERE entrance_id=? AND status!='closed' "
                "ORDER BY id DESC LIMIT 1", (e["id"],))
            water = self.db.one(
                "SELECT level_cm, ts FROM water_log WHERE entrance_id=? "
                "ORDER BY id DESC LIMIT 1", (e["id"],))
            entrances.append({
                "id": e["id"], "code": e["code"], "name": e["name"],
                "task_id": t["id"] if t else None,
                "task_status": t["status"] if t else None,
                "warning_cleared": bool(t and t["warning_cleared"]),
                "water_cm": water["level_cm"] if water else 0.0,
            })
        rains = [dict(r) for r in self.db.q(
            "SELECT tick, mm, level, ts FROM rainfall_log ORDER BY id DESC LIMIT 24")][::-1]
        active_incidents = self.db.q(
            "SELECT i.*, e.name entrance_name FROM incident i "
            "LEFT JOIN entrance e ON e.id=i.entrance_id "
            "WHERE i.resolved_at IS NULL ORDER BY i.id DESC LIMIT 20")
        return {
            "weather": dict(w) if w else {"level": "NONE", "rainfall": 0},
            "entrances": entrances,
            "rainfall": rains,
            "incidents": [dict(r) for r in active_incidents],
        }

    def entrance_detail(self, eid):
        e = self.db.one("SELECT * FROM entrance WHERE id=?", (eid,))
        if not e:
            raise DomainError("入口不存在")
        t = self.db.one(
            "SELECT * FROM task WHERE entrance_id=? AND status!='closed' "
            "ORDER BY id DESC LIMIT 1", (eid,))
        data = {"entrance": dict(e), "slots_spec": json.loads(e["slot_specs"])}
        if t:
            data["task"] = self.task_detail(t["id"])
        return data

    def task_detail(self, tid):
        t = self._task(tid)
        d = dict(t)
        e = self.db.one("SELECT * FROM entrance WHERE id=?", (t["entrance_id"],))
        d["entrance"] = dict(e)
        d["lines"] = [dict(r) for r in self.db.q(
            "SELECT l.*, i.code item_code, i.status item_status, i.rack_code, i.rack_pos "
            "FROM task_line l LEFT JOIN item i ON i.id=l.item_id "
            "WHERE l.task_id=? ORDER BY l.id", (tid,))]
        d["slots"] = [dict(r) for r in self.db.q(
            "SELECT s.*, i.code seg_code FROM task_slot s "
            "LEFT JOIN task_line l ON l.task_id=s.task_id AND l.slot_no=s.slot_no "
            "AND l.line_type='segment' LEFT JOIN item i ON i.id=l.item_id "
            "WHERE s.task_id=? ORDER BY s.slot_no", (tid,))]
        d["assignments"] = [dict(r) for r in self.db.q(
            "SELECT a.*, p.name person_name, p.role FROM task_assignment a "
            "JOIN person p ON p.id=a.person_id WHERE a.task_id=? ORDER BY a.id", (tid,))]
        d["photos"] = [dict(r) for r in self.db.q(
            "SELECT * FROM photo WHERE task_id=? ORDER BY id", (tid,))]
        d["passages"] = [dict(r) for r in self.db.q(
            "SELECT * FROM passage WHERE task_id=? ORDER BY id", (tid,))]
        ap = self.db.one(
            "SELECT * FROM removal_approval WHERE task_id=? AND status='pending' "
            "ORDER BY id DESC LIMIT 1", (tid,))
        d["pending_approval"] = dict(ap) if ap else None
        rc = self.db.one("SELECT * FROM rack_check WHERE task_id=? ORDER BY id DESC LIMIT 1",
                         (tid,))
        d["rack_check"] = dict(rc) if rc else None
        if rc:
            exp_ids = json.loads(rc["expected"])
            if exp_ids:
                codes = [r["code"] for r in self.db.q(
                    "SELECT code FROM item WHERE id IN (%s) ORDER BY id"
                    % ",".join("?" * len(exp_ids)), exp_ids)]
            else:
                codes = []
            d["rack_check"]["expected_codes"] = codes
            d["rack_check"]["scanned_codes"] = json.loads(rc["scanned"])
        waters = self.db.q(
            "SELECT * FROM water_log WHERE entrance_id=? ORDER BY id DESC LIMIT 10",
            (t["entrance_id"],))
        d["waters"] = [dict(r) for r in waters]
        return d

    # ------------------------------------------------------- 清单生成（内部）
    def generate_task(self, entrance_id, level, actor="系统"):
        """预警达阈值后按入口生成部署清单；每个入口同时只允许一个活动任务。"""
        e = self.db.one("SELECT * FROM entrance WHERE id=?", (entrance_id,))
        if not e:
            raise DomainError("入口不存在")
        exists = self.db.one(
            "SELECT id FROM task WHERE entrance_id=? AND status!='closed'", (entrance_id,))
        if exists:
            raise DomainError(f"入口{e['name']}已有进行中的部署任务 #{exists['id']}")
        specs = json.loads(e["slot_specs"])
        cur = self.db.execute(
            "INSERT INTO task(entrance_id,level_triggered,status,created_at) "
            "VALUES (?,?, 'generated',?)",
            (entrance_id, level, now_iso()))
        tid = cur.lastrowid
        labels_post = ["左端柱"] + [f"{i}-{i+1}间柱" for i in range(1, len(specs))] + ["右端柱"]
        for n, w in enumerate(specs, start=1):
            self.db.execute(
                "INSERT INTO task_line(task_id,line_type,slot_no,label,status) "
                "VALUES (?,?,?,?,'pending')",
                (tid, "segment", n, f"{n}号槽挡板 {w:g}m"))
            self.db.execute(
                "INSERT INTO task_line(task_id,line_type,slot_no,label,status) "
                "VALUES (?,?,?,?,'pending')",
                (tid, "seal", n, f"{n}号槽密封条"))
            self.db.execute(
                "INSERT INTO task_slot(task_id,slot_no,expected_width) VALUES (?,?,?)",
                (tid, n, w))
        for n, lab in enumerate(labels_post):
            self.db.execute(
                "INSERT INTO task_line(task_id,line_type,slot_no,label,status) "
                "VALUES (?,?,?,?, 'pending')",
                (tid, "post", n, lab))
        self.log("生成部署清单", f"入口{e['name']} {len(specs)}段挡板/{len(specs)+1}根立柱",
                 actor)
        return tid

    # ------------------------------------------------------------------ 领用
    def _available_item(self, kind, width=None, exclude_task=None):
        if kind == "segment":
            return self.db.one(
                "SELECT * FROM item WHERE kind='segment' AND status='available' "
                "AND ABS(width-?)<0.001 ORDER BY rack_code, rack_pos, id", (width,))
        return self.db.one(
            "SELECT * FROM item WHERE kind=? AND status='available' "
            "ORDER BY rack_code, rack_pos, id", (kind,))

    def pick_line(self, task_id, line_id, code, person_id):
        """扫码领件：锁定器材；同一件器材不能被两个入口任务同时领取。"""
        p = self._person(person_id)
        t = self._task(task_id)
        if t["status"] not in ("generated", "picking", "installing"):
            raise DomainError("当前任务阶段不能领件")
        line = self.db.one("SELECT * FROM task_line WHERE id=? AND task_id=?",
                           (line_id, task_id))
        if not line:
            raise DomainError("清单项不存在")
        if line["status"] not in ("pending",):
            raise DomainError(f"「{line['label']}」已锁定，不能重复领用")
        item = self.db.one("SELECT * FROM item WHERE code=?", (code,))
        if not item:
            raise DomainError(f"器材条码 {code} 不存在")
        # —— 排他事务：读状态与写锁定在同一个 IMMEDIATE 事务内 ——
        db = self.db
        db.begin()
        try:
            row = db.one("SELECT * FROM item WHERE id=?", (item["id"],))
            if row["status"] != "available":
                holder = row["holder_task_id"]
                ht = db.one("SELECT * FROM task WHERE id=?", (holder,)) if holder else None
                he = db.one("SELECT name FROM entrance WHERE id=?",
                            (ht["entrance_id"],)) if ht else None
                db.rollback()
                raise DomainError(
                    f"器材 {code} 已被「{he['name'] if he else '其他'}」任务领取，"
                    f"不能重复发放（同件器材排他）")
            if row["kind"] != line["line_type"]:
                db.rollback()
                raise DomainError(f"器材类型不符：{line['label']} 需要{line['line_type']}")
            if line["line_type"] == "segment":
                slot = db.one("SELECT expected_width FROM task_slot WHERE task_id=? AND slot_no=?",
                              (task_id, line["slot_no"]))
                if abs((row["width"] or 0) - slot["expected_width"]) > 0.001:
                    self._incident("wrong_segment", t=t,
                                   detail=f"{line['label']}扫码成 {code}（宽{row['width']:g}m），错段拒绝")
                    db.rollback()
                    raise DomainError(
                        f"挡板错段：{line['label']} 需要 {slot['expected_width']:g}m，"
                        f"该件为 {row['width']:g}m，已记录并阻止上架")
            db.execute(
                "UPDATE item SET status='locked', holder_task_id=? WHERE id=?",
                (task_id, row["id"]))
            db.execute("UPDATE task_line SET status='locked', item_id=? WHERE id=?",
                       (row["id"], line["id"]))
            if t["status"] == "generated":
                db.execute("UPDATE task SET status='picking', picked_at=? WHERE id=?",
                           (now_iso(), task_id))
            db.commit()
        except DomainError:
            raise
        except Exception:
            db.rollback()
            raise
        self.log("领用锁定", f"{line['label']} → {code}", p["name"])
        return {"ok": True, "line_id": line["id"], "code": code}

    def pick_auto(self, task_id, person_id):
        """整单一次领齐；库存不足的行保留待领并提示缺件。"""
        p = self._person(person_id)
        t = self._task(task_id)
        if t["status"] not in ("generated", "picking"):
            raise DomainError("当前任务阶段不能领件")
        picked, shortage = [], []
        for line in self.db.q(
                "SELECT * FROM task_line WHERE task_id=? AND status='pending' ORDER BY id",
                (task_id,)):
            width = None
            if line["line_type"] == "segment":
                s = self.db.one("SELECT expected_width FROM task_slot WHERE task_id=? AND slot_no=?",
                                (task_id, line["slot_no"]))
                width = s["expected_width"]
            item = self._available_item(line["line_type"], width)
            if not item:
                shortage.append(line["label"])
                continue
            self.pick_line(task_id, line["id"], item["code"], person_id)
            picked.append(item["code"])
        if picked:
            self.log("批量领用", f"{len(picked)}件：{', '.join(picked)}", p["name"])
        if shortage:
            self._incident("shortage", t=t, detail="缺件：" + "、".join(shortage))
        return {"picked": picked, "shortage": shortage}

    def replenish(self, kind, width, qty, person_id, codes=None):
        """缺件补充：新器材入库直接上存放架，立即可用。"""
        p = self._require_role(person_id, ["supervisor", "installer"], "缺件补充")
        codes = codes or []
        created = []
        for i in range(qty):
            code = codes[i] if i < len(codes) else f"NEW-{kind.upper()}-{self.db.one('SELECT COUNT(*) c FROM item')['c']+i+1}"
            if self.db.one("SELECT 1 FROM item WHERE code=?", (code,)):
                raise DomainError(f"条码 {code} 已存在")
            self.db.execute(
                "INSERT INTO item(code,kind,width,rack_code,rack_pos,status) "
                "VALUES (?,?,?, '补件架', '待定位', 'available')",
                (code, kind, width))
            created.append(code)
        cur = self.db.execute(
            "INSERT INTO replenish_order(kind,width,qty,new_codes,status,created_at,received_at) "
            "VALUES (?,?,?,?,'received',?,?)",
            (kind, width, qty, json.dumps(created, ensure_ascii=False), now_iso(), now_iso()))
        self.log("缺件补充入库", f"{kind} x{qty}：{', '.join(created)}", p["name"])
        return {"order_id": cur.lastrowid, "codes": created}

    def finish_picking(self, task_id, person_id):
        p = self._person(person_id)
        t = self._task(task_id)
        if t["status"] not in ("picking", "generated"):
            raise DomainError("当前阶段不在领用中")
        pending = self.db.q("SELECT label FROM task_line WHERE task_id=? AND status='pending'",
                            (task_id,))
        if pending:
            raise DomainError("仍有缺件未领：" + "、".join(r["label"] for r in pending)
                              + "；请补件后再进入安装")
        self.db.execute("UPDATE task SET status='installing' WHERE id=?", (task_id,))
        self.log("领用齐套，进入安装", "", p["name"])
        return {"ok": True}

    # ------------------------------------------------------------------ 安装
    def confirm_slot(self, task_id, slot_no, kind, person_id, photo_path=None):
        """逐段确认：conn 连接(安装工) / seal 密封(安装工) / ctrl 通道封控(保安)。"""
        p = self._person(person_id)
        t = self._task(task_id)
        if t["status"] != "installing":
            raise DomainError("仅安装中任务可逐段确认")
        s = self.db.one("SELECT * FROM task_slot WHERE task_id=? AND slot_no=?",
                        (task_id, slot_no))
        if not s:
            raise DomainError("槽位不存在")
        seg_line = self.db.one(
            "SELECT * FROM task_line WHERE task_id=? AND line_type='segment' AND slot_no=?",
            (task_id, slot_no))
        if not seg_line["item_id"]:
            raise DomainError(f"{slot_no}号槽挡板尚未领用，不能确认")
        if kind == "conn":
            self._require_role(person_id, ["installer"], "连接确认")
            self.db.execute(
                "UPDATE task_slot SET conn_by=?, conn_at=?, conn_photo=COALESCE(?,conn_photo) "
                "WHERE id=?", (person_id, now_iso(), photo_path, s["id"]))
        elif kind == "seal":
            self._require_role(person_id, ["installer"], "密封确认")
            if not s["seal_ok"]:
                raise DomainError(f"{slot_no}号槽密封条破损未更换，不能密封确认")
            seal_line = self.db.one(
                "SELECT * FROM task_line WHERE task_id=? AND line_type='seal' AND slot_no=?",
                (task_id, slot_no))
            if not seal_line["item_id"]:
                raise DomainError(f"{slot_no}号槽密封条未领用")
            self.db.execute(
                "UPDATE task_slot SET seal_by=?, seal_at=?, seal_photo=COALESCE(?,seal_photo) "
                "WHERE id=?", (person_id, now_iso(), photo_path, s["id"]))
        elif kind == "ctrl":
            self._require_role(person_id, ["guard"], "通道封控")
            self.db.execute(
                "UPDATE task_slot SET ctrl_by=?, ctrl_at=?, ctrl_photo=COALESCE(?,ctrl_photo) "
                "WHERE id=?", (person_id, now_iso(), photo_path, s["id"]))
        else:
            raise DomainError("确认类型错误")
        if photo_path:
            self.db.execute(
                "INSERT INTO photo(task_id,slot_no,kind,path,uploaded_by,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (task_id, slot_no, kind, photo_path, person_id, now_iso()))
        names = {"conn": "连接", "seal": "密封", "ctrl": "封控"}
        self.log("分段确认", f"{slot_no}号槽 {names[kind]}", p["name"])
        return {"ok": True}

    def mark_seal_damage(self, task_id, slot_no, person_id, note=""):
        """密封条破损：旧条下架待报损，自动用库存备用条替换并重置密封确认。"""
        p = self._require_role(person_id, ["installer", "supervisor"], "密封条破损登记")
        t = self._task(task_id)
        s = self.db.one("SELECT * FROM task_slot WHERE task_id=? AND slot_no=?",
                        (task_id, slot_no))
        if not s:
            raise DomainError("槽位不存在")
        seal_line = self.db.one(
            "SELECT * FROM task_line WHERE task_id=? AND line_type='seal' AND slot_no=?",
            (task_id, slot_no))
        old = self.db.one("SELECT * FROM item WHERE id=?", (seal_line["item_id"],))
        spare = self._available_item("seal")
        if not spare:
            self._incident("seal_damage", t=t,
                           detail=f"{slot_no}号槽密封条破损，库内无备用条，需紧急补充")
            raise DomainError("密封条破损且库内无备用条，请先申请补充")
        self.db.begin()
        try:
            self.db.execute("UPDATE item SET status='damaged' WHERE id=?", (old["id"],))
            self.db.execute("UPDATE item SET status='locked',holder_task_id=? WHERE id=?",
                            (task_id, spare["id"]))
            self.db.execute("UPDATE task_line SET item_id=? WHERE id=?",
                            (spare["id"], seal_line["id"]))
            self.db.execute(
                "UPDATE task_slot SET seal_ok=0, seal_by=NULL, seal_at=NULL, seal_photo=NULL "
                "WHERE id=?", (s["id"],))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self._incident("seal_damage", t=t,
                       detail=f"{slot_no}号槽 {old['code']} 破损，已换备用 {spare['code']}。{note}",
                       resolution="已更换备用密封条，待重新密封确认")
        self.db.execute("UPDATE incident SET resolved_at=? WHERE id="
                        "(SELECT id FROM incident WHERE type='seal_damage' AND task_id=? "
                        "ORDER BY id DESC LIMIT 1)", (now_iso(), task_id))
        self.log("密封条破损更换", f"{old['code']} → {spare['code']}", p["name"])
        return {"old": old["code"], "spare": spare["code"]}

    def seal_replaced_ok(self, task_id, slot_no, person_id):
        """更换完成，恢复该槽可密封确认状态。"""
        self._require_role(person_id, ["installer"], "密封条更换验收")
        self.db.execute("UPDATE task_slot SET seal_ok=1 WHERE task_id=? AND slot_no=?",
                        (task_id, slot_no))
        self.log("密封条更换验收", f"{slot_no}号槽", self._person(person_id)["name"])
        return {"ok": True}

    def complete_install(self, task_id, person_id):
        """安装完成：所有槽段连接+密封+封控齐备，且无开启中的临时通道。
        并发只允许成功一次。"""
        p = self._person(person_id)
        db = self.db
        db.begin()
        try:
            t = db.one("SELECT * FROM task WHERE id=?", (task_id,))
            if not t:
                db.rollback()
                raise DomainError("任务不存在")
            if t["status"] == "installed":
                db.rollback()
                raise DomainError("该任务已完成安装并封控，并发完成只允许一次")
            if t["status"] != "installing":
                db.rollback()
                raise DomainError(f"当前状态 {t['status']}，不能完成安装")
            missing = []
            for s in db.q("SELECT * FROM task_slot WHERE task_id=? ORDER BY slot_no", (task_id,)):
                if not s["conn_at"]:
                    missing.append(f"{s['slot_no']}号槽未确认连接")
                if not s["seal_at"]:
                    missing.append(f"{s['slot_no']}号槽未确认密封")
                if not s["ctrl_at"]:
                    missing.append(f"{s['slot_no']}号槽未确认通道封控")
                if s["opened"]:
                    missing.append(f"{s['slot_no']}号槽临时放行通道尚未复位")
                if not s["seal_ok"]:
                    missing.append(f"{s['slot_no']}号槽密封条破损未处理")
            pend = db.q("SELECT label FROM task_line WHERE task_id=? AND status!='locked'",
                        (task_id,))
            for r in pend:
                missing.append(f"未领齐：{r['label']}")
            if missing:
                db.rollback()
                raise DomainError("分段安装不完整：" + "；".join(missing))
            db.execute(
                "UPDATE item SET status='installed' WHERE holder_task_id=? AND status='locked'",
                (task_id,))
            db.execute("UPDATE task SET status='installed', installed_at=? WHERE id=?",
                       (now_iso(), task_id))
            db.execute("UPDATE person SET status='onsite' WHERE id IN "
                       "(SELECT person_id FROM task_assignment WHERE task_id=?)", (task_id,))
            db.commit()
        except DomainError:
            raise
        except Exception:
            db.rollback()
            raise
        self.log("安装完成封控", f"任务 #{task_id}", p["name"])
        return {"ok": True}

    # -------------------------------------------------------------- 临时放行
    def open_passage(self, task_id, slot_no, person_id, plate):
        """临时车辆放行：保安开启指定槽位通道，原连接/密封/封控确认全部失效。"""
        p = self._require_role(person_id, ["guard"], "临时车辆放行")
        t = self._task(task_id)
        if t["status"] != "installed":
            raise DomainError("仅已封控的挡板可以临时开启放行")
        s = self.db.one("SELECT * FROM task_slot WHERE task_id=? AND slot_no=?",
                        (task_id, slot_no))
        if not s:
            raise DomainError("槽位不存在")
        cur = self.db.execute(
            "INSERT INTO passage(task_id,slot_no,plate,guard_id,opened_at) VALUES (?,?,?,?,?)",
            (task_id, slot_no, plate, person_id, now_iso()))
        self.db.execute(
            "UPDATE task_slot SET opened=1, conn_at=NULL,conn_by=NULL,conn_photo=NULL,"
            "seal_at=NULL,seal_by=NULL,seal_photo=NULL,ctrl_at=NULL,ctrl_by=NULL,ctrl_photo=NULL "
            "WHERE id=?", (s["id"],))
        self.db.execute("UPDATE task SET status='installing' WHERE id=?", (task_id,))
        self._incident("passage_open", t=t,
                       detail=f"{slot_no}号槽临时放行车辆 {plate or '未登记车牌'}，"
                              f"复位后须重新逐段确认")
        self.log("临时放行开启", f"{slot_no}号槽 {plate}", p["name"])
        return {"passage_id": cur.lastrowid}

    def restore_passage(self, passage_id, person_id):
        p = self._require_role(person_id, ["guard"], "通道复位封控")
        pg = self.db.one("SELECT * FROM passage WHERE id=?", (passage_id,))
        if not pg or pg["restored_at"]:
            raise DomainError("放行记录不存在或已复位")
        self.db.execute("UPDATE passage SET restored_at=? WHERE id=?", (now_iso(), passage_id))
        self.db.execute("UPDATE task_slot SET opened=0 WHERE task_id=? AND slot_no=?",
                        (pg["task_id"], pg["slot_no"]))
        self.db.execute(
            "UPDATE incident SET resolved_at=?, resolution='通道已复位，待重新确认' "
            "WHERE type='passage_open' AND task_id=? AND resolved_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (now_iso(), pg["task_id"]))
        self.log("临时放行复位", f"{pg['slot_no']}号槽，需重新连接/密封/封控确认", p["name"])
        return {"ok": True, "need_reconfirm": ["conn", "seal", "ctrl"]}

    # ------------------------------------------------------------------ 人员
    def signin(self, assignment_id, person_id):
        a = self.db.one("SELECT * FROM task_assignment WHERE id=?", (assignment_id,))
        if not a:
            raise DomainError("派工记录不存在")
        if a["person_id"] != person_id:
            raise DomainError("只能本人签到")
        p = self._person(person_id)
        if a["arrived_at"]:
            raise DomainError("已签到，请勿重复")
        late = bool(a["due_at"] and now_iso() > a["due_at"])
        self.db.execute(
            "UPDATE task_assignment SET arrived_at=?, late=? WHERE id=?",
            (now_iso(), 1 if late else 0, assignment_id))
        self.db.execute("UPDATE person SET status=? WHERE id=?",
                        ("late" if late else "onsite", person_id))
        if late:
            t = self._task(a["task_id"])
            self._incident("late", t=t, detail=f"{p['name']} 迟到（应到 {a['due_at']}）")
        self.log("现场签到", f"{p['name']}{'（迟到）' if late else ''}", p["name"])
        return {"late": late}

    # ------------------------------------------------------------------ 积水
    def add_water(self, entrance_id, cm, person_id, source="人工", note=""):
        p = self._person(person_id) if person_id else None
        e = self.db.one("SELECT * FROM entrance WHERE id=?", (entrance_id,))
        if not e:
            raise DomainError("入口不存在")
        cm = float(cm)
        prev = self.db.one(
            "SELECT level_cm FROM water_log WHERE entrance_id=? ORDER BY id DESC LIMIT 1",
            (entrance_id,))
        self.db.execute(
            "INSERT INTO water_log(entrance_id,ts,level_cm,source,note,observer) "
            "VALUES (?,?,?,?,?,?)",
            (entrance_id, now_iso(), cm, source, note, p["name"] if p else source))
        if cm >= WATER_WARN_CM and (not prev or prev["level_cm"] < WATER_WARN_CM):
            self._incident("water", eid=entrance_id,
                           detail=f"入口积水达 {cm:g}cm，警戒阈值 {WATER_WARN_CM:g}cm")
        actor = p["name"] if p else source
        self.log("积水观察", f"{e['name']} {cm:g}cm {note}", actor)
        return {"blocked": cm >= WATER_BLOCK_CM, "cm": cm}

    # ------------------------------------------------------------ 拆除与审批
    def latest_water(self, entrance_id):
        r = self.db.one(
            "SELECT level_cm FROM water_log WHERE entrance_id=? ORDER BY id DESC LIMIT 1",
            (entrance_id,))
        return r["level_cm"] if r else 0.0

    def request_removal(self, task_id, person_id, reason):
        """预警未解除时申请拆除，进入主管审批；已解除可直接拆除。"""
        p = self._person(person_id)
        t = self._task(task_id)
        if t["status"] != "installed":
            raise DomainError("仅已封控任务可以申请拆除")
        if t["warning_cleared"]:
            return {"need_approval": False}
        exist = self.db.one(
            "SELECT id FROM removal_approval WHERE task_id=? AND status='pending'", (task_id,))
        if exist:
            raise DomainError("已有待审批的提前拆除申请")
        cur = self.db.execute(
            "INSERT INTO removal_approval(task_id,requested_by,reason,status,created_at) "
            "VALUES (?,?,?,'pending',?)",
            (task_id, person_id, reason, now_iso()))
        self._incident("early_removal", t=t,
                       detail=f"{p['name']}申请预警未解除提前拆除：{reason}（待主管审批）")
        self.log("申请提前拆除", reason, p["name"])
        return {"need_approval": True, "approval_id": cur.lastrowid}

    def decide_removal(self, approval_id, supervisor_id, approve, note=""):
        """物业主管决定提前拆除（批准/驳回）。"""
        s = self._require_role(supervisor_id, ["supervisor"], "提前拆除审批")
        ap = self.db.one("SELECT * FROM removal_approval WHERE id=?", (approval_id,))
        if not ap or ap["status"] != "pending":
            raise DomainError("审批单不存在或已处理")
        self.db.execute(
            "UPDATE removal_approval SET status=?, decided_by=?, decided_at=?, note=? WHERE id=?",
            ("approved" if approve else "rejected", supervisor_id, now_iso(), note,
             approval_id))
        self.db.execute(
            "UPDATE incident SET resolved_at=?, resolution=? WHERE type='early_removal' "
            "AND task_id=? AND resolved_at IS NULL ORDER BY id DESC LIMIT 1",
            (now_iso(), f"主管{'批准' if approve else '驳回'}：{note}", ap["task_id"]))
        self.log(f"主管{'批准' if approve else '驳回'}提前拆除", note or "—", s["name"])
        return {"ok": True, "status": "approved" if approve else "rejected"}

    def remove(self, task_id, person_id, photo_path=None):
        """拆除：必须已解除预警或持有主管批准；入口积水超限时一律禁止。"""
        p = self._person(person_id)
        t = self._task(task_id)
        if t["status"] != "installed":
            raise DomainError("仅已封控任务可以拆除")
        water = self.latest_water(t["entrance_id"])
        if water >= WATER_BLOCK_CM:
            self._incident("water_block", t=t,
                           detail=f"申请拆除时入口积水 {water:g}cm ≥ {WATER_BLOCK_CM:g}cm，禁止拆除")
            raise DomainError(f"入口积水 {water:g}cm，未退至 {WATER_BLOCK_CM:g}cm 以下禁止拆除")
        approved = False
        if not t["warning_cleared"]:
            ap = self.db.one(
                "SELECT * FROM removal_approval WHERE task_id=? AND status='approved' "
                "ORDER BY id DESC LIMIT 1", (task_id,))
            if not ap:
                raise DomainError("预警尚未解除：请先申请并由物业主管批准提前拆除")
            approved = True
        self.db.execute(
            "UPDATE item SET status='removed' WHERE holder_task_id=? AND status='installed'",
            (task_id,))
        self.db.execute(
            "UPDATE task SET status='cleaning', removed_at=?, closure_by=?, closure_at=?, "
            "closure_photo=COALESCE(?,closure_photo) WHERE id=?",
            (now_iso(), person_id, now_iso(), photo_path, task_id))
        if photo_path:
            self.db.execute(
                "INSERT INTO photo(task_id,kind,path,uploaded_by,created_at) VALUES (?,?,?,?,?)",
                (task_id, "closure", photo_path, person_id, now_iso()))
        if approved:
            self._incident("early_removal", t=t, resolution="主管批准后提前拆除",
                           detail="预警未解除，凭主管批准拆除")
        self.log("挡板拆除", "凭批准提前拆除" if approved else "预警解除后拆除", p["name"])
        return {"ok": True, "early": approved}

    # ------------------------------------------------------------ 清洗/晾干
    def confirm_clean(self, task_id, person_id, photo_path=None):
        p = self._require_role(person_id, ["installer", "supervisor"], "清洗确认")
        t = self._task(task_id)
        if t["status"] != "cleaning":
            raise DomainError("仅清洗阶段可确认清洗")
        self.db.execute("UPDATE task SET status='drying', cleaned_at=? WHERE id=?",
                        (now_iso(), task_id))
        if photo_path:
            self.db.execute(
                "INSERT INTO photo(task_id,kind,path,uploaded_by,created_at) VALUES (?,?,?,?,?)",
                (task_id, "clean", photo_path, person_id, now_iso()))
        self.log("清洗完成，进入晾干", "", p["name"])
        return {"ok": True}

    def confirm_dry(self, task_id, person_id, photo_path=None):
        p = self._require_role(person_id, ["installer", "supervisor"], "晾干确认")
        t = self._task(task_id)
        if t["status"] != "drying":
            raise DomainError("仅晾干阶段可确认晾干")
        self.db.execute("UPDATE task SET status='racking', dried_at=? WHERE id=?",
                        (now_iso(), task_id))
        if photo_path:
            self.db.execute(
                "INSERT INTO photo(task_id,kind,path,uploaded_by,created_at) VALUES (?,?,?,?,?)",
                (task_id, "dry", photo_path, person_id, now_iso()))
        self.start_rack(task_id, person_id)
        self.log("晾干完成，开始归架核对", "", p["name"])
        return {"ok": True}

    # ------------------------------------------------------------------ 归架
    def start_rack(self, task_id, actor_id=None):
        t = self._task(task_id)
        if self.db.one("SELECT id FROM rack_check WHERE task_id=? AND finished_at IS NULL",
                       (task_id,)):
            raise DomainError("已有进行中的归架盘点单")
        items = self.db.q(
            "SELECT id FROM item WHERE holder_task_id=? AND status IN ('removed','damaged') "
            "ORDER BY id", (task_id,))
        ids = [r["id"] for r in items]
        cur = self.db.execute(
            "INSERT INTO rack_check(task_id,expected,scanned,open_diff,created_at) "
            "VALUES (?,?, '[]', 1,?)",
            (task_id, json.dumps(ids), now_iso()))
        if actor_id:
            self.log("建立归架盘点单", f"应归 {len(ids)} 件", self._person(actor_id)["name"])
        return {"check_id": cur.lastrowid, "expected": len(ids)}

    def rack_scan(self, check_id, code, person_id):
        p = self._person(person_id)
        rc = self.db.one("SELECT * FROM rack_check WHERE id=?", (check_id,))
        if not rc or rc["finished_at"]:
            raise DomainError("盘点单不存在或已结束")
        item = self.db.one("SELECT * FROM item WHERE code=?", (code,))
        if not item:
            raise DomainError(f"条码 {code} 不存在")
        if item["holder_task_id"] != rc["task_id"]:
            raise DomainError(f"{code} 不属于本任务领用器材")
        expected = json.loads(rc["expected"])
        scanned = json.loads(rc["scanned"])
        if code in scanned:
            raise DomainError(f"{code} 已扫码，不要重复扫描")
        if item["id"] not in expected:
            raise DomainError(f"{code} 不在本单应归清单内")
        scanned.append(code)
        self.db.execute("UPDATE rack_check SET scanned=? WHERE id=?",
                        (json.dumps(scanned, ensure_ascii=False), check_id))
        self.log("归架扫码", code, p["name"])
        return {"scanned": len(scanned), "expected": len(expected),
                "damaged": item["status"] == "damaged"}

    def finish_rack(self, check_id, person_id, allow_diff=False):
        """核对完成：有差异时须显式承认，缺件转主管报损补充；破损件须报损。"""
        p = self._person(person_id)
        rc = self.db.one("SELECT * FROM rack_check WHERE id=?", (check_id,))
        if not rc or rc["finished_at"]:
            raise DomainError("盘点单不存在或已结束")
        t = self._task(rc["task_id"])
        expected = json.loads(rc["expected"])
        scanned = json.loads(rc["scanned"])
        scanned_items = self.db.q(
            "SELECT * FROM item WHERE code IN (%s)" % ",".join("?" * len(scanned)),
            scanned) if scanned else []
        scanned_ids = {r["id"] for r in scanned_items}
        missing = self.db.q(
            "SELECT id,code,kind FROM item WHERE id IN (%s)" % ",".join("?" * len(expected)),
            expected) if expected else []
        missing = [dict(r) for r in missing if r["id"] not in scanned_ids]
        damaged = [dict(r) for r in scanned_items if r["status"] == "damaged"]
        if (missing or damaged) and not allow_diff:
            raise DomainError("归架差异：缺件 "
                              + ("、".join(r["code"] for r in missing) or "无")
                              + "；破损待报损 "
                              + ("、".join(r["code"] for r in damaged) or "无")
                              + "。确认差异后由主管处理")
        # 正常件归架重新可用
        for r in scanned_items:
            if r["status"] != "damaged":
                self.db.execute(
                    "UPDATE item SET status='available', holder_task_id=NULL, "
                    "installed_slot=NULL WHERE id=?", (r["id"],))
                self.db.execute(
                    "UPDATE task_line SET status='returned' WHERE task_id=? AND item_id=?",
                    (rc["task_id"], r["id"]))
        for m in missing:
            self._incident("rack_missing", t=t,
                           detail=f"归架缺件 {m['code']}（{m['kind']}），待物业主管报损补充")
        for d in damaged:
            self._incident("rack_damaged", t=t,
                           detail=f"归架发现破损 {d['code']}（{d['kind']}），待物业主管报损")
        self.db.execute("UPDATE rack_check SET finished_at=?, open_diff=? WHERE id=?",
                        (now_iso(), 0 if not (missing or damaged) else 1, check_id))
        self.log("归架核对", f"缺件{len(missing)} 破损{len(damaged)}", p["name"])
        self._maybe_close(rc["task_id"])
        return {"missing": missing, "damaged": damaged}

    def report_loss(self, task_id, item_code, supervisor_id, note="", new_code=None):
        """物业主管缺件/破损报损，并以同规格补件顶替归架，器材重新可用。"""
        s = self._require_role(supervisor_id, ["supervisor"], "缺件报损")
        t = self._task(task_id)
        old = self.db.one("SELECT * FROM item WHERE code=?", (item_code,))
        if not old or old["holder_task_id"] != task_id:
            raise DomainError("器材不存在或不属于本任务")
        if old["status"] not in ("removed", "damaged"):
            raise DomainError(f"器材当前状态 {old['status']}，不能报损")
        new_code = new_code or f"REP-{old['kind'].upper()}-{old['id']}"
        if self.db.one("SELECT 1 FROM item WHERE code=?", (new_code,)):
            raise DomainError(f"补件条码 {new_code} 已存在")
        self.db.begin()
        try:
            self.db.execute(
                "INSERT INTO item(code,kind,width,rack_code,rack_pos,status,replaced_from) "
                "VALUES (?,?,?, '补件架','替换就位','available',?)",
                (new_code, old["kind"], old["width"], old["id"]))
            nid = self.db.one("SELECT id FROM item WHERE code=?", (new_code,))["id"]
            self.db.execute(
                "UPDATE item SET status='written_off', holder_task_id=NULL WHERE id=?",
                (old["id"],))
            self.db.execute(
                "UPDATE task_line SET item_id=?, status='returned' "
                "WHERE task_id=? AND item_id=?", (nid, task_id, old["id"]))
            self.db.execute(
                "INSERT INTO loss_report(task_id,item_id,supervisor_id,note,created_at) "
                "VALUES (?,?,?,?,?)", (task_id, old["id"], supervisor_id, note, now_iso()))
            self.db.execute(
                "INSERT INTO replenish_order(task_id,kind,width,qty,new_codes,status,"
                "created_at,received_at) VALUES (?,?,?,1,?,'received',?,?)",
                (task_id, old["kind"], old["width"],
                 json.dumps([new_code], ensure_ascii=False), now_iso(), now_iso()))
            self.db.execute(
                "UPDATE incident SET resolved_at=?, resolution=? WHERE id IN "
                "(SELECT id FROM incident WHERE task_id=? AND resolved_at IS NULL "
                "AND type IN ('rack_missing','rack_damaged') "
                "AND detail LIKE ?)",
                (now_iso(), f"主管报损并补件 {new_code}", task_id, f"%{old['code']}%"))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.log("缺件报损补充", f"{old['code']} 报损 → 补件 {new_code}", s["name"])
        self._maybe_close(task_id)
        return {"new_code": new_code}

    def pending_losses(self, task_id):
        return [dict(r) for r in self.db.q(
            "SELECT i.* FROM item i WHERE i.holder_task_id=? "
            "AND i.status IN ('removed','damaged')", (task_id,))]

    def _maybe_close(self, task_id):
        t = self.db.one("SELECT * FROM task WHERE id=?", (task_id,))
        if not t or t["status"] != "racking":
            return
        rc = self.db.one("SELECT * FROM rack_check WHERE task_id=? ORDER BY id DESC LIMIT 1",
                         (task_id,))
        if not rc or not rc["finished_at"]:
            return
        bad = self.db.one(
            "SELECT COUNT(*) c FROM item WHERE holder_task_id=? AND status IN ('removed','damaged')",
            (task_id,))["c"]
        unreturned = self.db.one(
            "SELECT COUNT(*) c FROM task_line WHERE task_id=? AND status NOT IN "
            "('locked','returned')", (task_id,))["c"]
        # locked 的行在归架正常路径已转 returned；若全部器材处理完即可结案
        still_held = self.db.one(
            "SELECT COUNT(*) c FROM item WHERE holder_task_id=? AND status!='written_off'",
            (task_id,))["c"]
        if bad == 0 and still_held == 0 and unreturned == 0:
            self.db.execute(
                "UPDATE task SET status='closed', closed_at=? WHERE id=?",
                (now_iso(), task_id))
            self.log("任务结案", "器材全部归架/补件，重新可用", "系统")

    # ------------------------------------------------------------------ 事件
    def events(self, limit=50):
        return [dict(r) for r in self.db.q(
            "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,))]

    def incidents(self, include_closed=False):
        sql = ("SELECT i.*, e.name entrance_name FROM incident i "
               "LEFT JOIN entrance e ON e.id=i.entrance_id ")
        if not include_closed:
            sql += "WHERE i.resolved_at IS NULL "
        sql += "ORDER BY i.id DESC LIMIT 50"
        return [dict(r) for r in self.db.q(sql)]
