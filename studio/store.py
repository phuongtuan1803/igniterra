"""SQLite (WAL): tasks, step_runs, gate_decisions, reviews (04 · Lược đồ SQLite).

Nội dung nằm trong thư mục task; bảng chỉ trỏ tới. Giao dịch ngắn, một kết nối mỗi lời gọi.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    series TEXT,
    brief TEXT NOT NULL,
    overrides TEXT NOT NULL DEFAULT '{}',
    plan_rev INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    wait_reason TEXT,
    wait_detail TEXT,
    current_step TEXT,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS step_runs (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    step TEXT NOT NULL,
    scene TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    plan_rev INTEGER NOT NULL,
    output_path TEXT,
    output_sha256 TEXT,
    job_id TEXT,
    note TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (task_id, step, scene, attempt)
);
CREATE TABLE IF NOT EXISTS gate_decisions (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    gate TEXT NOT NULL,
    plan_rev INTEGER NOT NULL,
    subject TEXT,
    render_rev TEXT,
    decision TEXT NOT NULL,
    note TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    render_rev TEXT NOT NULL,
    checklist TEXT NOT NULL,
    accepted INTEGER,
    active_minutes REAL,
    reviewer TEXT NOT NULL,
    note TEXT,
    at TEXT NOT NULL
);
"""

WAIT_REASONS = {"gate:keyframes", "gate:final", "unknown_call", "retries_exhausted", "duration_exceeded", "paused"}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    # ------------------------------------------------------------ tasks

    def next_task_id(self) -> str:
        with self.connect() as db:
            row = db.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
        number = int(row["id"].split("_")[1]) + 1 if row else 1
        return f"t_{number:04d}"

    def create_task(self, task_id: str, brief: dict, series: str | None, overrides: dict) -> None:
        stamp = now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO tasks (id, series, brief, overrides, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, series, json.dumps(brief, ensure_ascii=False), json.dumps(overrides), "pending", stamp, stamp),
            )

    def task(self, task_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(f"không có task {task_id}")
        task = dict(row)
        task["brief"] = json.loads(task["brief"])
        task["overrides"] = json.loads(task["overrides"])
        return task

    def tasks(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id, status, wait_reason, current_step, plan_rev, updated_at FROM tasks ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def claim(self) -> str | None:
        """Nhận một task pending bằng cập nhật có điều kiện."""
        with self.connect() as db:
            rows = db.execute("SELECT id FROM tasks WHERE status='pending' ORDER BY updated_at, id").fetchall()
            for row in rows:
                changed = db.execute(
                    "UPDATE tasks SET status='running', wait_reason=NULL, wait_detail=NULL, updated_at=? "
                    "WHERE id=? AND status='pending'",
                    (now(), row["id"]),
                ).rowcount
                if changed:
                    return row["id"]
        return None

    def claim_one(self, task_id: str) -> bool:
        with self.connect() as db:
            return bool(db.execute(
                "UPDATE tasks SET status='running', wait_reason=NULL, wait_detail=NULL, updated_at=? WHERE id=? AND status='pending'",
                (now(), task_id),
            ).rowcount)

    def requeue_running(self) -> list[str]:
        """Khởi động lại worker: mọi task running về pending."""
        with self.connect() as db:
            rows = db.execute("SELECT id FROM tasks WHERE status='running'").fetchall()
            db.execute("UPDATE tasks SET status='pending', updated_at=? WHERE status='running'", (now(),))
        return [row["id"] for row in rows]

    def set_status(self, task_id: str, status: str, wait_reason: str | None = None, detail: str | None = None,
                   *, expect: tuple[str, ...] | None = None) -> bool:
        if status == "waiting" and wait_reason not in WAIT_REASONS:
            raise ValueError(f"wait_reason lạ: {wait_reason}")
        sql = "UPDATE tasks SET status=?, wait_reason=?, wait_detail=?, pause_requested=0, updated_at=? WHERE id=?"
        args: list = [status, wait_reason, detail, now(), task_id]
        if expect:
            sql += f" AND status IN ({','.join('?' * len(expect))})"
            args.extend(expect)
        with self.connect() as db:
            return bool(db.execute(sql, args).rowcount)

    def update_task(self, task_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        columns = ", ".join(f"{key}=?" for key in fields)
        with self.connect() as db:
            db.execute(f"UPDATE tasks SET {columns} WHERE id=?", (*fields.values(), task_id))

    def request_pause(self, task_id: str) -> bool:
        with self.connect() as db:
            return bool(db.execute("UPDATE tasks SET pause_requested=1 WHERE id=? AND status='running'", (task_id,)).rowcount)

    def pause_requested(self, task_id: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT pause_requested FROM tasks WHERE id=?", (task_id,)).fetchone()
        return bool(row and row["pause_requested"])

    # ------------------------------------------------------------ step_runs

    def runs(self, task_id: str, step: str | None = None, scene: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM step_runs WHERE task_id=?", [task_id]
        if step:
            sql, args = sql + " AND step=?", args + [step]
        if scene:
            sql, args = sql + " AND scene=?", args + [scene]
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + " ORDER BY id", args).fetchall()]

    def latest_run(self, task_id: str, step: str, scene: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM step_runs WHERE task_id=? AND step=? AND scene=? ORDER BY attempt DESC LIMIT 1",
                (task_id, step, scene),
            ).fetchone()
        return dict(row) if row else None

    def done_run(self, task_id: str, step: str, scene: str, input_hash: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM step_runs WHERE task_id=? AND step=? AND scene=? AND input_hash=? AND status='done' "
                "ORDER BY attempt DESC LIMIT 1",
                (task_id, step, scene, input_hash),
            ).fetchone()
        return dict(row) if row else None

    def start_run(self, task_id: str, step: str, scene: str, input_hash: str, plan_rev: int, note: str | None) -> dict:
        with self.connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(attempt), 0) AS a FROM step_runs WHERE task_id=? AND step=? AND scene=?",
                (task_id, step, scene),
            ).fetchone()
            attempt = row["a"] + 1
            cursor = db.execute(
                "INSERT INTO step_runs (task_id, step, scene, attempt, status, input_hash, plan_rev, note, started_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (task_id, step, scene, attempt, "running", input_hash, plan_rev, note, now()),
            )
        return {"id": cursor.lastrowid, "attempt": attempt}

    def finish_run(self, run_id: int, status: str, *, output_path: str | None = None, output_sha256: str | None = None,
                   error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE step_runs SET status=?, output_path=?, output_sha256=?, error=?, finished_at=? WHERE id=?",
                (status, output_path, output_sha256, error, now(), run_id),
            )

    def set_job(self, run_id: int, job_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE step_runs SET job_id=? WHERE id=?", (job_id, run_id))

    def mark_running_runs(self, task_id: str, status: str, error: str) -> int:
        """StepRun còn 'running' khi task được nhận lại: tiến trình trước đã chết."""
        with self.connect() as db:
            return db.execute(
                "UPDATE step_runs SET status=?, error=?, finished_at=? WHERE task_id=? AND status='running'",
                (status, error, now(), task_id),
            ).rowcount

    def run_by_request(self, task_id: str, step: str, scene: str, attempt: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM step_runs WHERE task_id=? AND step=? AND scene=? AND attempt=?",
                (task_id, step, scene, attempt),
            ).fetchone()
        return dict(row) if row else None

    def set_run_status(self, run_id: int, status: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE step_runs SET status=?, error=COALESCE(?, error), finished_at=? WHERE id=?",
                       (status, error, now(), run_id))

    # ------------------------------------------------------------ gate_decisions, reviews

    def decide(self, task_id: str, gate: str, plan_rev: int, decision: str, *, subject: str | None = None,
               render_rev: str | None = None, note: str | None = None) -> int:
        with self.connect() as db:
            return db.execute(
                "INSERT INTO gate_decisions (task_id, gate, plan_rev, subject, render_rev, decision, note, at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (task_id, gate, plan_rev, subject, render_rev, decision, note, now()),
            ).lastrowid

    def decisions(self, task_id: str, gate: str | None = None, subject: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM gate_decisions WHERE task_id=?", [task_id]
        if gate:
            sql, args = sql + " AND gate=?", args + [gate]
        if subject is not None:
            sql, args = sql + " AND subject=?", args + [subject]
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + " ORDER BY id", args).fetchall()]

    def add_review(self, task_id: str, render_rev: str, checklist: dict, accepted: bool, active_minutes: float | None,
                   reviewer: str, note: str) -> int:
        with self.connect() as db:
            return db.execute(
                "INSERT INTO reviews (task_id, render_rev, checklist, accepted, active_minutes, reviewer, note, at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (task_id, render_rev, json.dumps(checklist, ensure_ascii=False), int(accepted), active_minutes,
                 reviewer, note, now()),
            ).lastrowid

    def reviews(self, task_id: str, render_rev: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM reviews WHERE task_id=?", [task_id]
        if render_rev:
            sql, args = sql + " AND render_rev=?", args + [render_rev]
        with self.connect() as db:
            rows = [dict(row) for row in db.execute(sql + " ORDER BY id", args).fetchall()]
        for row in rows:
            row["checklist"] = json.loads(row["checklist"])
        return rows
