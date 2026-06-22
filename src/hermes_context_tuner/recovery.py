from __future__ import annotations
import hashlib, json, sqlite3, time, uuid
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

def message_fingerprint(message: dict[str, Any]) -> str:
    canonical = json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()

class RecoveryPointerStore:
    """Versioned lineage metadata only; message content is never persisted."""
    def __init__(self, path: str | Path):
        self.path=Path(path); self.path.parent.mkdir(parents=True, exist_ok=True); self._init_schema()
    def _connect(self):
        db=sqlite3.connect(str(self.path)); db.row_factory=sqlite3.Row; return db
    def _columns(self, db, table): return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    def _init_schema(self):
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS schema_info(version INTEGER NOT NULL)")
            if not db.execute("SELECT 1 FROM schema_info").fetchone(): db.execute("INSERT INTO schema_info VALUES(?)",(SCHEMA_VERSION,))
            db.execute("CREATE TABLE IF NOT EXISTS compression_events (id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT UNIQUE, status TEXT NOT NULL DEFAULT 'finalized', created_at REAL NOT NULL, finalized_at REAL, old_session_id TEXT, new_session_id TEXT, original_count INTEGER NOT NULL, compressed_count INTEGER, total_tokens INTEGER NOT NULL, summary TEXT NOT NULL)")
            cols=self._columns(db,"compression_events")
            for name, ddl in [("operation_id","TEXT"),("status","TEXT NOT NULL DEFAULT 'finalized'"),("finalized_at","REAL")]:
                if name not in cols: db.execute(f"ALTER TABLE compression_events ADD COLUMN {name} {ddl}")
            db.execute("CREATE TABLE IF NOT EXISTS recovery_pointers (id INTEGER PRIMARY KEY AUTOINCREMENT,event_id INTEGER NOT NULL,source_session_id TEXT,range_start INTEGER NOT NULL,range_end INTEGER NOT NULL,fingerprint TEXT NOT NULL,role TEXT NOT NULL,rough_tokens INTEGER NOT NULL,decision TEXT NOT NULL,reason TEXT NOT NULL,FOREIGN KEY(event_id) REFERENCES compression_events(id))")
            db.execute("UPDATE schema_info SET version=?",(SCHEMA_VERSION,))
    def begin(self, *, old_session_id="", original_count:int, total_tokens:int, messages:list[dict[str,Any]], decisions:list[dict[str,Any]]) -> str:
        op=uuid.uuid4().hex; summary=self._summarize_decisions(decisions)
        with self._connect() as db:
            cur=db.execute("INSERT INTO compression_events(operation_id,status,created_at,old_session_id,original_count,total_tokens,summary) VALUES(?,'pending',?,?,?,?,?)",(op,time.time(),old_session_id,original_count,total_tokens,summary)); eid=int(cur.lastrowid)
            rows=[]
            for d in decisions:
                i=int(d.get("index",-1)); msg=messages[i] if 0 <= i < len(messages) else {}
                rows.append((eid,old_session_id,i,i+1,message_fingerprint(msg),str(d.get("role","")),int(d.get("rough_tokens",0)),str(d.get("decision","")),str(d.get("reason",""))))
            db.executemany("INSERT INTO recovery_pointers(event_id,source_session_id,range_start,range_end,fingerprint,role,rough_tokens,decision,reason) VALUES(?,?,?,?,?,?,?,?,?)",rows)
        return op
    def finalize(self, operation_id:str, *, new_session_id:str, compressed_count:int):
        with self._connect() as db: db.execute("UPDATE compression_events SET status='finalized',finalized_at=?,new_session_id=?,compressed_count=? WHERE operation_id=? AND status='pending'",(time.time(),new_session_id,compressed_count,operation_id))
    def record_event(self, *, old_session_id="",new_session_id="",original_count:int,compressed_count:int,total_tokens:int,decisions:list[dict[str,Any]]) -> int:
        op=self.begin(old_session_id=old_session_id,original_count=original_count,total_tokens=total_tokens,messages=[{} for _ in range(original_count)],decisions=decisions); self.finalize(op,new_session_id=new_session_id,compressed_count=compressed_count)
        with self._connect() as db: return int(db.execute("SELECT id FROM compression_events WHERE operation_id=?",(op,)).fetchone()[0])
    def latest_events(self,limit=5):
        with self._connect() as db: rows=db.execute("SELECT id,operation_id,status,created_at,finalized_at,old_session_id,new_session_id,original_count,compressed_count,total_tokens,summary FROM compression_events ORDER BY id DESC LIMIT ?",(int(limit),)).fetchall()
        return [dict(r) for r in rows]
    def lookup(self, operation_id:str, source_messages:list[dict[str,Any]]):
        with self._connect() as db: rows=db.execute("SELECT p.* FROM recovery_pointers p JOIN compression_events e ON e.id=p.event_id WHERE e.operation_id=? ORDER BY range_start",(operation_id,)).fetchall()
        return [{**dict(r),"available": 0 <= r["range_start"] < len(source_messages) and message_fingerprint(source_messages[r["range_start"]]) == r["fingerprint"]} for r in rows]
    @staticmethod
    def _summarize_decisions(decisions):
        counts={}
        for d in decisions: counts[str(d.get("decision","unknown"))]=counts.get(str(d.get("decision","unknown")),0)+1
        return ", ".join(f"{k}={v}" for k,v in sorted(counts.items())) or "no decisions"
