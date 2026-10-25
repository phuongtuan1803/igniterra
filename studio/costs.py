"""costs.jsonl: ghi ``pending`` (fsync) TRƯỚC lượt gọi trả phí, kết quả SAU (ADR-0006).

Không tra giá, không chặn ngân sách. Giới hạn chi tiêu nằm ở tài khoản provider.
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from studio.contracts import CostEvent
from studio.providers.base import ContentRefused, Fatal, QualityFail, Retryable, Unconfirmed

_lock = threading.Lock()
_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("paid_call", default=None)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def append(path: Path, event: CostEvent) -> None:
    line = event.model_dump_json(exclude_none=True) + "\n"
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())


def read(path: Path) -> list[CostEvent]:
    if not path.is_file():
        return []
    return [CostEvent.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def orphans(path: Path) -> list[CostEvent]:
    """Dòng ``pending`` không có dòng kết quả cùng ``request_id``."""
    pending: dict[str, CostEvent] = {}
    for event in read(path):
        if event.status == "pending":
            pending[event.request_id] = event
        else:
            pending.pop(event.request_id, None)
    return list(pending.values())


def mark_unknown(path: Path, request_id: str, note: str) -> None:
    append(path, CostEvent(ts=_now(), request_id=request_id, status="unknown", note=note))


def report_usage(units: dict[str, float] | None = None, est_usd: float | None = None, model: str | None = None) -> None:
    """Adapter gọi trong lượt gọi trả phí để ghi lượng dùng thật."""
    call = _current.get()
    if call is not None:
        if units:
            call["units"] = units
        if est_usd is not None:
            call["est_usd"] = est_usd
        if model:
            call["model"] = model


@contextmanager
def paid_call(path: Path, request_id: str, provider: str, model: str | None) -> Iterator[dict]:
    """pending → gọi → ok | failed | unknown. Lỗi được ném lại nguyên loại cho worker."""
    append(path, CostEvent(ts=_now(), request_id=request_id, status="pending", provider=provider, model=model))
    call: dict = {"units": None, "est_usd": None, "model": None}
    token = _current.set(call)
    try:
        yield call
    except Unconfirmed as exc:
        append(path, CostEvent(ts=_now(), request_id=request_id, status="unknown", note=str(exc)[:300]))
        raise
    except ContentRefused as exc:
        append(path, CostEvent(ts=_now(), request_id=request_id, status="failed", note=f"refused: {exc.reason}"[:300]))
        raise
    except QualityFail as exc:
        # Provider đã trả kết quả (có thể đã tính phí); chỉ nội dung trượt kiểm tra.
        append(path, CostEvent(ts=_now(), request_id=request_id, status="ok", units=call["units"],
                               est_usd=call["est_usd"], model=call["model"], note=f"quality_fail: {exc.reason}"[:300]))
        raise
    except (Retryable, Fatal) as exc:
        append(path, CostEvent(ts=_now(), request_id=request_id, status="failed", note=str(exc)[:300]))
        raise
    except BaseException as exc:
        # Lỗi lạ trong lúc gọi: không chắc provider đã nhận hay chưa.
        append(path, CostEvent(ts=_now(), request_id=request_id, status="unknown", note=f"{type(exc).__name__}: {exc}"[:300]))
        raise
    else:
        append(path, CostEvent(ts=_now(), request_id=request_id, status="ok", units=call["units"],
                               est_usd=call["est_usd"], model=call["model"]))
    finally:
        _current.reset(token)


def total(path: Path) -> tuple[float, int]:
    """Tổng est_usd đã biết và số dòng ok thiếu giá."""
    usd, missing = 0.0, 0
    for event in read(path):
        if event.status == "ok":
            if event.est_usd is None:
                missing += 1
            else:
                usd += event.est_usd
    return usd, missing
