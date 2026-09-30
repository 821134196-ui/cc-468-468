"""SQLite 持久化层：连接管理与建表。"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
-- 车库入口
CREATE TABLE IF NOT EXISTS entrances (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,                 -- 如：东门车库入口
    code TEXT NOT NULL UNIQUE,
    width_slots INTEGER NOT NULL,       -- 需要的挡板段数（含立柱间隔）
    post_count INTEGER NOT NULL,        -- 需要立柱数（段数+1）
    ramp_desc TEXT NOT NULL DEFAULT '', -- 坡道说明
    layout_order INTEGER NOT NULL DEFAULT 0
);

-- 存放架
CREATE TABLE IF NOT EXISTS racks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    zone TEXT NOT NULL DEFAULT 'A'
);

-- 通用器材表：挡板分段 / 立柱 / 密封条
CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,          -- 资产编号 DB-01 / POST-01 / SEAL-01
    kind TEXT NOT NULL,                 -- board | post | seal
    length_cm INTEGER NOT NULL DEFAULT 0,
    rack_id INTEGER REFERENCES racks(id),
    status TEXT NOT NULL DEFAULT 'available',
    -- available 可用 | locked 已锁定 | deployed 已部署 | cleaning 清洗中
    -- drying 晾干中 | returned 已归架待核 | lost 报损 | repair 维修
    active_task_id INTEGER,            -- 当前占用的部署任务
    damage_note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 现场人员（演示数据）
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL,                 -- guard 保安 | worker 防汛员 | supervisor 物业主管
    phone TEXT NOT NULL DEFAULT '',
    on_duty INTEGER NOT NULL DEFAULT 1
);

-- 预警轮次
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY,
    level TEXT NOT NULL DEFAULT 'blue', -- blue | yellow | orange | red | lifted 已解除
    status TEXT NOT NULL DEFAULT 'active', -- active | lifted
    rain_rate REAL NOT NULL DEFAULT 0,  -- 毫米/小时
    water_level REAL NOT NULL DEFAULT 0,-- 厘米
    started_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    lifted_at TEXT,
    lift_kind TEXT NOT NULL DEFAULT 'normal', -- normal | early
    lift_approver_id INTEGER,
    lift_reason TEXT NOT NULL DEFAULT '',
    weather_note TEXT NOT NULL DEFAULT ''
);

-- 部署任务（一个入口在一个预警轮次中一条）
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    round_id INTEGER NOT NULL REFERENCES rounds(id),
    entrance_id INTEGER NOT NULL REFERENCES entrances(id),
    status TEXT NOT NULL DEFAULT 'planned',
    -- planned 清单已生成 | claimed 已领件 | installing 安装中 | sealed 封控完成
    -- watch 守看 | dismantling 拆除中 | cleaning 清洗 | drying 晾干
    -- stocktaking 归架核对 | done 已完成 | removed_early 提前拆除
    claimed_by INTEGER REFERENCES staff(id),
    claimed_at TEXT,
    completed_at TEXT,
    done_at TEXT,
    UNIQUE(round_id, entrance_id)
);

-- 任务分段：按拼接顺序排列的槽位
CREATE TABLE IF NOT EXISTS task_boards (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    slot INTEGER NOT NULL,              -- 从左到右 0..n-1
    board_id INTEGER REFERENCES equipment(id),
    seal_id INTEGER REFERENCES equipment(id),
    conn_confirmed INTEGER NOT NULL DEFAULT 0,  -- 连接确认
    seal_confirmed INTEGER NOT NULL DEFAULT 0,  -- 密封确认
    photo_path TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(task_id, slot)
);

-- 任务立柱（两端+段间）
CREATE TABLE IF NOT EXISTS task_posts (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    slot INTEGER NOT NULL,              -- 0..post_count-1
    post_id INTEGER REFERENCES equipment(id),
    UNIQUE(task_id, slot)
);

-- 通道封控确认
CREATE TABLE IF NOT EXISTS task_closures (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
    closed INTEGER NOT NULL DEFAULT 0,
    by_staff_id INTEGER REFERENCES staff(id),
    at TEXT,
    photo_path TEXT NOT NULL DEFAULT ''
);

-- 临时车辆放行记录
CREATE TABLE IF NOT EXISTS passages (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    plate TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    opened_by INTEGER REFERENCES staff(id),
    opened_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    resealed INTEGER NOT NULL DEFAULT 0,
    resealed_at TEXT,
    reseal_confirmed_by INTEGER REFERENCES staff(id)
);

-- 积水观察
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    water_level_cm REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    by_staff_id INTEGER REFERENCES staff(id),
    photo_path TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 拆除 / 清洗 / 晾干 节点
CREATE TABLE IF NOT EXISTS task_dismantle (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
    dismantled_by INTEGER REFERENCES staff(id),
    dismantled_at TEXT,
    cleaned_at TEXT,
    dried_at TEXT,
    note TEXT NOT NULL DEFAULT ''
);

-- 归架盘点：每件器材归架核对结果
CREATE TABLE IF NOT EXISTS stocktake_items (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    equipment_id INTEGER NOT NULL REFERENCES equipment(id),
    state TEXT NOT NULL DEFAULT 'present', -- present 在位 | missing 缺失 | damaged 损坏
    rack_id INTEGER REFERENCES racks(id),
    checked INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    UNIQUE(task_id, equipment_id)
);

-- 缺件报损与补充单
CREATE TABLE IF NOT EXISTS replacement_orders (
    id INTEGER PRIMARY KEY,
    round_id INTEGER REFERENCES rounds(id),
    equipment_id INTEGER REFERENCES equipment(id),
    kind TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'reported', -- reported 已报损 | ordered 已采购 | received 已入库
    reported_by INTEGER REFERENCES staff(id),
    new_equipment_id INTEGER REFERENCES equipment(id),
    at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    resolved_at TEXT
);

-- 提前拆除审批（物业主管）
CREATE TABLE IF NOT EXISTS task_approvals (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL DEFAULT 'early_dismantle',
    by_staff_id INTEGER REFERENCES staff(id),
    reason TEXT NOT NULL,
    at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    UNIQUE(task_id, kind)
);

-- 事件流水
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    kind TEXT NOT NULL,
    task_id INTEGER,
    detail TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_equipment_status ON equipment(status, kind);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
"""


_lock = threading.local()


def get_conn(db_path: str | Path) -> sqlite3.Connection:
    """每线程一个连接；启用外键与 WAL。"""
    path = str(db_path)
    if not hasattr(_lock, "conns"):
        _lock.conns = {}
    conn = _lock.conns.get(path)
    if conn is None:
        conn = sqlite3.connect(path, check_same_thread=False, timeout=30,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        _lock.conns[path] = conn
    return conn


def init_db(db_path: str | Path) -> None:
    conn = get_conn(db_path)
    conn.executescript(SCHEMA)
    conn.commit()
