"""番茄钟历史：在共享数据库（majima_db.py）里记录任务和每个番茄（和笔记本的表分开）

表：
  tasks      每个任务：名字、开始时间、结束时间、状态（active / done / stopped）
  pomodoros  每个专注番茄：属于哪个任务、开始/结束时间、是否完成、第几轮的第几个
"""
import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,                  -- 本地时间 ISO 8601（带时区）
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'active'  -- active / done / stopped
);
CREATE TABLE IF NOT EXISTS pomodoros (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    status      TEXT NOT NULL,                  -- completed（满 25 分钟）/ stopped（中途停止）/ interrupted（Mac 睡眠中断），后两种不算数
    section     INTEGER NOT NULL,               -- 第几轮（每轮 4 个番茄）
    number      INTEGER NOT NULL                -- 这一轮里的第几个（1–4）
);
CREATE INDEX IF NOT EXISTS pomodoros_by_task ON pomodoros(task_id);
CREATE INDEX IF NOT EXISTS pomodoros_by_start ON pomodoros(started_at);
"""

def setup(db):
    """在共享数据库里建好番茄钟用的表（已存在就跳过）"""
    db.executescript(SCHEMA)

def iso(t=None):
    """时间戳（time.time()）转成本地时间 ISO 8601；不传则用现在"""
    d = datetime.datetime.now() if t is None else datetime.datetime.fromtimestamp(t)
    return d.astimezone().isoformat(timespec='seconds')

def start_task(db, name):
    with db:
        return db.execute('INSERT INTO tasks (name, created_at) VALUES (?, ?)', (name.strip(), iso())).lastrowid

def finish_task(db, task_id, status='done'):
    """结束任务：done = 完成，stopped = 中途停止"""
    with db:
        db.execute('UPDATE tasks SET status = ?, finished_at = ? WHERE id = ?', (status, iso(), task_id))

def add_pomodoro(db, task_id, started, ended, status, section, number):
    """记录一个专注番茄（started / ended 是 time.time() 时间戳）"""
    with db:
        db.execute('INSERT INTO pomodoros (task_id, started_at, ended_at, status, section, number) '
                   'VALUES (?, ?, ?, ?, ?, ?)', (task_id, iso(started), iso(ended), status, section, number))

def task_summary(db, task_id):
    """(完成的番茄数, 专注总分钟数)"""
    rows = db.execute("SELECT started_at, ended_at FROM pomodoros WHERE task_id = ? AND status = 'completed'",
                      (task_id,)).fetchall()
    minutes = sum((datetime.datetime.fromisoformat(e) - datetime.datetime.fromisoformat(s)).total_seconds()
                  for s, e in rows) / 60
    return len(rows), round(minutes)
