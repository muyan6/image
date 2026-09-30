"""Small persistent mapping for unconsumed COS upload permits."""
import atexit
import json
import sqlite3
import threading
from collections.abc import MutableMapping
from pathlib import Path

_MISSING=object()


class UploadRegistry(MutableMapping):
    def __init__(self,path_factory):
        self.path_factory=path_factory;self.lock=threading.RLock();self.db=None;self.path=None
        atexit.register(self.close)

    def connection(self):
        path=str(self.path_factory())
        if self.db is None or self.path!=path:
            self.close();Path(path).parent.mkdir(parents=True,exist_ok=True)
            self.db=sqlite3.connect(path,check_same_thread=False)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,owner TEXT NOT NULL,created REAL NOT NULL,payload TEXT NOT NULL)')
            self.db.execute('CREATE INDEX IF NOT EXISTS uploads_created ON uploads(created)')
            self.db.execute('CREATE INDEX IF NOT EXISTS uploads_owner ON uploads(owner)')
            self.db.commit();self.path=path
        return self.db

    def close(self):
        with self.lock:
            if self.db is not None:self.db.close()
            self.db=None;self.path=None

    def __getitem__(self,key):
        with self.lock:row=self.connection().execute('SELECT payload FROM uploads WHERE id=?',(key,)).fetchone()
        if row is None:raise KeyError(key)
        return json.loads(row[0])

    def __setitem__(self,key,value):
        payload=json.dumps(value,ensure_ascii=False)
        with self.lock:
            db=self.connection()
            with db:db.execute('INSERT OR REPLACE INTO uploads VALUES(?,?,?,?)',(key,value['openid'],value['created_at'],payload))

    def __delitem__(self,key):
        self.pop(key)

    def pop(self,key,default=_MISSING):
        with self.lock:
            db=self.connection()
            with db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT payload FROM uploads WHERE id=?',(key,)).fetchone()
                if row:db.execute('DELETE FROM uploads WHERE id=?',(key,))
        if row:return json.loads(row[0])
        if default is _MISSING:raise KeyError(key)
        return default

    def __iter__(self):
        with self.lock:keys=[r[0] for r in self.connection().execute('SELECT id FROM uploads')]
        return iter(keys)

    def __len__(self):
        with self.lock:return self.connection().execute('SELECT COUNT(*) FROM uploads').fetchone()[0]

    def clear(self):
        with self.lock:
            db=self.connection()
            with db:db.execute('DELETE FROM uploads')

    def expire(self,cutoff):
        with self.lock:
            db=self.connection()
            with db:db.execute('DELETE FROM uploads WHERE created<?',(cutoff,))

    def count_owner(self,owner):
        with self.lock:return self.connection().execute('SELECT COUNT(*) FROM uploads WHERE owner=?',(owner,)).fetchone()[0]
