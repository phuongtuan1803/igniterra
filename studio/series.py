"""Series Bible đầy đủ (Gate 3, FR-13): bible.yaml, ảnh tham chiếu, character sheet.

Web và CLI chỉ ghi dữ liệu; sinh ảnh tham chiếu (trả phí) là job trong ``series_jobs`` do worker chạy,
vì web không gọi provider. Ảnh tham chiếu và sheet chỉ được thêm, không ghi đè.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import yaml   # pyyaml do gói mpt ghim

from studio import costs, media, prompt
from studio.contracts import Character, SeriesBible
from studio.providers.base import ContentRefused, Fatal, QualityFail, Retryable, Unconfirmed
from studio.store import Store
from studio.taskdir import commit_file, fsync_dir

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
VIEWS = {"front": "front view", "side": "side profile view", "three_quarter": "three-quarter view"}
SHEET_HEIGHT = 1536


class SeriesError(Exception):
    pass


class SeriesDir:
    def __init__(self, data_dir: Path, name: str):
        if not NAME.match(name):
            raise SeriesError("tên series: chữ thường, số, gạch nối, tối đa 41 ký tự")
        self.name = name
        self.root = Path(data_dir) / "series" / name

    @property
    def bible_path(self) -> Path:
        return self.root / "bible.yaml"

    @property
    def costs_path(self) -> Path:
        return self.root / "costs.jsonl"

    def exists(self) -> bool:
        return self.bible_path.is_file()

    def bible(self) -> SeriesBible:
        data = yaml.safe_load(self.bible_path.read_text(encoding="utf-8")) or {}
        return SeriesBible.model_validate({**data, "name": self.name})

    def save(self, bible: SeriesBible) -> None:
        """Thay bible.yaml qua file tạm; bản trước được giữ trong history/."""
        self.root.mkdir(parents=True, exist_ok=True)
        if self.bible_path.is_file():
            history = self.root / "history"
            history.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
            shutil.copyfile(self.bible_path, history / f"bible_{stamp}.yaml")
        data = bible.model_dump(mode="json", exclude={"name"})
        fd, name = tempfile.mkstemp(prefix=".bible-", dir=self.root)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, self.bible_path)
        fsync_dir(self.root)

    def rel(self, path: Path) -> str:
        return str(Path(path).resolve().relative_to(self.root.resolve()))

    def candidates(self, character: str) -> list[Path]:
        folder = self.root / "refs" / "candidates"
        return sorted(folder.glob(f"{character}_*")) if folder.is_dir() else []

    def next_name(self, folder: Path, stem: str, suffix: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        number = 1
        while any(folder.glob(f"{stem}_v{number}.*")) or any(folder.glob(f"{stem}_a{number}.*")):
            number += 1
        return folder / f"{stem}_{'a' if folder.name == 'candidates' else 'v'}{number}{suffix}"


def list_series(data_dir: Path) -> list[str]:
    folder = Path(data_dir) / "series"
    return sorted(p.name for p in folder.iterdir() if (p / "bible.yaml").is_file()) if folder.is_dir() else []


def build_sheet(sd: SeriesDir, refs: list[str], character: str) -> str:
    """Ghép ảnh tham chiếu cạnh nhau cùng chiều cao → refs/<nhân vật>_sheet_vN.png (cho model nhận 1 ảnh)."""
    paths = [sd.root / ref for ref in refs]
    final = sd.next_name(sd.root / "refs", f"{character}_sheet", ".png")
    tmp = final.with_name(f".tmp-{final.name}")
    inputs, filters = [], []
    for index, path in enumerate(paths):
        inputs += ["-i", str(path)]
        filters.append(f"[{index}:v]scale=-2:{SHEET_HEIGHT},setsar=1[v{index}]")
    stack = "".join(f"[v{i}]" for i in range(len(paths)))
    graph = ";".join(filters) + (f";{stack}hstack=inputs={len(paths)}[out]" if len(paths) > 1 else "")
    out_label = "[out]" if len(paths) > 1 else "[v0]"
    media.run([media.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *inputs,
               "-filter_complex", graph, "-map", out_label, "-frames:v", "1", "-f", "image2", "-c:v", "png", str(tmp)])
    commit_file(tmp, final)
    return sd.rel(final)


class SeriesActions:
    """Hành động của người trên trang Series; không gọi provider."""

    def __init__(self, store: Store, config: dict):
        self.store = store
        self.config = config

    def dir(self, name: str) -> SeriesDir:
        return SeriesDir(Path(self.config["data_dir"]), name)

    def create(self, name: str, bible: SeriesBible | None = None) -> SeriesDir:
        sd = self.dir(name)
        if sd.exists():
            raise SeriesError(f"series {name} đã có")
        sd.save(bible or SeriesBible(name=name))
        return sd

    def create_from_task(self, name: str, task_id: str) -> SeriesDir:
        """Khởi tạo series từ cast của ScenePlan một task (mô tả nhân vật đã dùng ở Gate 2)."""
        from studio.taskdir import TaskDir

        task = self.store.task(task_id)
        plan = TaskDir(Path(self.config["data_dir"]), task_id).plan(task["plan_rev"])
        bible = SeriesBible(name=name, characters={key: Character(description=text) for key, text in plan.cast.items()})
        return self.create(name, bible)

    def update(self, name: str, **fields) -> SeriesBible:
        sd = self.dir(name)
        data = sd.bible().model_dump(mode="json")
        for key, value in fields.items():
            if value is None:
                continue
            if key in ("style", "negative"):
                data[key] = value
            elif key == "lexicon":
                data["lexicon"] = value
            elif key in ("voice_name", "voice_rate", "voice_wps"):
                data["voices"].setdefault("narrator", {})[key.split("_", 1)[1]] = value
            else:
                raise SeriesError(f"không sửa được trường {key}")
        bible = SeriesBible.model_validate(data)
        sd.save(bible)
        return bible

    def set_character(self, name: str, key: str, description: str) -> None:
        if not NAME.match(key):
            raise SeriesError("khóa nhân vật: chữ thường, số, gạch nối")
        sd = self.dir(name)
        bible = sd.bible()
        current = bible.characters.get(key)
        bible.characters[key] = Character(description=description, refs=current.refs if current else [],
                                          sheet=current.sheet if current else None)
        sd.save(bible)

    def request_ref(self, name: str, key: str, view: str, note: str | None = None) -> int:
        sd = self.dir(name)
        if key not in sd.bible().characters:
            raise SeriesError(f"series {name} không có nhân vật {key}")
        if view not in VIEWS:
            raise SeriesError(f"góc nhìn phải là {', '.join(VIEWS)}")
        return self.store.add_series_job(name, key, view, note)

    def upload_ref(self, name: str, key: str, file: Path, source: str, rights: str) -> str:
        sd = self.dir(name)
        file = Path(file)
        final = sd.next_name(sd.root / "refs" / "candidates", f"{key}_upload", file.suffix.lower())
        tmp = final.with_name(f".tmp-{final.name}")
        shutil.copyfile(file, tmp)
        commit_file(tmp, final)
        with (sd.root / "refs" / "sources.yaml").open("a", encoding="utf-8") as handle:
            yaml.safe_dump([{"file": sd.rel(final), "source": source, "rights": rights, "ai_generated": False}],
                           handle, allow_unicode=True)
        return sd.rel(final)

    def approve_refs(self, name: str, key: str, candidates: list[str]) -> Character:
        """Chọn ảnh tham chiếu được duyệt (2–3 góc nhìn) và dựng lại character sheet."""
        sd = self.dir(name)
        bible = sd.bible()
        if key not in bible.characters:
            raise SeriesError(f"không có nhân vật {key}")
        refs = []
        for candidate in candidates:
            source = sd.root / candidate
            if not source.is_file() or os.path.commonpath([source.resolve(), sd.root.resolve()]) != str(sd.root.resolve()):
                raise SeriesError(f"không có ảnh {candidate}")
            media.image_size(source)
            final = sd.next_name(sd.root / "refs", f"{key}_ref", source.suffix)
            tmp = final.with_name(f".tmp-{final.name}")
            shutil.copyfile(source, tmp)
            commit_file(tmp, final)
            refs.append(sd.rel(final))
        if not refs:
            raise SeriesError("cần ít nhất một ảnh")
        character = bible.characters[key].model_copy(update={"refs": refs, "sheet": build_sheet(sd, refs, key)})
        bible.characters[key] = character
        sd.save(bible)
        return character


# ---------------------------------------------------------------- worker


def ref_prompt(bible: SeriesBible, key: str, view: str, note: str | None, anchored: bool = False) -> str:
    parts = [
        "Character reference image for a film production.",
        *(["Same character as the reference image: keep identical design, proportions, colours and outfit."] if anchored else []),
        bible.style.rstrip(".") + ".",
        f"{key.capitalize()}: {bible.characters[key].description.rstrip('.')}.",
        f"{VIEWS[view]}, full body, standing in a neutral pose, plain mid-grey studio background, even soft lighting.",
    ]
    if note:
        parts.append(f"Correction: {note.rstrip('.')}.")
    parts.append(bible.negative.rstrip(".") + ".")
    return "\n".join(parts)


def run_series_job(store: Store, config: dict, job: dict) -> str:
    """Sinh một ảnh tham chiếu ứng viên. Lượt gọi trả phí qua costs.paid_call của series."""
    from studio import providers

    sd = SeriesDir(Path(config["data_dir"]), job["series"])
    request_id = f"series:{job['series']}:{job['character']}:{job['view']}:{job['id']}.{job['attempt']}"
    note = job["note"]
    feedback: list[str] = []
    last_error = ""
    try:
        bible = sd.bible()
        image = providers.get("image", config)
        # Góc nhìn sinh độc lập thì tự trôi; ảnh đã duyệt đầu tiên làm mốc cho các góc sau.
        approved = bible.characters[job["character"]].refs
        anchor = [sd.root / approved[0]] if approved else []
        for attempt in range(1, int(config["retry"]["max_attempts"]) + 1):
            hint = "; ".join([n for n in [note, *feedback] if n]) or None
            final = sd.next_name(sd.root / "refs" / "candidates", f"{job['character']}_{job['view']}", "")
            tmp = final.with_name(f".tmp-{final.name}")
            try:
                with costs.paid_call(sd.costs_path, f"{request_id}:{attempt}", image.name, image.model):
                    candidate = image.generate(ref_prompt(bible, job["character"], job["view"], hint, bool(anchor)),
                                               anchor, "1080x1920", tmp)
                written = Path(candidate.path)
                width, height = media.image_size(written)
                if min(width, height) < 512:
                    raise QualityFail(f"ảnh tham chiếu {width}x{height} quá nhỏ")
                final = final.with_suffix(written.suffix)
                commit_file(written, final)
                store.finish_series_job(job["id"], "done", output_path=sd.rel(final))
                return "done"
            except ContentRefused as exc:
                last_error = str(exc)
                feedback.append("calm, non-graphic, safe-for-all-audiences depiction")
            except (QualityFail, Retryable) as exc:   # lý do kỹ thuật không giúp model ảnh; chỉ thử lại
                last_error = str(exc)
            finally:
                for leftover in final.parent.glob(f".tmp-{final.stem}*"):
                    leftover.unlink(missing_ok=True)
        store.finish_series_job(job["id"], "failed", error=f"hết lượt thử: {last_error}"[:500])
        return "failed"
    except Unconfirmed as exc:
        store.finish_series_job(job["id"], "unknown", error=str(exc)[:500])
        return "unknown"
    except (Fatal, Exception) as exc:  # noqa: BLE001 — job hỏng không làm dừng worker
        store.finish_series_job(job["id"], "failed", error=f"{type(exc).__name__}: {exc}"[:500])
        return "failed"


def recover_series_jobs(store: Store, config: dict) -> None:
    """Worker khởi động lại: job 'running' có pending mồ côi → unknown, còn lại → failed."""
    for job in store.series_jobs(status="running"):
        sd = SeriesDir(Path(config["data_dir"]), job["series"])
        prefix = f"series:{job['series']}:{job['character']}:{job['view']}:{job['id']}."
        orphan = [o for o in costs.orphans(sd.costs_path) if o.request_id.startswith(prefix)]
        for event in orphan:
            costs.mark_unknown(sd.costs_path, event.request_id, "worker dừng sau khi gửi")
        store.finish_series_job(job["id"], "unknown" if orphan else "failed", error="worker dừng giữa chừng")
