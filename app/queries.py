"""只读查询模型：为看板与各页面聚合数据。"""
from __future__ import annotations

from .db import get_conn

LEVEL_LABEL = {"blue": "蓝色", "yellow": "黄色", "orange": "橙色", "red": "红色",
               "lifted": "已解除"}
TASK_LABEL = {
    "planned": "待领件", "claimed": "已领件", "installing": "安装中",
    "sealed": "已封控", "watch": "守看中", "dismantling": "拆除中",
    "cleaning": "待清洗", "drying": "晾干中", "stocktaking": "归架盘点",
    "done": "已完成", "removed_early": "提前拆除",
}


def _dicts(rows):
    return [dict(r) for r in rows]


class Queries:
    def __init__(self, db_path: str):
        self.db_path = db_path

    @property
    def conn(self):
        return get_conn(self.db_path)

    def staff_list(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM staff ORDER BY CASE role WHEN 'supervisor' THEN 0 "
            "WHEN 'guard' THEN 1 ELSE 2 END, id").fetchall()
        return _dicts(rows)

    def equipment_list(self, kind: str | None = None) -> list[dict]:
        sql = ("SELECT e.*, r.name rack_name FROM equipment e LEFT JOIN racks r ON r.id=e.rack_id")
        args: list = []
        if kind:
            sql += " WHERE e.kind=?"
            args.append(kind)
        sql += " ORDER BY e.kind, e.code"
        return _dicts(self.conn.execute(sql, args).fetchall())

    def entrances(self) -> list[dict]:
        return _dicts(self.conn.execute(
            "SELECT * FROM entrances ORDER BY layout_order, id").fetchall())

    def active_round(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM rounds WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def recent_rounds(self, limit: int = 8) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM rounds ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return _dicts(rows)

    def task_detail(self, task_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT t.*, e.name entrance_name, e.code entrance_code, e.width_slots, "
            "e.post_count, e.ramp_desc, r.level round_level, r.status round_status, "
            "s.name claimed_by_name "
            "FROM tasks t JOIN entrances e ON e.id=t.entrance_id "
            "JOIN rounds r ON r.id=t.round_id "
            "LEFT JOIN staff s ON s.id=t.claimed_by WHERE t.id=?",
            (task_id,)).fetchone()
        return dict(row) if row else None

    def task_boards(self, task_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT tb.*, b.code board_code, sl.code seal_code, sl.status seal_status "
            "FROM task_boards tb LEFT JOIN equipment b ON b.id=tb.board_id "
            "LEFT JOIN equipment sl ON sl.id=tb.seal_id "
            "WHERE tb.task_id=? ORDER BY tb.slot", (task_id,)).fetchall()
        return _dicts(rows)

    def task_posts(self, task_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT tp.*, p.code post_code FROM task_posts tp "
            "LEFT JOIN equipment p ON p.id=tp.post_id "
            "WHERE tp.task_id=? ORDER BY tp.slot", (task_id,)).fetchall()
        return _dicts(rows)

    def task_list(self, round_id: int | None = None,
                  include_all: bool = False) -> list[dict]:
        sql = ("SELECT t.*, e.name entrance_name, e.code entrance_code, r.level round_level, "
               "r.status round_status FROM tasks t JOIN entrances e ON e.id=t.entrance_id "
               "JOIN rounds r ON r.id=t.round_id")
        args: list = []
        if round_id:
            sql += " WHERE t.round_id=?"
            args.append(round_id)
        elif not include_all:
            sql += " WHERE r.status='active'"
        sql += " ORDER BY t.id DESC"
        return _dicts(self.conn.execute(sql, args).fetchall())

    def board_overview(self) -> list[dict]:
        """看板：按入口聚合任务进度。"""
        ents = self.entrances()
        tasks = {t["entrance_id"]: t for t in self.task_list()}
        out = []
        for ent in ents:
            item = {**ent, "task": None}
            t = tasks.get(ent["id"])
            if t:
                boards = self.task_boards(t["id"])
                installed = sum(1 for b in boards if b["conn_confirmed"])
                sealed = sum(1 for b in boards if b["seal_confirmed"])
                filled = sum(1 for b in boards if b["board_id"])
                closure = self.conn.execute(
                    "SELECT closed FROM task_closures WHERE task_id=?",
                    (t["id"],)).fetchone()
                item["task"] = {
                    "id": t["id"], "status": t["status"],
                    "status_label": TASK_LABEL.get(t["status"], t["status"]),
                    "round_level": t["round_level"],
                    "filled": filled, "installed": installed, "sealed": sealed,
                    "total": ent["width_slots"],
                    "closed": bool(closure and closure["closed"]),
                }
            out.append(item)
        return out

    def observations(self, task_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT o.*, s.name staff_name FROM observations o "
            "LEFT JOIN staff s ON s.id=o.by_staff_id "
            "WHERE o.task_id=? ORDER BY o.id DESC", (task_id,)).fetchall()
        return _dicts(rows)

    def passages(self, task_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT p.*, s.name staff_name FROM passages p "
            "LEFT JOIN staff s ON s.id=p.opened_by WHERE p.task_id=? ORDER BY p.id DESC",
            (task_id,)).fetchall()
        return _dicts(rows)

    def stocktake_items(self, task_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT si.*, e.code, e.kind, r.name rack_name FROM stocktake_items si "
            "JOIN equipment e ON e.id=si.equipment_id "
            "LEFT JOIN racks r ON r.id=si.rack_id WHERE si.task_id=? ORDER BY e.kind, e.code",
            (task_id,)).fetchall()
        return _dicts(rows)

    def replacement_orders(self, active_only: bool = False) -> list[dict]:
        sql = ("SELECT o.*, e.code equipment_code, s.name reported_by_name, "
               "n.code new_code FROM replacement_orders o "
               "JOIN equipment e ON e.id=o.equipment_id "
               "LEFT JOIN staff s ON s.id=o.reported_by "
               "LEFT JOIN equipment n ON n.id=o.new_equipment_id")
        if active_only:
            sql += " WHERE o.status!='received'"
        sql += " ORDER BY o.id DESC"
        return _dicts(self.conn.execute(sql).fetchall())

    def approvals(self, task_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT a.*, s.name staff_name FROM task_approvals a "
            "LEFT JOIN staff s ON s.id=a.by_staff_id WHERE a.task_id=?",
            (task_id,)).fetchall()
        return _dicts(rows)

    def events(self, task_id: int | None = None, limit: int = 30) -> list[dict]:
        if task_id:
            rows = self.conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task_id, limit)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return _dicts(rows)

    def dismantle(self, task_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM task_dismantle WHERE task_id=?", (task_id,)).fetchone()
        return dict(row) if row else None
