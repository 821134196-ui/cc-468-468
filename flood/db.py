"""SQLite 连接、建表与基础访问。

单连接 + 进程锁：HTTP 工作线程、天气调度线程与测试共用同一连接，
关键业务事务使用 BEGIN IMMEDIATE，保证器材领用等排他操作串行化。
"""
import json
import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS entrance (
    id          INTEGER PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    slot_specs  TEXT NOT NULL,          -- JSON: 每段挡板设计宽度(米)，按拼接顺序
    zone        TEXT,
    sort_no     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS item (
    id              INTEGER PRIMARY KEY,
    code            TEXT UNIQUE NOT NULL,
    kind            TEXT NOT NULL,      -- segment 挡板 / post 立柱 / seal 密封条
    width           REAL,               -- 挡板宽度(米)
    rack_code       TEXT,               -- 存放架编号
    rack_pos        TEXT,               -- 架位
    status          TEXT NOT NULL DEFAULT 'available',
    -- available 可用 / locked 已锁定 / installed 已安装 / removed 已拆除待归架
    -- / damaged 破损 / written_off 报损
    holder_task_id  INTEGER,
    installed_slot  INTEGER,
    seal_id         INTEGER,            -- 挡板当前配用的密封条 id
    replaced_from   INTEGER             -- 补件替换了哪件旧器材
);

CREATE TABLE IF NOT EXISTS person (
    id     INTEGER PRIMARY KEY,
    name   TEXT NOT NULL,
    role   TEXT NOT NULL,               -- supervisor 主管 / installer 安装工 / guard 保安 / observer 巡查
    phone  TEXT,
    status TEXT NOT NULL DEFAULT 'idle' -- idle / assigned / late / onsite
);

CREATE TABLE IF NOT EXISTS weather_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    level       TEXT NOT NULL,          -- NONE / BLUE / YELLOW / ORANGE / RED
    rainfall    REAL NOT NULL DEFAULT 0,
    low_ticks   INTEGER NOT NULL DEFAULT 0,  -- 雨量低于蓝警的连续计数（复查）
    tick        INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS rainfall_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    tick    INTEGER,
    mm      REAL NOT NULL,
    level   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS water_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entrance_id INTEGER NOT NULL,
    ts          TEXT NOT NULL,
    level_cm    REAL NOT NULL,
    source      TEXT DEFAULT '人工',     -- 人工 / 监测设备
    note        TEXT,
    observer    TEXT
);

CREATE TABLE IF NOT EXISTS task (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    entrance_id      INTEGER NOT NULL,
    level_triggered  TEXT,
    status           TEXT NOT NULL,     -- generated 清单已生成 / picking 领用中 /
    -- installing 安装中 / installed 已封控 / cleaning 清洗中 / drying 晾干中 /
    -- racking 归架核对中 / closed 已结案
    warning_cleared  INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT,
    picked_at        TEXT,
    installed_at     TEXT,
    removed_at       TEXT,
    cleaned_at       TEXT,
    dried_at         TEXT,
    closed_at        TEXT,
    closure_by       INTEGER,
    closure_at       TEXT,
    closure_photo    TEXT
);

CREATE TABLE IF NOT EXISTS task_line (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id   INTEGER NOT NULL,
    line_type TEXT NOT NULL,            -- segment / post
    slot_no   INTEGER,                  -- segment 对应槽位
    item_id   INTEGER,                  -- NULL 表示库存不足，待补件
    label     TEXT NOT NULL,
    status    TEXT NOT NULL DEFAULT 'pending'
    -- pending 待领 / locked 已领 / returned 已归架 / nostock 缺货待补
);

CREATE TABLE IF NOT EXISTS task_slot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL,
    slot_no       INTEGER NOT NULL,
    expected_width REAL NOT NULL,
    segment_id    INTEGER,
    conn_by       INTEGER,
    conn_at       TEXT,
    conn_photo    TEXT,
    seal_by       INTEGER,
    seal_at       TEXT,
    seal_photo    TEXT,
    ctrl_by       INTEGER,              -- 通道封控由保安确认
    ctrl_at       TEXT,
    ctrl_photo    TEXT,
    seal_ok       INTEGER NOT NULL DEFAULT 1,  -- 密封条是否完好
    opened        INTEGER NOT NULL DEFAULT 0   -- 临时车辆放行中
);

CREATE TABLE IF NOT EXISTS task_assignment (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    person_id   INTEGER NOT NULL,
    duty        TEXT NOT NULL,
    due_at      TEXT,
    arrived_at  TEXT,
    late        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS passage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    slot_no     INTEGER NOT NULL,
    plate       TEXT,
    guard_id    INTEGER,
    opened_at   TEXT,
    restored_at TEXT
);

CREATE TABLE IF NOT EXISTS incident (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER,
    entrance_id INTEGER,
    type        TEXT NOT NULL,          -- wrong_segment / seal_damage / late / water / early_removal ...
    detail      TEXT,
    created_at  TEXT,
    resolved_at TEXT,
    resolution  TEXT
);

CREATE TABLE IF NOT EXISTS removal_approval (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL,
    requested_by INTEGER,
    reason       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending', -- pending / approved / rejected
    decided_by   INTEGER,
    decided_at   TEXT,
    note         TEXT,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS photo (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER,
    slot_no     INTEGER,
    kind        TEXT NOT NULL,          -- conn / seal / closure / clean / dry
    path        TEXT NOT NULL,
    uploaded_by INTEGER,
    created_at  TEXT
);

CREATE TABLE IF NOT EXISTS rack_check (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    expected    TEXT NOT NULL DEFAULT '[]',  -- JSON item ids
    scanned     TEXT NOT NULL DEFAULT '[]',
    open_diff   INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS loss_report (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       INTEGER NOT NULL,
    item_id       INTEGER NOT NULL,
    supervisor_id INTEGER NOT NULL,
    note          TEXT,
    created_at    TEXT
);

CREATE TABLE IF NOT EXISTS replenish_order (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER,
    kind        TEXT NOT NULL,
    width       REAL,
    qty         INTEGER NOT NULL,
    new_codes   TEXT,
    status      TEXT NOT NULL DEFAULT 'received',
    created_at  TEXT,
    received_at TEXT
);

CREATE TABLE IF NOT EXISTS event_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     TEXT NOT NULL,
    actor  TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
"""

LEVELS = ["NONE", "BLUE", "YELLOW", "ORANGE", "RED"]
LEVEL_NAME = {"NONE": "无预警", "BLUE": "蓝色 IV", "YELLOW": "黄色 III",
              "ORANGE": "橙色 II", "RED": "红色 I"}
DEPLOY_LEVEL = "YELLOW"          # 达到黄警生成部署清单
WATER_BLOCK_CM = 15.0            # 入口积水超过此值禁止拆除


class DB:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        self.init()

    def init(self):
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def q(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def begin(self):
        """进入立即写事务，用于排他领用等竞争场景。"""
        self.lock.acquire()
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self):
        try:
            self.conn.commit()
        finally:
            self.lock.release()

    def rollback(self):
        try:
            self.conn.rollback()
        finally:
            self.lock.release()


def now_iso():
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
