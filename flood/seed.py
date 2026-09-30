"""演示数据与样例载入：人员/天气为演示数据。

一条启动命令（python -m flood.app --demo）载入三类样例：
  1) 部署样例   —— 东门任务已安装封控
  2) 缺件样例   —— 西门任务领用中，2.0m 挡板库存不足一件
  3) 提前解除样例 —— 北门任务已封控，预警未解除、拆除申请待主管审批
"""
import json

from .db import now_iso
from .service import Service


def reset(db):
    for tbl in ("event_log", "replenish_order", "loss_report", "rack_check", "photo",
                "removal_approval", "incident", "passage", "task_assignment",
                "task_slot", "task_line", "task", "water_log", "rainfall_log",
                "weather_state", "item", "person", "entrance"):
        db.execute(f"DELETE FROM {tbl}")


def _add_item(db, code, kind, width=None, rack="A架", pos="—"):
    db.execute(
        "INSERT INTO item(code,kind,width,rack_code,rack_pos,status) VALUES (?,?,?,?,?,'available')",
        (code, kind, width, rack, pos))


def load_demo(db, adapter=None):
    reset(db)
    svc = Service(db)

    # ----------------------------------------------------------- 入口
    db.execute(
        "INSERT INTO entrance(id,code,name,slot_specs,zone,sort_no) VALUES (1,'E','东门',?,'东区',1)",
        (json.dumps([2.0, 1.5, 2.0]),))
    db.execute(
        "INSERT INTO entrance(id,code,name,slot_specs,zone,sort_no) VALUES (2,'W','西门',?,'西区',2)",
        (json.dumps([2.0, 2.0]),))
    db.execute(
        "INSERT INTO entrance(id,code,name,slot_specs,zone,sort_no) VALUES (3,'N','北门',?,'北区',3)",
        (json.dumps([2.0, 2.0]),))

    # ----------------------------------------------------------- 人员（演示）
    people = [
        ("王主管", "supervisor", "138-0000-0001"),
        ("李安装", "installer", "138-0000-0002"),
        ("赵安装", "installer", "138-0000-0003"),
        ("陈保安", "guard", "138-0000-0004"),
        ("周巡查", "observer", "138-0000-0005"),
    ]
    for i, (n, r, ph) in enumerate(people, start=1):
        db.execute("INSERT INTO person(id,name,role,phone) VALUES (?,?,?,?)", (i, n, r, ph))

    # ----------------------------------------------------------- 器材库存
    # 2.0m 挡板仅 5 件：东门 2 + 北门 2 + 西门领到 1，西门第二件缺货
    for i in range(1, 6):
        _add_item(db, f"SG-20-{i:02d}", "segment", 2.0, "A架", f"2米区-{i}")
    for i in range(1, 4):
        _add_item(db, f"SG-15-{i:02d}", "segment", 1.5, "A架", f"1.5米区-{i}")
    for i in range(1, 12):
        _add_item(db, f"POST-{i:02d}", "post", None, "B架", f"立柱位-{i}")
    for i in range(1, 10):
        _add_item(db, f"SEAL-{i:02d}", "seal", None, "C架", f"密封条位-{i}")

    if adapter is not None:
        adapter.set_factor("E", 0.18)
        adapter.set_factor("W", 0.30)
        adapter.set_factor("N", 0.42)

    # ----------------------------------------------------------- 初始天气：橙警
    db.execute(
        "INSERT INTO weather_state(id,level,rainfall,low_ticks,tick,updated_at) "
        "VALUES (1,'ORANGE',33,0,5,?)", (now_iso(),))
    script_prev = [(1, 9, "BLUE"), (2, 16, "BLUE"), (3, 24, "YELLOW"),
                   (4, 33, "ORANGE"), (5, 33, "ORANGE")]
    for tk, mm, lv in script_prev:
        db.execute("INSERT INTO rainfall_log(ts,tick,mm,level) VALUES (?,?,?,?)",
                   (now_iso(), tk, mm, lv))
    if adapter is not None:
        adapter.tick = 5

    def install_full(tid):
        """领齐 → 安装 → 三段确认 → 完成封控。"""
        for line in db.q("SELECT * FROM task_line WHERE task_id=? ORDER BY id", (tid,)):
            width = None
            if line["line_type"] == "segment":
                s = db.one("SELECT expected_width FROM task_slot WHERE task_id=? AND slot_no=?",
                           (tid, line["slot_no"]))
                width = s["expected_width"]
            item = svc._available_item(line["line_type"], width)
            svc.pick_line(tid, line["id"], item["code"], 2)
        svc.finish_picking(tid, 2)
        nslots = db.one("SELECT COUNT(*) c FROM task_slot WHERE task_id=?", (tid,))["c"]
        for n in range(1, nslots + 1):
            svc.confirm_slot(tid, n, "conn", 2)
            svc.confirm_slot(tid, n, "seal", 2)
            svc.confirm_slot(tid, n, "ctrl", 4)
        svc.complete_install(tid, 2)

    # ① 部署样例：东门已封控，派工含一名迟到待签到人员，入口少量积水
    t1 = svc.generate_task(1, "ORANGE", actor="演示数据")
    db.execute(
        "INSERT INTO task_assignment(task_id,person_id,duty,due_at) VALUES (?,?, '现场指挥','')",
        (t1, 1))
    db.execute(
        "INSERT INTO task_assignment(task_id,person_id,duty,due_at,arrived_at) "
        "VALUES (?,?, '挡板安装','',?)", (t1, 2, now_iso()))
    db.execute(
        "INSERT INTO task_assignment(task_id,person_id,duty,due_at,arrived_at) "
        "VALUES (?,?, '通道封控','',?)", (t1, 4, now_iso()))
    db.execute(
        "INSERT INTO task_assignment(task_id,person_id,duty,due_at) VALUES (?,?, '积水巡查',?)",
        (t1, 5, "2026-09-30 08:30:00"))
    install_full(t1)
    db.execute(
        "INSERT INTO water_log(entrance_id,ts,level_cm,source,note,observer) "
        "VALUES (1,?,?, '人工','坡道正常排水','周巡查')", (now_iso(), 5.0))

    # ② 缺件样例：西门 2.0m 挡板只领到 1 件，第 2 件库存不足
    t2 = svc.generate_task(2, "ORANGE", actor="演示数据")
    for line in db.q(
            "SELECT * FROM task_line WHERE task_id=? AND status='pending' ORDER BY id", (t2,)):
        if line["line_type"] == "segment" and line["slot_no"] == 2:
            continue  # 该件无库存
        width = 2.0
        item = svc._available_item(line["line_type"], width)
        svc.pick_line(t2, line["id"], item["code"], 3)
    svc._incident("shortage", t=db.one("SELECT * FROM task WHERE id=?", (t2,)),
                  detail="缺件：2号槽挡板 2m（库存 2.0m 挡板已全部被占用），需补充入库")

    # ③ 提前解除样例：北门已封控，安装工申请提前拆除，待主管审批
    t3 = svc.generate_task(3, "ORANGE", actor="演示数据")
    install_full(t3)
    db.execute(
        "INSERT INTO water_log(entrance_id,ts,level_cm,source,note,observer) "
        "VALUES (3,?,?, '监测设备','','监测设备')", (now_iso(), 12.0))
    svc.request_removal(t3, 2, "业主应急车队需在汛中出库，申请提前拆除北门挡板")

    svc.log("演示数据载入", "东门=已部署封控；西门=领用缺件；北门=提前拆除待审批")
    return {"deployed_task": t1, "shortage_task": t2, "early_removal_task": t3}
