"""数据模型层：SQLite 按 架构设计方案 的 9 张表建模。

生产替换点：SQLite → PostgreSQL + pgvector（表结构一致），
SQLAlchemy/Alembic 管理迁移。此处零依赖实现。
"""
import json
import os
import sqlite3
import time

from ..core import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('employee','admin')),
    station TEXT DEFAULT '',
    is_active INTEGER DEFAULT 1,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS kb_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    doc_type TEXT DEFAULT '工艺卡',
    file_path TEXT DEFAULT '',
    version INTEGER DEFAULT 1,
    status TEXT DEFAULT 'published' CHECK (status IN ('draft','published')),
    updated_by TEXT,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL REFERENCES kb_documents(id),
    chunk_index INTEGER,
    content TEXT NOT NULL,
    embedding TEXT NOT NULL,            -- JSON: {ngram: weight}，生产换 pgvector
    tokens INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS dishes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    station TEXT NOT NULL,
    standard_minutes INTEGER NOT NULL,
    allergens TEXT DEFAULT '[]',        -- JSON 数组
    version INTEGER DEFAULT 1,
    is_active INTEGER DEFAULT 1,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_no TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('system','paper','voice','takeout')),
    table_no TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'created',
    raw_text TEXT DEFAULT '',
    confidence REAL,
    provider TEXT DEFAULT '',
    created_by TEXT,
    created_at REAL,
    closed_at REAL
);
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    dish_id INTEGER REFERENCES dishes(id),
    dish_name TEXT NOT NULL,
    qty INTEGER DEFAULT 1,
    notes TEXT DEFAULT '',
    allergy_flag INTEGER DEFAULT 0,
    status TEXT DEFAULT 'created'
);
CREATE TABLE IF NOT EXISTS board_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_item_id INTEGER NOT NULL REFERENCES order_items(id),
    order_id INTEGER NOT NULL,
    station TEXT,
    dish_name TEXT,
    score REAL DEFAULT 0,
    reason TEXT DEFAULT '',
    status TEXT DEFAULT 'queued',
    urge_count INTEGER DEFAULT 0,
    urge_at REAL DEFAULT 0,
    snapshot_at REAL
);
CREATE TABLE IF NOT EXISTS repair_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_no TEXT,
    source_event_id INTEGER,
    reason TEXT NOT NULL,
    detail TEXT DEFAULT '',
    assigned_to TEXT DEFAULT '值班经理',
    deadline_at REAL,
    resolved_at REAL,
    resolved_by TEXT DEFAULT '',
    status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    biz_no TEXT,
    event_type TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    actor_id TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_events_biz ON events (biz_no);
CREATE INDEX IF NOT EXISTS idx_tasks_station ON board_tasks (station, status);
"""

SEED_DISHES = [
    # 名称, 档口, 标准制作分钟, 过敏原
    ("红烧肉", "烧腊档", 25, ["猪肉"]),
    ("清蒸鲈鱼", "海鲜档", 18, ["鱼", "海鲜"]),
    ("麻婆豆腐", "川湘档", 10, ["豆制品"]),
    ("宫保鸡丁", "川湘档", 15, ["花生", "鸡肉"]),
    ("蒜蓉西兰花", "蔬菜档", 8, []),
    ("番茄蛋汤", "汤档", 6, ["蛋"]),
]

SEED_DOCS = [
    ("工艺卡总表 v1", "工艺卡", (
        "红烧肉：烧腊档，标准制作时间25分钟。五花肉焯水去腥，冰糖炒糖色，"
        "小火焖煮20分钟后大火收汁。成品色泽红亮，肥而不腻。\n"
        "清蒸鲈鱼：海鲜档，标准制作时间18分钟。鱼身打花刀，旺火蒸8分钟，"
        "淋蒸鱼豉油，热油激香葱丝。过敏原提示：鱼类、海鲜。\n"
        "麻婆豆腐：川湘档，标准制作时间10分钟。豆腐焯水定型，牛肉末炒香，"
        "豆瓣酱炒出红油，勾芡两次，撒花椒面。\n"
        "宫保鸡丁：川湘档，标准制作时间15分钟。鸡丁上浆滑油，干辣椒炝锅，"
        "兑碗汁（糖醋比例1:1），最后加熟花生米。过敏原提示：含花生，花生过敏客人严禁下单。\n"
        "蒜蓉西兰花：蔬菜档，标准制作时间8分钟。西兰花焯水30秒，蒜蓉旺火快炒。\n"
        "番茄蛋汤：汤档，标准制作时间6分钟。番茄炒出沙，开水冲汤，淋蛋液成花。"
    )),
    ("食安与过敏处理制度 v1", "食安制度", (
        "第一条：客人过敏备注属于不可逆风险项，必须随订单逐项传递到对应档口工位。\n"
        "第二条：系统检测到过敏原与菜品冲突时，执行 BLOCK 级拦截，整单暂停，"
        "转食品安全主管签核，未签核不得开工。\n"
        "第三条：过敏备注不冲突时，该菜品按 P0 置顶处理，优先出品。\n"
        "第四条：纸质单与系统单到达后必须统一归一排序，不允许口头插单。\n"
        "第五条：催菜事件同单5分钟内去重，每次催菜菜品优先级加权。\n"
        "第六条：所有写操作（拦截、签核、改派）必须审计留痕，事后可查。"
    )),
]

SEED_USERS = [
    # 用户名, 口令, 角色, 档口
    ("admin", "admin123", "admin", ""),
    ("qianting", "123456", "employee", "前厅"),
    ("chushi", "123456", "employee", "川湘档"),
]


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = get_conn()
    conn.executescript(SCHEMA)
    _seed(conn)
    conn.commit()
    conn.close()


def _seed(conn: sqlite3.Connection) -> None:
    now = time.time()
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        from ..core import security
        for username, pw, role, station in SEED_USERS:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, station, created_at) VALUES (?,?,?,?,?)",
                (username, security.hash_password(pw), role, station, now),
            )
    if conn.execute("SELECT COUNT(*) c FROM dishes").fetchone()["c"] == 0:
        for name, station, minutes, allergens in SEED_DISHES:
            conn.execute(
                "INSERT INTO dishes (name, station, standard_minutes, allergens, created_at) VALUES (?,?,?,?,?)",
                (name, station, minutes, json.dumps(allergens, ensure_ascii=False), now),
            )
    if conn.execute("SELECT COUNT(*) c FROM kb_documents").fetchone()["c"] == 0:
        for title, doc_type, content in SEED_DOCS:
            cur = conn.execute(
                "INSERT INTO kb_documents (title, doc_type, status, updated_by, updated_at) VALUES (?,?,?,?,?)",
                (title, doc_type, "draft", "seed", now),
            )
            from ..kb import ingest
            ingest.chunk_and_embed(conn, cur.lastrowid, content)
            conn.execute("UPDATE kb_documents SET status='published' WHERE id=?", (cur.lastrowid,))
    conn.commit()
