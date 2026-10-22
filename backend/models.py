"""SQLite store for runs, personas, calls, scores and patches.

Everything runs on one event loop in one process, so a single connection is enough.
"""

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,                 -- pending | running | completed
    target_prompt_version TEXT NOT NULL,
    call_cap INTEGER NOT NULL,
    parent_run_id TEXT REFERENCES runs(id),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS personas (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    language_mix TEXT NOT NULL,
    behavior_prompt TEXT NOT NULL,
    success_criteria TEXT NOT NULL        -- JSON
);

CREATE TABLE IF NOT EXISTS calls (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    persona_id TEXT NOT NULL REFERENCES personas(id),
    status TEXT NOT NULL,                 -- queued | dialing | in_progress | completed | failed
    bolna_call_id TEXT UNIQUE,
    started_at TEXT,
    ended_at TEXT,
    transcript TEXT,                      -- JSON list of {speaker, text}
    audio_url TEXT,
    first_response_ms REAL,
    duration_s REAL,
    tool_payload TEXT,                    -- JSON: what the target agent extracted / sent to its tool
    error TEXT,
    target_call_sid TEXT,                 -- set once linked to the target's side (real mode)
    target_transcript TEXT                -- JSON: the same call as the target transcribed it
);
CREATE INDEX IF NOT EXISTS idx_calls_run ON calls(run_id);

CREATE TABLE IF NOT EXISTS scores (
    call_id TEXT NOT NULL REFERENCES calls(id),
    metric TEXT NOT NULL,
    value REAL,
    passed INTEGER NOT NULL,
    reason TEXT,
    span TEXT,                            -- JSON: flagged transcript span, if any
    PRIMARY KEY (call_id, metric)
);

-- The target agent's side of a call (its inbound execution and tool call), keyed by the
-- telephony call_sid. Linked to the caller-side call after both finish.
CREATE TABLE IF NOT EXISTS target_legs (
    call_sid TEXT PRIMARY KEY,
    execution_id TEXT UNIQUE,
    created_at TEXT,
    status TEXT,
    transcript TEXT,                      -- JSON list of {speaker, text}, as the target heard it
    first_response_ms REAL,
    tool_payload TEXT,                    -- JSON: what the target sent to schedule_payment
    linked_call_id TEXT REFERENCES calls(id)
);

-- Hand labels from the spot-check, to measure how often a human agrees with the judge.
CREATE TABLE IF NOT EXISTS reviews (
    call_id TEXT NOT NULL REFERENCES calls(id),
    metric TEXT NOT NULL,
    human_passed INTEGER NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (call_id, metric)
);

CREATE TABLE IF NOT EXISTS patches (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    failure_cluster TEXT NOT NULL,        -- JSON
    proposed_diff TEXT NOT NULL,
    applied_run_id TEXT REFERENCES runs(id),
    created_at TEXT NOT NULL
);
"""

TERMINAL_STATUSES = {"completed", "failed"}
JSON_FIELDS = {"transcript", "tool_payload", "success_criteria", "failure_cluster", "span", "target_transcript",
               "changes"}

# Columns added after the first schema; ALTERed into older database files.
MIGRATIONS = [
    ("calls", "target_call_sid", "TEXT"),
    ("calls", "target_transcript", "TEXT"),
    ("patches", "base_version", "TEXT"),
    ("patches", "new_prompt", "TEXT"),
    ("patches", "summary", "TEXT"),
    ("patches", "changes", "TEXT"),  # JSON list of {metric, change}
    ("patches", "source", "TEXT"),  # claude | rules
    ("patches", "applied_version", "TEXT"),
    ("runs", "mode", "TEXT"),  # mock | real, at creation time
    ("runs", "seeded", "INTEGER NOT NULL DEFAULT 0"),  # 1 = imported example run
    ("calls", "attempts", "INTEGER NOT NULL DEFAULT 1"),
    ("scores", "judge", "TEXT"),  # code | heuristic | claude | heuristic fallback
]
# Tables in dependency order, for exporting and importing example runs.
EXPORT_TABLES = ["runs", "calls", "scores", "patches", "reviews"]


def now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_FIELDS & d.keys():
        if d[k] is not None:
            d[k] = json.loads(d[k])
    return d


class Store:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        for table, column, kind in MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # personas
    def upsert_persona(self, p: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO personas (id, name, language_mix, behavior_prompt, success_criteria)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, language_mix=excluded.language_mix,
                 behavior_prompt=excluded.behavior_prompt, success_criteria=excluded.success_criteria""",
            (p["id"], p["name"], p["language_mix"], p["behavior_prompt"], json.dumps(p["success_criteria"])),
        )
        self.conn.commit()

    # runs
    def create_run(self, call_cap: int, target_prompt_version: str, parent_run_id: str | None = None,
                   mode: str = "mock") -> dict:
        run_id = new_id()
        self.conn.execute(
            "INSERT INTO runs (id, created_at, status, target_prompt_version, call_cap, parent_run_id, mode)"
            " VALUES (?, ?, 'pending', ?, ?, ?, ?)",
            (run_id, now(), target_prompt_version, call_cap, parent_run_id, mode),
        )
        self.conn.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict | None:
        return _row(self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    def list_runs(self) -> list[dict]:
        return [_row(r) for r in self.conn.execute("SELECT * FROM runs ORDER BY created_at DESC")]

    def set_run_status(self, run_id: str, status: str) -> None:
        completed_at = now() if status == "completed" else None
        self.conn.execute(
            "UPDATE runs SET status = ?, completed_at = COALESCE(?, completed_at) WHERE id = ?",
            (status, completed_at, run_id),
        )
        self.conn.commit()

    # calls
    def create_call(self, run_id: str, persona_id: str) -> dict:
        call_id = new_id()
        self.conn.execute(
            "INSERT INTO calls (id, run_id, persona_id, status) VALUES (?, ?, ?, 'queued')",
            (call_id, run_id, persona_id),
        )
        self.conn.commit()
        return self.get_call(call_id)

    def get_call(self, call_id: str) -> dict | None:
        return _row(self.conn.execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone())

    def get_call_by_bolna_id(self, bolna_call_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM calls WHERE bolna_call_id = ?", (bolna_call_id,)).fetchone()
        return _row(row)

    def list_calls(self, run_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM calls WHERE run_id = ? ORDER BY rowid", (run_id,))
        return [_row(r) for r in rows]

    def update_call(self, call_id: str, **fields: Any) -> None:
        if not fields:
            return
        for k in JSON_FIELDS & fields.keys():
            if fields[k] is not None:
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE calls SET {cols} WHERE id = ?", (*fields.values(), call_id))
        self.conn.commit()

    def finish_call_if_open(self, call_id: str, **fields: Any) -> bool:
        """Move a call to a terminal status unless it already has one. Returns True if it changed."""
        row = self.conn.execute("SELECT status FROM calls WHERE id = ?", (call_id,)).fetchone()
        if row is None or row["status"] in TERMINAL_STATUSES:
            return False
        self.update_call(call_id, ended_at=now(), **fields)
        return True

    def reset_call_for_retry(self, call_id: str) -> None:
        """Clear a failed call's results so it can be dialled again inside the same run."""
        self.conn.execute("DELETE FROM scores WHERE call_id = ?", (call_id,))
        self.conn.execute("DELETE FROM reviews WHERE call_id = ?", (call_id,))
        self.conn.execute(
            """UPDATE calls SET status = 'queued', bolna_call_id = NULL, started_at = NULL, ended_at = NULL,
                 transcript = NULL, audio_url = NULL, first_response_ms = NULL, duration_s = NULL,
                 tool_payload = NULL, error = NULL, target_call_sid = NULL, target_transcript = NULL,
                 attempts = attempts + 1
               WHERE id = ?""",
            (call_id,),
        )
        self.conn.commit()

    # target legs
    def upsert_target_leg(self, call_sid: str, **fields: Any) -> None:
        """Insert or merge one target-side record. Fields set to None never overwrite data."""
        fields = {k: v for k, v in fields.items() if v is not None}
        for k in JSON_FIELDS & fields.keys():
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
        self.conn.execute("INSERT OR IGNORE INTO target_legs (call_sid) VALUES (?)", (call_sid,))
        if fields:
            cols = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(f"UPDATE target_legs SET {cols} WHERE call_sid = ?", (*fields.values(), call_sid))
        self.conn.commit()

    def get_target_leg(self, call_sid: str) -> dict | None:
        return _row(self.conn.execute("SELECT * FROM target_legs WHERE call_sid = ?", (call_sid,)).fetchone())

    def unlinked_target_legs(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM target_legs WHERE linked_call_id IS NULL AND status = 'completed'"
        )
        return [_row(r) for r in rows]

    def link_target_leg(self, call_sid: str, call_id: str) -> bool:
        cur = self.conn.execute(
            "UPDATE target_legs SET linked_call_id = ? WHERE call_sid = ? AND linked_call_id IS NULL",
            (call_id, call_sid),
        )
        self.conn.commit()
        return cur.rowcount == 1

    # patches
    def save_patch(self, patch: dict[str, Any]) -> None:
        fields = dict(patch)
        for k in JSON_FIELDS & fields.keys():
            fields[k] = json.dumps(fields[k], ensure_ascii=False)
        cols = ", ".join(fields)
        self.conn.execute(f"INSERT INTO patches ({cols}) VALUES ({', '.join('?' * len(fields))})",
                          tuple(fields.values()))
        self.conn.commit()

    def get_patch(self, patch_id: str) -> dict | None:
        return _row(self.conn.execute("SELECT * FROM patches WHERE id = ?", (patch_id,)).fetchone())

    def patches_for_run(self, run_id: str) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM patches WHERE run_id = ? ORDER BY created_at DESC", (run_id,))
        return [_row(r) for r in rows]

    def patch_applied_in(self, run_id: str) -> dict | None:
        return _row(self.conn.execute("SELECT * FROM patches WHERE applied_run_id = ?", (run_id,)).fetchone())

    def mark_patch_applied(self, patch_id: str, run_id: str, version: str) -> None:
        self.conn.execute("UPDATE patches SET applied_run_id = ?, applied_version = ? WHERE id = ?",
                          (run_id, version, patch_id))
        self.conn.commit()

    # scores
    def save_score(self, call_id: str, metric: str, value: float | None, passed: bool,
                   reason: str = "", span: Any = None, judge: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO scores (call_id, metric, value, passed, reason, span, judge) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(call_id, metric) DO UPDATE SET value=excluded.value, passed=excluded.passed,
                 reason=excluded.reason, span=excluded.span, judge=excluded.judge""",
            (call_id, metric, value, int(passed), reason, json.dumps(span) if span is not None else None, judge),
        )
        self.conn.commit()

    def scores_for_run(self, run_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT s.* FROM scores s JOIN calls c ON c.id = s.call_id WHERE c.run_id = ?", (run_id,)
        )
        return [_row(r) for r in rows]

    # spot-check reviews
    def save_review(self, call_id: str, metric: str, human_passed: bool, note: str = "") -> None:
        self.conn.execute(
            """INSERT INTO reviews (call_id, metric, human_passed, note, created_at) VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(call_id, metric) DO UPDATE SET human_passed=excluded.human_passed,
                 note=excluded.note, created_at=excluded.created_at""",
            (call_id, metric, int(human_passed), note, now()),
        )
        self.conn.commit()

    def reviews_for_run(self, run_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT r.* FROM reviews r JOIN calls c ON c.id = r.call_id WHERE c.run_id = ?", (run_id,)
        )
        return [_row(r) for r in rows]

    # example runs
    def export_runs(self, run_ids: list[str]) -> dict[str, list[dict]]:
        """Raw rows (JSON columns left as stored text) for these runs and everything under them."""
        marks = ",".join("?" * len(run_ids))
        queries = {
            "runs": f"SELECT * FROM runs WHERE id IN ({marks})",
            "calls": f"SELECT * FROM calls WHERE run_id IN ({marks})",
            "scores": f"SELECT s.* FROM scores s JOIN calls c ON c.id = s.call_id WHERE c.run_id IN ({marks})",
            "patches": f"SELECT * FROM patches WHERE run_id IN ({marks})",
            "reviews": f"SELECT r.* FROM reviews r JOIN calls c ON c.id = r.call_id WHERE c.run_id IN ({marks})",
        }
        return {t: [dict(r) for r in self.conn.execute(q, run_ids)] for t, q in queries.items()}

    def import_rows(self, tables: dict[str, list[dict]]) -> int:
        """Insert exported rows, skipping any that already exist. Returns the number of runs added."""
        before = self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        try:
            for table in EXPORT_TABLES:
                known = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for row in tables.get(table, []):
                    row = {k: v for k, v in row.items() if k in known}
                    cols = ", ".join(row)
                    self.conn.execute(
                        f"INSERT OR IGNORE INTO {table} ({cols}) VALUES ({', '.join('?' * len(row))})",
                        tuple(row.values()))
        except Exception:
            self.conn.rollback()  # all or nothing: never leave half an example run behind
            raise
        self.conn.commit()
        return self.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] - before

    def child_runs(self, run_id: str) -> list[dict]:
        return [_row(r) for r in self.conn.execute("SELECT * FROM runs WHERE parent_run_id = ?", (run_id,))]
