"""SQLite (WAL): tasks, step_runs, gate_decisions, reviews (04 · Lược đồ SQLite); gpu_* (09 · Dữ liệu).

Nội dung nằm trong thư mục task; bảng chỉ trỏ tới. Giao dịch ngắn, một kết nối mỗi lời gọi.
Bảng gpu_* tách khỏi bảng core, mốc thời gian theo UTC (báo giá có hạn).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
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
CREATE TABLE IF NOT EXISTS series_jobs (
    id INTEGER PRIMARY KEY,
    series TEXT NOT NULL,
    character TEXT NOT NULL,
    view TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    output_path TEXT,
    error TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
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
CREATE TABLE IF NOT EXISTS gpu_rentals (
    rental_id TEXT PRIMARY KEY,
    provider_key TEXT NOT NULL,
    provider_instance_id TEXT,
    label TEXT NOT NULL UNIQUE,
    offer_json TEXT NOT NULL,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    disk_gb REAL,
    manifest_sha256 TEXT,
    provision_deadline TEXT,
    idle_deadline TEXT,
    destroy_confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gpu_operations (
    op_id TEXT PRIMARY KEY,
    rental_id TEXT NOT NULL REFERENCES gpu_rentals(rental_id),
    action TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    confirmed_at TEXT,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gpu_provision_steps (
    rental_id TEXT NOT NULL REFERENCES gpu_rentals(rental_id),
    step_key TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_code TEXT,
    bytes_done INTEGER,
    bytes_total INTEGER,
    log_ref TEXT,
    PRIMARY KEY (rental_id, step_key)
);
CREATE TABLE IF NOT EXISTS gpu_events (
    id INTEGER PRIMARY KEY,
    rental_id TEXT NOT NULL REFERENCES gpu_rentals(rental_id),
    ts_utc TEXT NOT NULL,
    transition TEXT NOT NULL,
    error_code TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS gpu_manifests (
    manifest_sha256 TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS gpu_cost_samples (
    id INTEGER PRIMARY KEY,
    rental_id TEXT NOT NULL REFERENCES gpu_rentals(rental_id),
    ts TEXT NOT NULL,
    compute_per_h REAL NOT NULL,
    disk_per_h REAL NOT NULL,
    accrued_est REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS comfy_manifests (
    manifest_sha256 TEXT PRIMARY KEY,
    manifest_json TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS comfy_workflows (
    workflow_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    name TEXT NOT NULL,
    format TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_json TEXT NOT NULL,
    api_sha256 TEXT,
    api_json TEXT,
    class_types TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    manifest_sha256 TEXT REFERENCES comfy_manifests(manifest_sha256),
    manifest_source TEXT,
    version INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, revision)
);
CREATE TABLE IF NOT EXISTS comfy_runs (
    run_id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    workflow_sha256 TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    rental_id TEXT NOT NULL REFERENCES gpu_rentals(rental_id),
    prompt_id TEXT UNIQUE,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    detail TEXT,
    queue_state TEXT,
    current_node TEXT,
    last_event TEXT,
    last_event_at TEXT,
    lookup_misses INTEGER NOT NULL DEFAULT 0,
    download_attempts INTEGER NOT NULL DEFAULT 0,
    submitted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (workflow_id, revision) REFERENCES comfy_workflows(workflow_id, revision)
);
CREATE TABLE IF NOT EXISTS comfy_run_outputs (
    run_id TEXT NOT NULL REFERENCES comfy_runs(run_id),
    node TEXT NOT NULL,
    subfolder TEXT NOT NULL,
    filename TEXT NOT NULL,
    folder_type TEXT NOT NULL,
    status TEXT NOT NULL,
    path TEXT,
    bytes INTEGER,
    sha256 TEXT,
    media_type TEXT,
    error TEXT,
    verified_at TEXT,
    PRIMARY KEY (run_id, node, subfolder, filename)
);
CREATE TABLE IF NOT EXISTS comfy_run_events (
    id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES comfy_runs(run_id),
    ts_utc TEXT NOT NULL,
    transition TEXT NOT NULL,
    error_code TEXT,
    detail TEXT
);
"""

# Cột thêm sau khi bảng đã phát hành (CREATE TABLE IF NOT EXISTS không sửa bảng cũ): (bảng, cột, kiểu).
ADDED_COLUMNS = (("gpu_rentals", "disk_gb", "REAL"), ("gpu_rentals", "manifest_sha256", "TEXT"),
                 ("gpu_rentals", "provision_deadline", "TEXT"))

WAIT_REASONS = {"gate:keyframes", "gate:final", "unknown_call", "retries_exhausted", "duration_exceeded", "paused"}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            for table, column, kind in ADDED_COLUMNS:
                if column not in {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

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

    # ------------------------------------------------------------ series_jobs (ảnh tham chiếu, worker chạy)

    def add_series_job(self, series: str, character: str, view: str, note: str | None = None) -> int:
        with self.connect() as db:
            return db.execute(
                "INSERT INTO series_jobs (series, character, view, status, note, created_at) VALUES (?,?,?,?,?,?)",
                (series, character, view, "pending", note, now()),
            ).lastrowid

    def series_jobs(self, series: str | None = None, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM series_jobs WHERE 1=1", []
        if series:
            sql, args = sql + " AND series=?", args + [series]
        if status:
            sql, args = sql + " AND status=?", args + [status]
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + " ORDER BY id", args).fetchall()]

    def claim_series_job(self) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM series_jobs WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row and db.execute("UPDATE series_jobs SET status='running', attempt=attempt+1 WHERE id=? AND status='pending'",
                                  (row["id"],)).rowcount:
                return {**dict(row), "status": "running", "attempt": row["attempt"] + 1}
        return None

    def finish_series_job(self, job_id: int, status: str, output_path: str | None = None, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE series_jobs SET status=?, output_path=?, error=?, finished_at=? WHERE id=?",
                       (status, output_path, error, now(), job_id))

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

    # ------------------------------------------------------------ gpu_* (Gate 4; chỉ studio/gpu/service.py ghi)

    def add_rental(self, rental_id: str, provider_key: str, label: str, offer_json: str, steps: tuple[str, ...],
                   disk_gb: float | None = None, manifest_sha256: str | None = None) -> None:
        """Rental ``draft`` và các bước provisioning ``pending``; rental_id trùng → sqlite3.IntegrityError."""
        stamp = utc_now()
        with self.connect() as db:
            db.execute(
                "INSERT INTO gpu_rentals (rental_id, provider_key, label, offer_json, status, disk_gb, manifest_sha256, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (rental_id, provider_key, label, offer_json, "draft", disk_gb, manifest_sha256, stamp, stamp),
            )
            db.executemany("INSERT INTO gpu_provision_steps (rental_id, step_key, status) VALUES (?,?,?)",
                           [(rental_id, key, "pending") for key in steps])
            db.execute("INSERT INTO gpu_events (rental_id, ts_utc, transition) VALUES (?,?,?)", (rental_id, stamp, "draft"))

    def rental(self, rental_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM gpu_rentals WHERE rental_id=?", (rental_id,)).fetchone()
        if row is None:
            raise KeyError(f"không có rental {rental_id}")
        return dict(row)

    def rentals(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM gpu_rentals ORDER BY created_at DESC, rental_id").fetchall()]

    def transition_rental(self, rental_id: str, version: int, status: str, *, expect: str, error_code: str | None = None,
                          detail: str | None = None, **fields) -> bool:
        """CAS theo (``expect``, ``version``) trong một câu UPDATE; ghi sự kiện cùng giao dịch.

        False: bên khác đã đổi trạng thái trước; người gọi đọc lại, không xử lý tiếp.
        """
        stamp = utc_now()
        columns = "".join(f", {key}=?" for key in fields)
        with self.connect() as db:
            changed = db.execute(
                f"UPDATE gpu_rentals SET status=?, version=version+1, updated_at=?{columns} "
                "WHERE rental_id=? AND status=? AND version=?",
                (status, stamp, *fields.values(), rental_id, expect, version),
            ).rowcount
            if changed:
                db.execute("INSERT INTO gpu_events (rental_id, ts_utc, transition, error_code, detail) VALUES (?,?,?,?,?)",
                           (rental_id, stamp, f"{expect}→{status}", error_code, detail))
        return bool(changed)

    def release_rental(self, rental_id: str, version: int, run_id: str, executing: tuple[str, ...]) -> bool:
        """``busy→ready`` (CAS theo version) chỉ khi không còn run nào khác của rental ở ``executing``; cùng một câu
        UPDATE nên run mới vừa ghi mà chưa giành máy không bị nhả nhầm."""
        stamp = utc_now()
        marks = ",".join("?" * len(executing))
        with self.connect() as db:
            changed = db.execute(
                "UPDATE gpu_rentals SET status='ready', version=version+1, updated_at=? "
                "WHERE rental_id=? AND status='busy' AND version=? AND NOT EXISTS "
                f"(SELECT 1 FROM comfy_runs WHERE rental_id=? AND run_id<>? AND status IN ({marks}))",
                (stamp, rental_id, version, rental_id, run_id, *executing)).rowcount
            if changed:
                db.execute("INSERT INTO gpu_events (rental_id, ts_utc, transition, detail) VALUES (?,?,?,?)",
                           (rental_id, stamp, "busy→ready", f"run {run_id} xong phần chạy trên GPU"))
        return bool(changed)

    def add_gpu_event(self, rental_id: str, transition: str, error_code: str | None = None, detail: str | None = None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO gpu_events (rental_id, ts_utc, transition, error_code, detail) VALUES (?,?,?,?,?)",
                       (rental_id, utc_now(), transition, error_code, detail))

    def gpu_events(self, rental_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM gpu_events WHERE rental_id=? ORDER BY id", (rental_id,)).fetchall()]

    def start_gpu_operation(self, op_id: str, rental_id: str, action: str, request_hash: str) -> None:
        """Ghi intent trước khi gọi API (``sent``); kết quả cập nhật bằng ``finish_gpu_operation``."""
        with self.connect() as db:
            db.execute("INSERT INTO gpu_operations (op_id, rental_id, action, request_hash, sent_at, status) VALUES (?,?,?,?,?,?)",
                       (op_id, rental_id, action, request_hash, utc_now(), "sent"))

    def finish_gpu_operation(self, op_id: str, status: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE gpu_operations SET status=?, confirmed_at=? WHERE op_id=?",
                       (status, utc_now() if status == "confirmed" else None, op_id))

    def gpu_operations(self, rental_id: str, action: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM gpu_operations WHERE rental_id=?", [rental_id]
        if action:
            sql, args = sql + " AND action=?", args + [action]
        with self.connect() as db:
            # Thứ tự ghi thật (rowid): sent_at chỉ tới giây, op_id ngẫu nhiên nên không xếp được hai lượt cùng giây.
            return [dict(row) for row in db.execute(sql + " ORDER BY rowid", args).fetchall()]

    def provision_steps(self, rental_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM gpu_provision_steps WHERE rental_id=? ORDER BY rowid",
                                                    (rental_id,)).fetchall()]

    def set_provision_step(self, rental_id: str, step_key: str, status: str, error_code: str | None = None,
                           only_from: str | None = None, **fields) -> None:
        """``running`` tăng lượt thử và ghi started_at; trạng thái kết thúc ghi finished_at.

        ``fields``: ``attempt`` (lượt thử provisioner báo, thay cho tự tăng), ``bytes_done``, ``bytes_total``, ``log_ref``.
        ``only_from``: chỉ đổi khi trạng thái hiện tại là giá trị này (CAS trong cùng câu UPDATE).
        """
        unknown = set(fields) - {"attempt", "bytes_done", "bytes_total", "log_ref"}
        if unknown:
            raise ValueError(f"cột lạ: {sorted(unknown)}")
        stamp = utc_now()
        extra = "".join(f", {key}=?" for key in fields)
        guard, guard_args = (" AND status=?", (only_from,)) if only_from else ("", ())
        with self.connect() as db:
            if status == "running":
                bump = "" if "attempt" in fields else ", attempt=attempt+1"
                db.execute(f"UPDATE gpu_provision_steps SET status=?{bump}, started_at=?, finished_at=NULL, "
                           f"error_code=NULL{extra} WHERE rental_id=? AND step_key=?{guard}",
                           (status, stamp, *fields.values(), rental_id, step_key, *guard_args))
            else:
                db.execute(f"UPDATE gpu_provision_steps SET status=?, finished_at=?, error_code=?{extra} "
                           f"WHERE rental_id=? AND step_key=?{guard}",
                           (status, stamp if status != "pending" else None, error_code, *fields.values(), rental_id,
                            step_key, *guard_args))

    def update_provision_step(self, rental_id: str, step_key: str, **fields) -> None:
        """Đổi lượt thử/byte/log của bước đang chạy mà không đổi trạng thái, started_at."""
        unknown = set(fields) - {"attempt", "bytes_done", "bytes_total", "log_ref"}
        if unknown or not fields:
            raise ValueError(f"cột lạ: {sorted(unknown)}")
        with self.connect() as db:
            db.execute(f"UPDATE gpu_provision_steps SET {', '.join(f'{k}=?' for k in fields)} "
                       "WHERE rental_id=? AND step_key=?", (*fields.values(), rental_id, step_key))

    def add_manifest(self, manifest_sha256: str, manifest_json: str, note: str | None = None) -> bool:
        """Thêm manifest ``pending_review``; đã có (cùng SHA-256) thì giữ nguyên, trả False."""
        with self.connect() as db:
            return bool(db.execute("INSERT OR IGNORE INTO gpu_manifests (manifest_sha256, manifest_json, status, note, "
                                   "created_at) VALUES (?,?,?,?,?)",
                                   (manifest_sha256, manifest_json, "pending_review", note, utc_now())).rowcount)

    def set_manifest_status(self, manifest_sha256: str, status: str, *, expect: str) -> bool:
        with self.connect() as db:
            return bool(db.execute("UPDATE gpu_manifests SET status=?, approved_at=? WHERE manifest_sha256=? AND status=?",
                                   (status, utc_now() if status == "approved" else None, manifest_sha256,
                                    expect)).rowcount)

    def manifest(self, manifest_sha256: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM gpu_manifests WHERE manifest_sha256=?", (manifest_sha256,)).fetchone()
        if row is None:
            raise KeyError(f"không có manifest {manifest_sha256}")
        return dict(row)

    def manifests(self) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM gpu_manifests ORDER BY created_at DESC, rowid DESC")]

    def add_cost_sample(self, rental_id: str, ts: str, compute_per_h: float, disk_per_h: float,
                        accrued_est: float) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO gpu_cost_samples (rental_id, ts, compute_per_h, disk_per_h, accrued_est) "
                       "VALUES (?,?,?,?,?)", (rental_id, ts, compute_per_h, disk_per_h, accrued_est))

    def cost_samples(self, rental_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM gpu_cost_samples WHERE rental_id=? ORDER BY id",
                                                    (rental_id,)).fetchall()]

    # ------------------------------------------------------------ comfy_* (Gate 4 lát D; chỉ studio/comfyui/service.py ghi)

    def add_comfy_manifest(self, manifest_sha256: str, manifest_json: str, note: str | None = None) -> bool:
        """Thêm manifest ``pending_review``; đã có (cùng SHA-256) thì giữ nguyên, trả False."""
        with self.connect() as db:
            return bool(db.execute("INSERT OR IGNORE INTO comfy_manifests (manifest_sha256, manifest_json, status, note, "
                                   "created_at) VALUES (?,?,?,?,?)",
                                   (manifest_sha256, manifest_json, "pending_review", note, utc_now())).rowcount)

    def set_comfy_manifest_status(self, manifest_sha256: str, status: str, *, expect: str) -> bool:
        with self.connect() as db:
            return bool(db.execute("UPDATE comfy_manifests SET status=?, approved_at=? WHERE manifest_sha256=? AND status=?",
                                   (status, utc_now() if status == "approved" else None, manifest_sha256,
                                    expect)).rowcount)

    def comfy_manifest(self, manifest_sha256: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM comfy_manifests WHERE manifest_sha256=?", (manifest_sha256,)).fetchone()
        if row is None:
            raise KeyError(f"không có manifest workflow {manifest_sha256}")
        return dict(row)

    _REVISION = ("SELECT w.*, m.status AS manifest_status FROM comfy_workflows w "
                 "LEFT JOIN comfy_manifests m ON m.manifest_sha256 = w.manifest_sha256")

    def add_workflow_revision(self, workflow_id: str, name: str, fmt: str, source_sha256: str, source_json: str,
                              api_sha256: str | None, api_json: str | None, class_types: str, draft_json: str,
                              note: str | None = None) -> int:
        """Revision mới = lớn nhất + 1, tính trong cùng câu INSERT (hai lượt nhập song song không trùng số)."""
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO comfy_workflows (workflow_id, revision, name, format, source_sha256, source_json, api_sha256, "
                "api_json, class_types, draft_json, note, created_at) "
                "SELECT ?, COALESCE(MAX(revision), 0) + 1, ?,?,?,?,?,?,?,?,?,? FROM comfy_workflows WHERE workflow_id=?",
                (workflow_id, name, fmt, source_sha256, source_json, api_sha256, api_json, class_types, draft_json, note,
                 utc_now(), workflow_id))
            return db.execute("SELECT revision FROM comfy_workflows WHERE rowid=?", (cursor.lastrowid,)).fetchone()[0]

    def workflow_revision(self, workflow_id: str, revision: int | None = None) -> dict:
        sql, args = self._REVISION + " WHERE w.workflow_id=?", [workflow_id]
        if revision is not None:
            sql, args = sql + " AND w.revision=?", args + [revision]
        with self.connect() as db:
            row = db.execute(sql + " ORDER BY w.revision DESC LIMIT 1", args).fetchone()
        if row is None:
            raise KeyError(f"không có workflow {workflow_id}" + (f" revision {revision}" if revision else ""))
        return dict(row)

    def workflow_revisions(self, workflow_id: str | None = None) -> list[dict]:
        sql, args = self._REVISION, []
        if workflow_id:
            sql, args = sql + " WHERE w.workflow_id=?", [workflow_id]
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + " ORDER BY w.created_at DESC, w.workflow_id, w.revision DESC",
                                                    args).fetchall()]

    def attach_comfy_manifest(self, workflow_id: str, revision: int, version: int, manifest_sha256: str,
                              source: str) -> bool:
        """Gắn manifest vào revision (CAS theo version). Revision đã gắn manifest đã duyệt thì không đổi được nữa:
        điều kiện nằm trong cùng câu UPDATE, không đọc rồi ghi."""
        with self.connect() as db:
            return bool(db.execute(
                "UPDATE comfy_workflows SET manifest_sha256=?, manifest_source=?, version=version+1 "
                "WHERE workflow_id=? AND revision=? AND version=? AND (manifest_sha256 IS NULL OR manifest_sha256 NOT IN "
                "(SELECT manifest_sha256 FROM comfy_manifests WHERE status='approved'))",
                (manifest_sha256, source, workflow_id, revision, version)).rowcount)

    def add_comfy_run(self, run_id: str, workflow_id: str, revision: int, workflow_sha256: str, manifest_sha256: str,
                      rental_id: str) -> None:
        """Run ``checking``; run_id trùng → sqlite3.IntegrityError."""
        stamp = utc_now()
        with self.connect() as db:
            db.execute("INSERT INTO comfy_runs (run_id, workflow_id, revision, workflow_sha256, manifest_sha256, rental_id, "
                       "status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (run_id, workflow_id, revision, workflow_sha256, manifest_sha256, rental_id, "checking", stamp,
                        stamp))
            db.execute("INSERT INTO comfy_run_events (run_id, ts_utc, transition) VALUES (?,?,?)",
                       (run_id, stamp, "checking"))

    def comfy_run(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM comfy_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"không có run {run_id}")
        return dict(row)

    def comfy_runs(self, *, workflow_id: str | None = None, rental_id: str | None = None,
                   statuses: tuple[str, ...] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM comfy_runs WHERE 1=1", []
        if workflow_id:
            sql, args = sql + " AND workflow_id=?", args + [workflow_id]
        if rental_id:
            sql, args = sql + " AND rental_id=?", args + [rental_id]
        if statuses:
            sql, args = sql + f" AND status IN ({','.join('?' * len(statuses))})", args + list(statuses)
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql + " ORDER BY created_at DESC, rowid DESC", args).fetchall()]

    RUN_FIELDS = frozenset({"prompt_id", "queue_state", "current_node", "last_event", "last_event_at",
                            "lookup_misses", "download_attempts", "submitted_at", "finished_at"})

    def transition_comfy_run(self, run_id: str, version: int, status: str, *, expect: str, error_code: str | None = None,
                             detail: str | None = None, **fields) -> bool:
        """CAS theo (``expect``, ``version``) trong một câu UPDATE; đổi trạng thái thì ghi sự kiện cùng giao dịch.

        Cùng trạng thái (``expect == status``) là cập nhật tiến độ; vẫn tăng version nên lượt đọc cũ thua CAS.
        """
        unknown = set(fields) - self.RUN_FIELDS
        if unknown:
            raise ValueError(f"cột lạ: {sorted(unknown)}")
        stamp = utc_now()
        if status != expect:
            fields = {"error_code": error_code, "detail": detail, **fields}
        columns = "".join(f", {key}=?" for key in fields)
        with self.connect() as db:
            changed = db.execute(
                f"UPDATE comfy_runs SET status=?, version=version+1, updated_at=?{columns} "
                "WHERE run_id=? AND status=? AND version=?",
                (status, stamp, *fields.values(), run_id, expect, version),
            ).rowcount
            if changed and status != expect:
                db.execute("INSERT INTO comfy_run_events (run_id, ts_utc, transition, error_code, detail) VALUES (?,?,?,?,?)",
                           (run_id, stamp, f"{expect}→{status}", error_code, detail))
        return bool(changed)

    def bump_download_attempts(self, run_id: str) -> int:
        """Tăng lượt tải trong SQL (hai lượt poll song song không mất lượt đếm); trả số mới."""
        with self.connect() as db:
            db.execute("UPDATE comfy_runs SET download_attempts=download_attempts+1 WHERE run_id=?", (run_id,))
            return db.execute("SELECT download_attempts FROM comfy_runs WHERE run_id=?", (run_id,)).fetchone()[0]

    def abandon_comfy_runs(self, rental_id: str, statuses: tuple[str, ...], detail: str) -> list[str]:
        """Mọi run chưa xong của rental → ``abandoned`` trong một giao dịch (destroy đã xác nhận bỏ output)."""
        stamp = utc_now()
        marks = ",".join("?" * len(statuses))
        abandoned = []
        with self.connect() as db:
            rows = db.execute(f"SELECT run_id, status FROM comfy_runs WHERE rental_id=? AND status IN ({marks})",
                              (rental_id, *statuses)).fetchall()
            for row in rows:
                if db.execute("UPDATE comfy_runs SET status='abandoned', version=version+1, updated_at=?, finished_at=?, "
                              "error_code='destroy_accepted', detail=? WHERE run_id=? AND status=?",
                              (stamp, stamp, detail, row["run_id"], row["status"])).rowcount:
                    db.execute("INSERT INTO comfy_run_events (run_id, ts_utc, transition, error_code, detail) "
                               "VALUES (?,?,?,?,?)",
                               (row["run_id"], stamp, f"{row['status']}→abandoned", "destroy_accepted", detail))
                    abandoned.append(row["run_id"])
        return abandoned

    def add_comfy_event(self, run_id: str, transition: str, error_code: str | None = None,
                        detail: str | None = None) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO comfy_run_events (run_id, ts_utc, transition, error_code, detail) VALUES (?,?,?,?,?)",
                       (run_id, utc_now(), transition, error_code, detail))

    def comfy_events(self, run_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM comfy_run_events WHERE run_id=? ORDER BY id",
                                                    (run_id,)).fetchall()]

    def add_run_outputs(self, run_id: str, outputs: list[dict]) -> None:
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO comfy_run_outputs (run_id, node, subfolder, filename, folder_type, "
                           "status) VALUES (?,?,?,?,?,?)",
                           [(run_id, o["node"], o["subfolder"], o["filename"], o["type"], "pending") for o in outputs])

    def run_outputs(self, run_id: str) -> list[dict]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM comfy_run_outputs WHERE run_id=? ORDER BY rowid",
                                                    (run_id,)).fetchall()]

    def set_run_output(self, run_id: str, node: str, subfolder: str, filename: str, status: str, **fields) -> None:
        unknown = set(fields) - {"path", "bytes", "sha256", "media_type", "error"}
        if unknown:
            raise ValueError(f"cột lạ: {sorted(unknown)}")
        extra = "".join(f", {key}=?" for key in fields)
        with self.connect() as db:
            db.execute(f"UPDATE comfy_run_outputs SET status=?, verified_at=?{extra} "
                       "WHERE run_id=? AND node=? AND subfolder=? AND filename=?",
                       (status, utc_now() if status == "verified" else None, *fields.values(), run_id, node, subfolder,
                        filename))

    def reset_failed_outputs(self, run_id: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE comfy_run_outputs SET status='pending', error=NULL WHERE run_id=? AND status='failed'",
                       (run_id,))
