"""演示数据：一条启动命令载入「部署中 / 归架缺件 / 提前解除」三类样例。"""
from __future__ import annotations

from pathlib import Path

from . import photos
from .db import get_conn, init_db
from .services import Service


def seed(db_path: str | Path, reset: bool = True) -> None:
    db_path = Path(db_path)
    if reset and db_path.exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_path) + suffix)
            if p.exists():
                p.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    conn = get_conn(db_path)
    svc = Service(str(db_path))

    # ---------- 存放架 ----------
    conn.executemany(
        "INSERT INTO racks(id,name,zone) VALUES(?,?,?)",
        [(1, "一号存放架（挡板）", "A"), (2, "二号存放架（立柱/密封条）", "B")],
    )

    # ---------- 入口 ----------
    conn.executemany(
        "INSERT INTO entrances(id,name,code,width_slots,post_count,ramp_desc,layout_order) "
        "VALUES(?,?,?,?,?,?,?)",
        [
            (1, "东门车库入口", "EAST", 4, 5, "坡道长 28 米，双向车道，左侧设截水沟", 1),
            (2, "西门车库入口", "WEST", 3, 4, "坡道长 22 米，单进单出，雨水管在右侧", 2),
            (3, "北门车库入口", "NORTH", 2, 3, "坡道长 15 米，紧邻商铺，人行混行", 3),
        ],
    )

    # ---------- 器材：挡板 / 立柱 / 密封条，预留备件 ----------
    eq_rows = []
    for i in range(1, 15):
        eq_rows.append((f"DB-{i:02d}", "board", 100, 1))
    for i in range(1, 17):
        eq_rows.append((f"POST-{i:02d}", "post", 120, 2))
    for i in range(1, 15):
        eq_rows.append((f"SEAL-{i:02d}", "seal", 100, 2))
    conn.executemany(
        "INSERT INTO equipment(code,kind,length_cm,rack_id,status) VALUES(?,?,?,?,'available')",
        eq_rows,
    )

    # ---------- 现场人员（演示数据） ----------
    conn.executemany(
        "INSERT INTO staff(id,name,role,phone,on_duty) VALUES(?,?,?,?,1)",
        [
            (1, "王建国", "guard", "138-0000-0001"),
            (2, "李守夜", "guard", "138-0000-0002"),
            (3, "张伟", "worker", "138-0000-0003"),
            (4, "陈强", "worker", "138-0000-0004"),
            (5, "赵敏", "worker", "138-0000-0005"),
            (6, "周主管", "supervisor", "138-0000-0006"),
        ],
    )
    conn.commit()

    # ---------- 样例照片 ----------
    p_conn1 = photos.make_sample_png("sample_conn_1.png")
    p_seal = photos.make_sample_png("sample_seal_1.png", band=(60, 64, 70))
    p_closure = photos.make_sample_png("sample_closure.png", warn=True)
    p_water = photos.make_sample_png("sample_water.png", band=(43, 108, 163), warn=True)

    # ========== 样例三：提前解除（已闭环的历史轮次） ==========
    rc = svc.create_round(30, 16, "阵雨带过境")
    svc.update_round_reading(30, 16, "黄色预警")
    tc = svc.generate_checklist(rc, 3)              # 北门 2 段
    svc.claim(tc, 5)
    svc.begin_install(tc, 5)
    svc.confirm_connection(tc, 0, 5, p_conn1)
    svc.confirm_seal(tc, 0, 5, p_seal)
    svc.confirm_connection(tc, 1, 5, p_conn1)
    svc.confirm_seal(tc, 1, 5, p_seal)
    svc.confirm_closure(tc, 2, p_closure)
    svc.add_observation(tc, 6, 2, "北门封控后巡查，水位 6cm", p_water)
    svc.approve_early_dismantle(tc, 6, "上游雨带偏离，预报后续无降雨，商铺需恢复通行")
    svc.dismantle(tc, 5)                            # 审批通过，可提前拆
    svc.mark_cleaned(tc, 5, "清水冲洗，含沙量低")
    svc.mark_dried(tc, 5)
    items = conn.execute(
        "SELECT id FROM stocktake_items WHERE task_id=? ORDER BY id", (tc,)).fetchall()
    for it in items:
        svc.check_stock_item(it["id"], 5, "present")
    svc.finalize_stocktake(tc, 5)
    svc.lift_round(rc, "early", 6, "雨带偏离，现场连续复查无积水，提前解除")

    # ========== 样例二：归架缺件（待主管报损的盘点中任务） ==========
    rb = svc.create_round(28, 15, "持续降雨")
    svc.update_round_reading(28, 15, "黄色预警")
    tb = svc.generate_checklist(rb, 2)              # 西门 3 段
    svc.claim(tb, 4)
    svc.begin_install(tb, 4)
    for slot in range(3):
        svc.confirm_connection(tb, slot, 4, p_conn1)
        svc.confirm_seal(tb, slot, 4, p_seal)
    svc.confirm_closure(tb, 1, p_closure)
    svc.add_observation(tb, 8, 4, "西门水位 8cm，排水正常", p_water)
    svc.lift_round(rb, "normal")
    svc.dismantle(tb, 4)
    svc.mark_cleaned(tb, 4, "挡泥较多，重点刷洗底部")
    svc.mark_dried(tb, 4)
    stock = conn.execute(
        "SELECT si.id, e.code FROM stocktake_items si JOIN equipment e ON e.id=si.equipment_id "
        "WHERE si.task_id=? ORDER BY si.id", (tb,)).fetchall()
    for it in stock:
        if it["code"] == "SEAL-03":
            # 归架差异：密封条胶面撕裂，等待物业主管报损
            svc.check_stock_item(it["id"], 6, "damaged", note="胶面撕裂约 5cm，无法继续使用")
        else:
            svc.check_stock_item(it["id"], 4, "present")

    # ========== 样例一：部署中（活动黄色预警） ==========
    ra = svc.create_round(12, 6, "降雨回波东移")
    svc.update_round_reading(30, 16, "黄色预警：各入口待命安装")
    # 预警达到阈值，按全部入口生成部署清单
    for ent_id in (1, 2, 3):
        svc.generate_checklist(ra, ent_id)
    ta = conn.execute(
        "SELECT id FROM tasks WHERE round_id=? AND entrance_id=1", (ra,)).fetchone()["id"]
    svc.claim(ta, 3)                                # 东门领件，锁定 4 板 5 柱 4 条
    svc.begin_install(ta, 3)
    svc.confirm_connection(ta, 0, 3, p_conn1, "左端贴墙段")
    svc.confirm_seal(ta, 0, 3, p_seal)
    svc.confirm_connection(ta, 1, 3, p_conn1, "左中段")
    svc.confirm_seal(ta, 1, 3, p_seal)
    svc.confirm_connection(ta, 2, 3, "", "右中连接已卡紧，密封待查")
    # 第 4 段（slot 3）尚未安装 —— 示意图上显示为空槽
    svc.record_staff_late(ta, 5, 15, 3)             # 人员迟到，已安排顶替

    conn.commit()
    return {"active_round": ra, "east_task": ta,
            "west_task": tb, "north_task": tc}


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "data/garrison.db"
    info = seed(target)
    print("演示数据已载入：", info)
