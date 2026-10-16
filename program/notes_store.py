"""Majima 的笔记本：用 SQLite 保存记下的单词/笔记（只存在本机，不进 git 仓库）

位置：~/Library/Application Support/MajimaVpet/notes.db
每条笔记自动记录时间，并带标签：kind（如 "word"）+ 文本里的 #hashtag。
全文搜索用 FTS5 的 trigram 分词，中英文都能搜到。
"""
import datetime
import os
import re
import sqlite3

DB_PATH = os.path.expanduser('~/Library/Application Support/MajimaVpet/notes.db')

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL,              -- word / todo ...
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL,              -- 本地时间 ISO 8601（带时区）
    updated_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'kept'  -- kept / archived / deleted
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

def connect(path=DB_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('PRAGMA foreign_keys = ON')
    db.executescript(SCHEMA)
    return db

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

def list_notes(db, query='', label=None):
    """笔记本窗口用：按条件列出笔记，新的在前：[(id, text, created_at, 'label1,label2'), ...]
    query 为空时列出全部；label 为 None 时不按标签过滤。"""
    where, args = ["n.status = 'kept'"], []
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
    sql = f"""SELECT n.id, n.text, n.created_at, group_concat(l.label, ',')
              FROM notes n LEFT JOIN note_labels l ON l.note_id = n.id
              WHERE {' AND '.join(where)}
              GROUP BY n.id ORDER BY n.created_at DESC, n.id DESC"""
    return db.execute(sql, args).fetchall()

def all_labels(db):
    """所有在用的标签（按使用次数从多到少）"""
    sql = """SELECT l.label FROM note_labels l JOIN notes n ON n.id = l.note_id
             WHERE n.status = 'kept' GROUP BY l.label ORDER BY count(*) DESC, l.label"""
    return [r[0] for r in db.execute(sql)]

def delete_note(db, note_id):
    """软删除（标记为 deleted，数据还在数据库里，可以恢复）"""
    with db:
        db.execute("UPDATE notes SET status = 'deleted', updated_at = ? WHERE id = ?", (now(), note_id))
