"""Majima 的本地数据库：一个 SQLite 文件，各功能用各自的表（只存在本机，不进 git 仓库）

位置：~/Library/Application Support/MajimaVpet/majima.db
  - 笔记本：notes / note_labels / notes_fts（见 notes_store.py）
  - 番茄钟：之后放在自己的表里（pomodoro_store.py）
旧版本的文件叫 notes.db，第一次打开时会自动改名为 majima.db，内容不变。
"""
import os
import sqlite3

DATA_DIR = os.path.expanduser('~/Library/Application Support/MajimaVpet')
DB_PATH = os.path.join(DATA_DIR, 'majima.db')
OLD_DB_PATH = os.path.join(DATA_DIR, 'notes.db')

def migrate_old_file(old=OLD_DB_PATH, new=DB_PATH):
    """把旧的 notes.db（连同 SQLite 的附属文件）改名为 majima.db；新文件已存在时不动"""
    if not os.path.exists(old) or os.path.exists(new):
        return False
    for suffix in ('', '-wal', '-shm', '-journal'):
        if os.path.exists(old + suffix):
            os.rename(old + suffix, new + suffix)
    return True

def open_db(path=DB_PATH):
    """打开共享数据库（默认位置会先迁移旧文件）"""
    if path == DB_PATH:
        migrate_old_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('PRAGMA foreign_keys = ON')
    db.execute('PRAGMA secure_delete = ON')  # 永久删除的数据会在文件里被覆盖，不会残留
    return db
