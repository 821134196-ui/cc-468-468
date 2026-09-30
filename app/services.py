"""核心领域服务：领用排他、分段安装、封控放行、积水、拆除审批、清洗归架与缺件报损。"""
from __future__ import annotations

import threading
from datetime import datetime

from .db import get_conn

# 预警阈值：(等级, 雨强 mm/h, 水位 cm)
LEVEL_THRESHOLDS = [
    ("red", 80, 35),
    ("orange", 50, 25),
    ("yellow", 25, 15),
    ("blue", 10, 5),
]
LEVEL_RANK = {"blue": 1, "yellow": 2, "orange": 3, "red": 4}
# 积水观察警戒线（cm），超过则提示上报并请求升级
OBSERVATION_ALERT_CM = 10
# 归架盘点允许的差异状态
DIFF_STATES = {"present", "missing", "damaged"}


class DomainError(Exception):
    """业务规则冲突。"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def level_for(rain_rate: float, water_level: float) -> str | None:
    for level, rain, water in LEVEL_THRESHOLDS:
        if rain_rate >= rain or water_level >= water:
            return level
    return None


class Service:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()

    # ---------- 基础 ----------
    @property
    def conn(self):
        return get_conn(self.db_path)

    def _event(self, kind: str, detail: str, task_id: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(kind, detail, task_id, at) VALUES(?,?,?,?)",
            (kind, detail, task_id, now()),
        )

    def _row(self, sql: str, args=()):
        return self.conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args=()):
        return self.conn.execute(sql, args).fetchall()

    def _staff(self, staff_id: int) -> dict:
        row = self._row("SELECT * FROM staff WHERE id=?", (staff_id,))
        if not row:
            raise DomainError("现场人员不存在")
        return dict(row)

    def _require_role(self, staff_id: int, roles: set[str]) -> dict:
        person = self._staff(staff_id)
        if person["role"] not in roles:
            names = {"guard": "保安", "worker": "防汛员", "supervisor": "物业主管"}
            raise DomainError(f"该操作需要{'/'.join(names[r] for r in roles)}权限")
        return person

    def _task(self, task_id: int) -> dict:
        row = self._row("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not row:
            raise DomainError("部署任务不存在")
        return dict(row)

    # ---------- 预警轮次 ----------
    def create_round(self, rain_rate: float = 0, water_level: float = 0,
                     note: str = "") -> int:
        with self._lock:
            active = self._row("SELECT id FROM rounds WHERE status='active'")
            if active:
                raise DomainError("已存在进行中的预警轮次")
            level = level_for(rain_rate, water_level) or "blue"
            cur = self.conn.execute(
                "INSERT INTO rounds(level, status, rain_rate, water_level, weather_note) "
                "VALUES('blue','active',?,?,?)",
                (rain_rate, water_level, note),
            )
            rid = cur.lastrowid
            self._event("round_start", f"启动蓝色预警轮次 #{rid}（{note or '天气监测'}）")
            self.conn.commit()
            return rid

    def update_round_reading(self, rain_rate: float, water_level: float,
                             note: str = "") -> dict:
        """写入最新读数；只升不降，升级返回 changed=True。"""
        with self._lock:
            row = self._row("SELECT * FROM rounds WHERE status='active' ORDER BY id DESC LIMIT 1")
            if not row:
                raise DomainError("没有进行中的预警轮次")
            target = level_for(rain_rate, water_level) or row["level"]
            # 等级粘性：读数回落不降级，始终保留较高等级
            new_level = target if LEVEL_RANK[target] > LEVEL_RANK[row["level"]] else row["level"]
            changed = new_level != row["level"]
            self.conn.execute(
                "UPDATE rounds SET level=?, rain_rate=?, water_level=?, weather_note=? WHERE id=?",
                (new_level, rain_rate, water_level, note, row["id"]),
            )
            if changed:
                names = {"yellow": "黄色", "orange": "橙色", "red": "红色"}
                self._event("level_up",
                            f"预警升级为{names.get(new_level, new_level)}预警（雨强 {rain_rate}mm/h，水位 {water_level}cm）")
            self.conn.commit()
            return {"round_id": row["id"], "level": new_level, "changed": changed}

    def lift_round(self, round_id: int, kind: str = "normal",
                   approver_id: int | None = None, reason: str = "") -> None:
        with self._lock:
            row = self._row("SELECT * FROM rounds WHERE id=?", (round_id,))
            if not row:
                raise DomainError("预警轮次不存在")
            if row["status"] == "lifted":
                raise DomainError("预警已解除，请勿重复操作")
            if kind == "early":
                person = self._require_role(approver_id, {"supervisor"})
                if not reason:
                    raise DomainError("提前解除必须填写原因")
                detail = f"物业主管 {person['name']} 提前解除预警：{reason}"
            else:
                detail = "天气回落，复查通过，预警正常解除"
            self.conn.execute(
                "UPDATE rounds SET status='lifted', lifted_at=?, lift_kind=?, "
                "lift_approver_id=?, lift_reason=? WHERE id=?",
                (now(), kind, approver_id, reason, round_id),
            )
            self._event("round_lift", detail)
            self.conn.commit()

    # ---------- 部署清单与领件 ----------
    def generate_checklist(self, round_id: int, entrance_id: int) -> int:
        """预警达到阈值后按入口生成部署清单（空槽位，领件时才锁定器材）。"""
        with self._lock:
            rnd = self._row("SELECT * FROM rounds WHERE id=?", (round_id,))
            if not rnd or rnd["status"] != "active":
                raise DomainError("只能针对进行中的预警生成清单")
            ent = self._row("SELECT * FROM entrances WHERE id=?", (entrance_id,))
            if not ent:
                raise DomainError("入口不存在")
            exists = self._row(
                "SELECT id FROM tasks WHERE round_id=? AND entrance_id=?",
                (round_id, entrance_id),
            )
            if exists:
                return exists["id"]
            cur = self.conn.execute(
                "INSERT INTO tasks(round_id, entrance_id, status) VALUES(?,?,'planned')",
                (round_id, entrance_id),
            )
            tid = cur.lastrowid
            for slot in range(ent["width_slots"]):
                self.conn.execute(
                    "INSERT INTO task_boards(task_id, slot) VALUES(?,?)", (tid, slot))
            for slot in range(ent["post_count"]):
                self.conn.execute(
                    "INSERT INTO task_posts(task_id, slot) VALUES(?,?)", (tid, slot))
            self._event("checklist",
                        f"{ent['name']} 生成部署清单：{ent['width_slots']} 段挡板、"
                        f"{ent['post_count']} 根立柱、密封条 {ent['width_slots']} 条", tid)
            self.conn.commit()
            return tid

    def claim(self, task_id: int, staff_id: int) -> dict:
        """领件：排他锁定挡板/立柱/密封条。同一器材不能被两个入口同时领取。"""
        person = self._require_role(staff_id, {"worker", "supervisor", "guard"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] != "planned":
                raise DomainError("任务已领件或已开始，不能重复领件")
            ent = self._row("SELECT * FROM entrances WHERE id=?", (task["entrance_id"],))
            n_boards = ent["width_slots"]
            n_posts = ent["post_count"]
            # BEGIN IMMEDIATE 配合进程锁，保证跨线程/跨入口的排他分配
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                boards = self.conn.execute(
                    "SELECT * FROM equipment WHERE kind='board' AND status='available' "
                    "ORDER BY id LIMIT ?", (n_boards,)).fetchall()
                posts = self.conn.execute(
                    "SELECT * FROM equipment WHERE kind='post' AND status='available' "
                    "ORDER BY id LIMIT ?", (n_posts,)).fetchall()
                seals = self.conn.execute(
                    "SELECT * FROM equipment WHERE kind='seal' AND status='available' "
                    "ORDER BY id LIMIT ?", (n_boards,)).fetchall()
                if len(boards) < n_boards or len(posts) < n_posts or len(seals) < n_boards:
                    self.conn.execute("ROLLBACK")
                    raise DomainError(
                        f"可用器材不足：需挡板{n_boards}/立柱{n_posts}/密封条{n_boards}，"
                        f"现有挡板{len(boards)}/立柱{len(posts)}/密封条{len(seals)}")

                slots = self.conn.execute(
                    "SELECT id, slot FROM task_boards WHERE task_id=? ORDER BY slot",
                    (task_id,)).fetchall()
                for srow, brow, srow2 in zip(slots, boards, seals):
                    self.conn.execute(
                        "UPDATE task_boards SET board_id=?, seal_id=? WHERE id=?",
                        (brow["id"], srow2["id"], srow["id"]))
                pslots = self.conn.execute(
                    "SELECT id FROM task_posts WHERE task_id=? ORDER BY slot",
                    (task_id,)).fetchall()
                for prow, post in zip(pslots, posts):
                    self.conn.execute(
                        "UPDATE task_posts SET post_id=? WHERE id=?",
                        (post["id"], prow["id"]))
                picked = ([b["id"] for b in boards] + [p["id"] for p in posts]
                          + [s["id"] for s in seals])
                self.conn.execute(
                    f"UPDATE equipment SET status='locked', active_task_id=?, updated_at=? "
                    f"WHERE id IN ({','.join('?' * len(picked))})",
                    [task_id, now()] + picked,
                )
                self.conn.execute(
                    "UPDATE tasks SET status='claimed', claimed_by=?, claimed_at=? WHERE id=?",
                    (staff_id, now(), task_id),
                )
                self._event("claim",
                            f"{person['name']} 领件并锁定 {len(picked)} 件器材", task_id)
                self.conn.commit()
            except Exception:
                # 任何失败都回滚，绝不留下半锁定状态
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise
            return {"task_id": task_id, "locked": len(picked)}

    def begin_install(self, task_id: int, staff_id: int) -> None:
        self._require_role(staff_id, {"worker", "supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] != "claimed":
                raise DomainError("只有已领件未安装的任务才能开工")
            self.conn.execute("UPDATE tasks SET status='installing' WHERE id=?", (task_id,))
            self._event("install_start", "开始分段安装", task_id)
            self.conn.commit()

    # ---------- 分段安装 ----------
    def _board_slot(self, task_id: int, slot: int) -> dict:
        row = self._row("SELECT * FROM task_boards WHERE task_id=? AND slot=?",
                        (task_id, slot))
        if not row:
            raise DomainError("分段槽位不存在")
        return dict(row)

    def confirm_connection(self, task_id: int, slot: int, staff_id: int,
                           photo_path: str = "", note: str = "") -> None:
        """逐段确认连接。挡板错段（槽位上的板与实际不符）需先纠正。"""
        person = self._require_role(staff_id, {"worker", "supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] not in ("claimed", "installing"):
                raise DomainError("当前状态不能确认分段连接")
            s = self._board_slot(task_id, slot)
            if not s["board_id"]:
                raise DomainError(f"第 {slot + 1} 段缺挡板，无法确认连接")
            self.conn.execute(
                "UPDATE task_boards SET conn_confirmed=1, photo_path=COALESCE(NULLIF(?, ''), photo_path), "
                "note=CASE WHEN ?='' THEN note ELSE ? END WHERE id=?",
                (photo_path, note, note, s["id"]))
            self._event("conn_ok", f"{person['name']} 确认第 {slot + 1} 段连接牢固", task_id)
            self.conn.commit()

    def confirm_seal(self, task_id: int, slot: int, staff_id: int,
                     photo_path: str = "", note: str = "") -> None:
        person = self._require_role(staff_id, {"worker", "supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] not in ("claimed", "installing"):
                raise DomainError("当前状态不能确认密封")
            s = self._board_slot(task_id, slot)
            if not s["seal_id"]:
                raise DomainError(f"第 {slot + 1} 段缺密封条，无法确认密封")
            seal = self._row("SELECT status FROM equipment WHERE id=?", (s["seal_id"],))
            if seal["status"] in ("repair", "lost"):
                raise DomainError(f"第 {slot + 1} 段密封条破损，需更换后再确认")
            self.conn.execute(
                "UPDATE task_boards SET seal_confirmed=1, photo_path=COALESCE(NULLIF(?, ''), photo_path) "
                "WHERE id=?",
                (photo_path, s["id"]))
            self._event("seal_ok", f"{person['name']} 确认第 {slot + 1} 段密封完好", task_id)
            self.conn.commit()

    def fix_wrong_segment(self, task_id: int, slot: int, correct_board_code: str,
                          staff_id: int) -> None:
        """处理挡板错段：用正确的备用板替换该槽位挡板，原板退回可用池。"""
        person = self._require_role(staff_id, {"worker", "supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] not in ("claimed", "installing"):
                raise DomainError("当前状态不能调整挡板")
            s = self._board_slot(task_id, slot)
            new_board = self._row("SELECT * FROM equipment WHERE code=?",
                                  (correct_board_code,))
            if not new_board:
                raise DomainError(f"器材编号 {correct_board_code} 不存在")
            if new_board["kind"] != "board":
                raise DomainError(f"{correct_board_code} 不是挡板")
            if new_board["status"] != "available":
                raise DomainError(f"{correct_board_code} 当前状态为{new_board['status']}，不可替换")
            old_id = s["board_id"]
            self.conn.execute(
                "UPDATE equipment SET status='available', active_task_id=NULL, updated_at=? WHERE id=?",
                (now(), old_id))
            self.conn.execute(
                "UPDATE equipment SET status='locked', active_task_id=?, updated_at=? WHERE id=?",
                (task_id, now(), new_board["id"]))
            self.conn.execute(
                "UPDATE task_boards SET board_id=?, conn_confirmed=0, seal_confirmed=0 WHERE id=?",
                (new_board["id"], s["id"]))
            self._event("wrong_segment",
                        f"{person['name']} 纠正第 {slot + 1} 段错段："
                        f"换下原板，改用 {correct_board_code}，需重新确认连接与密封", task_id)
            self.conn.commit()

    def report_seal_damage(self, task_id: int, slot: int, staff_id: int,
                           note: str = "密封条破损") -> str:
        """密封条破损：旧条送修，自动领用备用条；无备用条时挂缺件待主管报损。"""
        person = self._require_role(staff_id, {"worker", "supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] not in ("claimed", "installing", "watch"):
                raise DomainError("当前状态不能上报密封条问题")
            s = self._board_slot(task_id, slot)
            old_seal = s["seal_id"]
            if old_seal:
                self.conn.execute(
                    "UPDATE equipment SET status='repair', active_task_id=NULL, "
                    "damage_note=?, updated_at=? WHERE id=?",
                    (note, now(), old_seal))
            spare = self._row(
                "SELECT * FROM equipment WHERE kind='seal' AND status='available' ORDER BY id LIMIT 1")
            self.conn.execute("UPDATE task_boards SET seal_id=NULL, seal_confirmed=0 WHERE id=?",
                              (s["id"],))
            if spare:
                self.conn.execute(
                    "UPDATE equipment SET status='locked', active_task_id=?, updated_at=? WHERE id=?",
                    (task_id, now(), spare["id"]))
                self.conn.execute("UPDATE task_boards SET seal_id=? WHERE id=?",
                                  (spare["id"], s["id"]))
                self._event("seal_damage",
                            f"{person['name']} 上报第 {slot + 1} 段密封条破损，已换用 {spare['code']}",
                            task_id)
                result = f"已更换备用密封条 {spare['code']}，请重新确认密封"
            else:
                self._event("seal_damage",
                            f"{person['name']} 上报第 {slot + 1} 段密封条破损，暂无备用条，挂缺件",
                            task_id)
                result = "暂无备用密封条，已挂缺件，需物业主管报损补充"
            self.conn.commit()
            return result

    # ---------- 封控与临时放行 ----------
    def confirm_closure(self, task_id: int, staff_id: int,
                        photo_path: str = "") -> None:
        """通道封控：保安可执行；必须所有分段连接、密封全部确认。"""
        person = self._require_role(staff_id, {"guard", "supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] not in ("claimed", "installing", "sealed", "watch"):
                raise DomainError("当前状态不能执行封控")
            total = self._row(
                "SELECT COUNT(*) c FROM task_boards WHERE task_id=?", (task_id,))["c"]
            ok = self._row(
                "SELECT COUNT(*) c FROM task_boards WHERE task_id=? "
                "AND conn_confirmed=1 AND seal_confirmed=1", (task_id,))["c"]
            if ok < total:
                raise DomainError(f"分段尚未全部确认（{ok}/{total}），不能封控通道")
            self.conn.execute(
                "INSERT INTO task_closures(task_id, closed, by_staff_id, at, photo_path) "
                "VALUES(?,1,?,?,?) ON CONFLICT(task_id) DO UPDATE SET closed=1, "
                "by_staff_id=excluded.by_staff_id, at=excluded.at, photo_path=excluded.photo_path",
                (task_id, staff_id, now(), photo_path))
            if task["status"] != "watch":
                self.conn.execute("UPDATE tasks SET status='watch' WHERE id=?", (task_id,))
            self.conn.execute(
                "UPDATE equipment SET status='deployed', updated_at=? WHERE active_task_id=?",
                (now(), task_id))
            self._event("closure", f"{person['name']} 完成通道封控，转入积水守看", task_id)
            self.conn.commit()

    def open_passage(self, task_id: int, plate: str, staff_id: int,
                     reason: str = "") -> int:
        """临时车辆放行：开封挡板通道。"""
        person = self._require_role(staff_id, {"guard", "supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] != "watch":
                raise DomainError("只有封控守看期间可以临时放行车辆")
            plate = (plate or "").strip().upper()
            if not plate:
                raise DomainError("请填写车牌号")
            cur = self.conn.execute(
                "INSERT INTO passages(task_id, plate, reason, opened_by) VALUES(?,?,?,?)",
                (task_id, plate, reason, staff_id))
            self._event("passage_open",
                        f"{person['name']} 临时放行车辆 {plate}（{reason or '紧急通行'}），通道开启",
                        task_id)
            self.conn.commit()
            return cur.lastrowid

    def reseal_passage(self, passage_id: int, staff_id: int,
                       photo_path: str = "") -> None:
        """车辆通过后重新封控并确认密封。"""
        person = self._require_role(staff_id, {"guard", "supervisor"})
        with self._lock:
            row = self._row("SELECT * FROM passages WHERE id=?", (passage_id,))
            if not row:
                raise DomainError("放行记录不存在")
            if row["resealed"]:
                raise DomainError("该通道已重新封控")
            self.conn.execute(
                "UPDATE passages SET resealed=1, resealed_at=?, reseal_confirmed_by=? WHERE id=?",
                (now(), staff_id, passage_id))
            self._event("passage_reseal",
                        f"{person['name']} 确认车辆 {row['plate']} 通过后重新封控密封",
                        row["task_id"])
            self.conn.commit()

    # ---------- 积水观察 / 人员迟到 ----------
    def add_observation(self, task_id: int, water_level_cm: float, staff_id: int,
                        note: str = "", photo_path: str = "") -> dict:
        person = self._require_role(staff_id, {"worker", "guard", "supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] not in ("sealed", "watch"):
                raise DomainError("封控后才能记录积水观察")
            self.conn.execute(
                "INSERT INTO observations(task_id, water_level_cm, note, by_staff_id, photo_path) "
                "VALUES(?,?,?,?,?)",
                (task_id, water_level_cm, note, staff_id, photo_path))
            alert = water_level_cm >= OBSERVATION_ALERT_CM
            if alert:
                self._event("water_alert",
                            f"{person['name']} 报告入口积水 {water_level_cm}cm 超过警戒值 "
                            f"{OBSERVATION_ALERT_CM}cm：{note or '加紧守看，准备强排'}", task_id)
            else:
                self._event("water_ok",
                            f"{person['name']} 记录积水 {water_level_cm}cm，可控", task_id)
            self.conn.commit()
            return {"alert": alert}

    def record_staff_late(self, task_id: int, staff_id: int, minutes: int,
                          reporter_id: int) -> None:
        """人员迟到：记录事件，任务可由其他在岗人员继续，不予阻塞。"""
        person = self._staff(staff_id)
        reporter = self._staff(reporter_id)
        with self._lock:
            self._event("staff_late",
                        f"{reporter['name']} 登记 {person['name']}（{person['phone'] or '无联系方式'}）"
                        f"迟到 {minutes} 分钟，已安排其他在岗人员顶替", task_id)
            self.conn.commit()

    # ---------- 拆除 / 清洗 / 晾干 ----------
    def approve_early_dismantle(self, task_id: int, supervisor_id: int,
                                reason: str) -> int:
        """物业主管审批提前拆除（预警尚未解除时）。"""
        person = self._require_role(supervisor_id, {"supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] not in ("sealed", "watch"):
                raise DomainError("仅已封控任务可以申请提前拆除")
            if not reason.strip():
                raise DomainError("提前拆除必须填写原因")
            if self._row(
                "SELECT id FROM task_approvals WHERE task_id=? AND kind='early_dismantle'",
                (task_id,)):
                raise DomainError("该任务已有提前拆除审批")
            cur = self.conn.execute(
                "INSERT INTO task_approvals(task_id, kind, by_staff_id, reason, at) "
                "VALUES(?,?,?,?,?)",
                (task_id, "early_dismantle", supervisor_id, reason, now()))
            self._event("early_approve",
                        f"物业主管 {person['name']} 批准提前拆除：{reason}", task_id)
            self.conn.commit()
            return cur.lastrowid

    def dismantle(self, task_id: int, staff_id: int) -> None:
        """拆除挡板；预警正常解除或持主管提前拆除审批方可执行。"""
        person = self._require_role(staff_id, {"worker", "supervisor", "guard"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] not in ("sealed", "watch"):
                raise DomainError("当前任务状态不能拆除")
            rnd = self._row("SELECT * FROM rounds WHERE id=?", (task["round_id"],))
            approval = self._row(
                "SELECT * FROM task_approvals WHERE task_id=? AND kind='early_dismantle'",
                (task_id,))
            if rnd["status"] != "lifted" and not approval:
                raise DomainError("预警尚未解除，禁止拆除；确需提前拆除须由物业主管审批")
            early = rnd["status"] != "lifted"
            self.conn.execute(
                "INSERT INTO task_dismantle(task_id, dismantled_by, dismantled_at) "
                "VALUES(?,?,?) ON CONFLICT(task_id) DO UPDATE SET "
                "dismantled_by=excluded.dismantled_by, dismantled_at=excluded.dismantled_at",
                (task_id, staff_id, now()))
            self.conn.execute(
                "UPDATE tasks SET status=? WHERE id=?",
                ("removed_early" if early else "cleaning", task_id))
            self.conn.execute(
                "UPDATE equipment SET status='cleaning', updated_at=? WHERE active_task_id=?",
                (now(), task_id))
            self._event("dismantle",
                        f"{person['name']} 拆除全部挡板与立柱"
                        f"（{'提前拆除，事后报备' if early else '预警已解除'}），进入清洗", task_id)
            self.conn.commit()

    def mark_cleaned(self, task_id: int, staff_id: int, note: str = "") -> None:
        self._require_role(staff_id, {"worker", "supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] not in ("cleaning", "removed_early"):
                raise DomainError("拆除后才能登记清洗")
            self.conn.execute(
                "INSERT INTO task_dismantle(task_id, cleaned_at, note) VALUES(?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET cleaned_at=excluded.cleaned_at, "
                "note=CASE WHEN ?='' THEN task_dismantle.note ELSE ? END",
                (task_id, now(), note, note, note))
            self.conn.execute("UPDATE tasks SET status='drying' WHERE id=?", (task_id,))
            self.conn.execute(
                "UPDATE equipment SET status='drying', updated_at=? WHERE active_task_id=?",
                (now(), task_id))
            self._event("cleaned", "器材清洗完成，进入晾干", task_id)
            self.conn.commit()

    def mark_dried(self, task_id: int, staff_id: int) -> None:
        self._require_role(staff_id, {"worker", "supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] != "drying":
                raise DomainError("清洗完成后才能登记晾干")
            self.conn.execute(
                "UPDATE task_dismantle SET dried_at=? WHERE task_id=? AND dried_at IS NULL",
                (now(), task_id))
            self.conn.execute("UPDATE tasks SET status='stocktaking' WHERE id=?", (task_id,))
            self.conn.execute(
                "UPDATE equipment SET status='returned', updated_at=? WHERE active_task_id=?",
                (now(), task_id))
            # 生成归架盘点清单（每件器材一条，默认在位）
            items = self._all(
                "SELECT id, rack_id FROM equipment WHERE active_task_id=?", (task_id,))
            for it in items:
                self.conn.execute(
                    "INSERT OR IGNORE INTO stocktake_items(task_id, equipment_id, state, rack_id) "
                    "VALUES(?,?,'present',?)", (task_id, it["id"], it["rack_id"]))
            self._event("dried", f"晾干完成，{len(items)} 件器材上存放架，开始归架盘点", task_id)
            self.conn.commit()

    # ---------- 归架盘点与缺件 ----------
    def check_stock_item(self, item_id: int, staff_id: int, state: str,
                         rack_id: int | None = None, note: str = "") -> None:
        person = self._require_role(staff_id, {"worker", "supervisor"})
        if state not in DIFF_STATES:
            raise DomainError("盘点结果只能是在位/缺失/损坏")
        with self._lock:
            row = self._row("SELECT * FROM stocktake_items WHERE id=?", (item_id,))
            if not row:
                raise DomainError("盘点项不存在")
            self.conn.execute(
                "UPDATE stocktake_items SET state=?, rack_id=COALESCE(?, rack_id), checked=1, "
                "note=? WHERE id=?",
                (state, rack_id, note, item_id))
            label = {"present": "在位", "missing": "缺失", "damaged": "损坏"}[state]
            eq = self._row("SELECT code FROM equipment WHERE id=?", (row["equipment_id"],))
            self._event("stock_check",
                        f"{person['name']} 盘点 {eq['code']}：{label}"
                        f"（{note}）" if note else f"{person['name']} 盘点 {eq['code']}：{label}",
                        row["task_id"])
            self.conn.commit()

    def report_loss(self, item_id: int, supervisor_id: int, reason: str) -> int:
        """缺件/损坏报损：物业主管决定，开具补充采购单，原器材标记丢失或维修。"""
        person = self._require_role(supervisor_id, {"supervisor"})
        with self._lock:
            row = self._row("SELECT * FROM stocktake_items WHERE id=?", (item_id,))
            if not row:
                raise DomainError("盘点项不存在")
            if row["state"] not in ("missing", "damaged"):
                raise DomainError("只有缺失或损坏项需要报损")
            task = self._task(row["task_id"])
            if not reason.strip():
                raise DomainError("报损必须说明原因")
            eq = self._row("SELECT * FROM equipment WHERE id=?", (row["equipment_id"],))
            dup = self._row(
                "SELECT id FROM replacement_orders WHERE equipment_id=? AND status!='received'",
                (eq["id"],))
            if dup:
                raise DomainError("该器材已有未完成的补充单")
            new_status = "lost" if row["state"] == "missing" else "repair"
            self.conn.execute(
                "UPDATE equipment SET status=?, damage_note=?, active_task_id=NULL, updated_at=? WHERE id=?",
                (new_status, reason, now(), eq["id"]))
            cur = self.conn.execute(
                "INSERT INTO replacement_orders(round_id, equipment_id, kind, reason, status, reported_by) "
                "VALUES(?,?,?,?,'reported',?)",
                (task["round_id"], eq["id"], eq["kind"], reason, supervisor_id))
            self._event("loss_report",
                        f"物业主管 {person['name']} 批准 {eq['code']} 报损（{row['state']}）：{reason}，"
                        f"已开补充采购单", row["task_id"])
            self.conn.commit()
            return cur.lastrowid

    def receive_replacement(self, order_id: int, new_code: str,
                            supervisor_id: int) -> int:
        """补充器材入库，重新可用。"""
        person = self._require_role(supervisor_id, {"supervisor"})
        with self._lock:
            order = self._row("SELECT * FROM replacement_orders WHERE id=?", (order_id,))
            if not order:
                raise DomainError("补充单不存在")
            if order["status"] == "received":
                raise DomainError("补充单已入库")
            if self._row("SELECT id FROM equipment WHERE code=?", (new_code,)):
                raise DomainError(f"器材编号 {new_code} 已存在")
            old = self._row("SELECT * FROM equipment WHERE id=?", (order["equipment_id"],))
            rack = old["rack_id"] if old else None
            cur = self.conn.execute(
                "INSERT INTO equipment(code, kind, length_cm, rack_id, status, updated_at) "
                "VALUES(?,?,?,?,'available',?)",
                (new_code, order["kind"], old["length_cm"] if old else 0, rack, now()))
            new_id = cur.lastrowid
            self.conn.execute(
                "UPDATE replacement_orders SET status='received', new_equipment_id=?, "
                "resolved_at=? WHERE id=?", (new_id, now(), order_id))
            self._event("replacement_in",
                        f"{person['name']} 确认补充器材 {new_code} 入库，器材重新可用")
            self.conn.commit()
            return new_id

    def finalize_stocktake(self, task_id: int, staff_id: int) -> dict:
        """归架核对完成：差异必须全部报损处理；并发完成只允许一次。"""
        self._require_role(staff_id, {"worker", "supervisor"})
        with self._lock:
            task = self._task(task_id)
            if task["status"] == "done":
                raise DomainError("任务已完成，请勿重复归架核对")
            if task["status"] != "stocktaking":
                raise DomainError("只有晾干上架后的任务才能核对归架")
            items = self._all(
                "SELECT si.*, e.code FROM stocktake_items si JOIN equipment e ON e.id=si.equipment_id "
                "WHERE si.task_id=?", (task_id,))
            if not items:
                raise DomainError("盘点清单缺失")
            unchecked = [i["code"] for i in items if not i["checked"]]
            if unchecked:
                raise DomainError(f"还有 {len(unchecked)} 件未盘点：{', '.join(unchecked)}")
            diffs = [i for i in items if i["state"] in ("missing", "damaged")]
            pending = []
            for i in diffs:
                # 只要存在报损补充单（采购中或已入库），差异即视为已处理
                order = self._row(
                    "SELECT id FROM replacement_orders WHERE equipment_id=?",
                    (i["equipment_id"],))
                if not order:
                    pending.append(f"{i['code']}({i['state']})")
            if pending:
                raise DomainError("归架差异未处理，须由物业主管报损后补充：" + "、".join(pending))
            # 条件更新：并发下只有一次能把任务从 stocktaking 推进到 done
            cur = self.conn.execute(
                "UPDATE tasks SET status='done', done_at=? WHERE id=? AND status='stocktaking'",
                (now(), task_id))
            if cur.rowcount == 0:
                raise DomainError("任务已被其他岗位完成核对，本次操作未生效")
            self.conn.execute(
                "UPDATE equipment SET status='available', active_task_id=NULL, updated_at=? "
                "WHERE active_task_id=? AND status='returned'", (now(), task_id))
            self.conn.commit()
            self._event("stock_done",
                        f"归架核对完成，在位器材恢复可用；差异 {len(diffs)} 件均已报损补充流程",
                        task_id)
            return {"differences": len(diffs), "total": len(items)}
