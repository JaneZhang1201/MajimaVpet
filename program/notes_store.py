"""Majima 的笔记本：在共享数据库（majima_db.py）里保存记下的单词/笔记

表：notes / note_labels / notes_fts（只属于笔记本，番茄钟等其他功能用各自的表）
每条笔记自动记录时间，并带标签：kind（如 "word"）+ 文本里的 #hashtag。
全文搜索用 FTS5 的 trigram 分词，中英文都能搜到。
"""
import datetime
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,              -- word / todo ...
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL,              -- 本地时间 ISO 8601（带时区）
    updated_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'kept', -- kept / deleted（在"最近删除"里）
    deleted_at  TEXT                          -- 放进"最近删除"的时间
);
CREATE TABLE IF NOT EXISTS note_labels (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    label   TEXT NOT NULL,
    PRIMARY KEY (note_id, label)
);
CREATE INDEX IF NOT EXISTS notes_created ON notes(created_at);
CREATE INDEX IF NOT EXISTS labels_by_name ON note_labels(label);

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(text, content='notes', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
    INSERT INTO notes_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE OF text ON notes BEGIN
    INSERT INTO notes_fts(notes_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO notes_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

def setup(db):
    """在共享数据库里建好笔记本用的表（已存在就跳过），并升级旧版本的表"""
    db.executescript(SCHEMA)
    columns = [r[1] for r in db.execute('PRAGMA table_info(notes)')]
    if 'deleted_at' not in columns:  # 旧版本没有 deleted_at：补上，已删除的笔记用 updated_at 作为删除时间
        with db:
            db.execute('ALTER TABLE notes ADD COLUMN deleted_at TEXT')
            db.execute("UPDATE notes SET deleted_at = updated_at WHERE status = 'deleted'")

def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')

def hashtags(text):
    return {t.lower() for t in re.findall(r'#(\w+)', text)}

def add_note(db, text, kind='word'):
    """保存一条笔记，返回 id。标签 = kind + 文本里的 #hashtag。"""
    text = text.strip()
    if not text:
        return None
    t = now()
    with db:
        cur = db.execute('INSERT INTO notes (kind, text, created_at, updated_at) VALUES (?, ?, ?, ?)',
                         (kind, text, t, t))
        db.executemany('INSERT OR IGNORE INTO note_labels (note_id, label) VALUES (?, ?)',
                       [(cur.lastrowid, label) for label in {kind} | hashtags(text)])
    return cur.lastrowid

def recent_notes(db, limit=10, kind=None):
    """最近的笔记，新的在前：[(id, text, created_at, 'label1,label2'), ...]"""
    sql = """SELECT n.id, n.text, n.created_at, group_concat(l.label, ',')
             FROM notes n LEFT JOIN note_labels l ON l.note_id = n.id
             WHERE n.status = 'kept' AND (? IS NULL OR n.kind = ?)
             GROUP BY n.id ORDER BY n.created_at DESC, n.id DESC LIMIT ?"""
    return db.execute(sql, (kind, kind, limit)).fetchall()

def search_notes(db, query, limit=50):
    """全文搜索（至少 3 个字符；更短的用 LIKE 匹配）"""
    if len(query) >= 3:
        sql = """SELECT n.id, n.text, n.created_at FROM notes_fts f JOIN notes n ON n.id = f.rowid
                 WHERE notes_fts MATCH ? AND n.status = 'kept' ORDER BY n.created_at DESC LIMIT ?"""
        return db.execute(sql, ('"' + query.replace('"', '""') + '"', limit)).fetchall()
    sql = """SELECT id, text, created_at FROM notes WHERE text LIKE ? AND status = 'kept'
             ORDER BY created_at DESC LIMIT ?"""
    return db.execute(sql, ('%' + query + '%', limit)).fetchall()

def list_notes(db, query='', label=None, deleted=False):
    """笔记本窗口用：按条件列出笔记，新的在前：[(id, text, created_at, 'label1,label2', deleted_at), ...]
    query 为空时列出全部；label 为 None 时不按标签过滤；deleted=True 时列出"最近删除"里的笔记。"""
    where, args = ["n.status = ?"], ['deleted' if deleted else 'kept']
    query = query.strip()
    if len(query) >= 3:
        where.append('n.id IN (SELECT rowid FROM notes_fts WHERE notes_fts MATCH ?)')
        args.append('"' + query.replace('"', '""') + '"')
    elif query:
        where.append('n.text LIKE ?')
        args.append('%' + query + '%')
    if label:
        where.append('n.id IN (SELECT note_id FROM note_labels WHERE label = ?)')
        args.append(label)
    order = 'n.deleted_at DESC, n.id DESC' if deleted else 'n.created_at DESC, n.id DESC'
    sql = f"""SELECT n.id, n.text, n.created_at, group_concat(l.label, ','), n.deleted_at
              FROM notes n LEFT JOIN note_labels l ON l.note_id = n.id
              WHERE {' AND '.join(where)}
              GROUP BY n.id ORDER BY {order}"""
    return db.execute(sql, args).fetchall()

def all_labels(db):
    """所有在用的标签（按使用次数从多到少）"""
    sql = """SELECT l.label FROM note_labels l JOIN notes n ON n.id = l.note_id
             WHERE n.status = 'kept' GROUP BY l.label ORDER BY count(*) DESC, l.label"""
    return [r[0] for r in db.execute(sql)]

# ---------------- 最近删除（删除缓冲区）----------------
def delete_note(db, note_id):
    """放进"最近删除"：还能恢复，到期后才真正删除"""
    t = now()
    with db:
        db.execute("UPDATE notes SET status = 'deleted', deleted_at = ?, updated_at = ? WHERE id = ?", (t, t, note_id))

def restore_note(db, note_id):
    """从"最近删除"恢复，时间和标签都保持原样"""
    with db:
        db.execute("UPDATE notes SET status = 'kept', deleted_at = NULL, updated_at = ? WHERE id = ?", (now(), note_id))

def delete_forever(db, note_ids):
    """永久删除（只限"最近删除"里的笔记）；标签和搜索索引一起删除，不能恢复"""
    with db:
        db.executemany("DELETE FROM notes WHERE id = ? AND status = 'deleted'", [(i,) for i in note_ids])

def empty_trash(db):
    """清空"最近删除"，返回删除的数量"""
    ids = [r[0] for r in db.execute("SELECT id FROM notes WHERE status = 'deleted'")]
    delete_forever(db, ids)
    return len(ids)

def purge_expired(db, days):
    """把在"最近删除"里放了超过 days 天的笔记永久删除，返回删除的数量"""
    cutoff = datetime.datetime.now().astimezone() - datetime.timedelta(days=days)
    ids = [i for i, t in db.execute("SELECT id, deleted_at FROM notes WHERE status = 'deleted'")
           if t is None or datetime.datetime.fromisoformat(t) < cutoff]
    delete_forever(db, ids)
    return len(ids)

def days_in_trash(deleted_at):
    """已经在"最近删除"里放了几天"""
    return (datetime.datetime.now().astimezone() - datetime.datetime.fromisoformat(deleted_at)).days
