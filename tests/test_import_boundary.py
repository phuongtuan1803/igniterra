"""NFR-01: chỉ studio/providers/mpt/ được import app.*; core không import adapter cụ thể."""

from __future__ import annotations

import ast
from pathlib import Path

STUDIO = Path(__file__).resolve().parent.parent / "studio"
ADAPTERS = ("studio.providers.mpt", "studio.providers.direct", "studio.providers.fake", "studio.render")
CORE = ("pipeline.py", "gates.py", "store.py", "taskdir.py", "costs.py", "contracts.py", "prompt.py", "config.py",
        "media.py", "cli.py", "series.py", "steps", "web")


def imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_only_mpt_adapters_import_app():
    offenders = []
    for path in STUDIO.rglob("*.py"):
        if (STUDIO / "providers" / "mpt") in path.parents:
            continue
        if any(name == "app" or name.startswith("app.") for name in imports(path)):
            offenders.append(str(path.relative_to(STUDIO)))
    assert offenders == []


def test_mpt_adapters_do_import_app():
    assert any(name.startswith("app") for name in imports(STUDIO / "providers" / "mpt" / "tts.py"))


def test_core_does_not_import_adapters():
    offenders = []
    for entry in CORE:
        for path in ([STUDIO / entry] if entry.endswith(".py") else (STUDIO / entry).rglob("*.py")):
            for name in imports(path):
                if name.startswith(ADAPTERS):
                    offenders.append(f"{path.relative_to(STUDIO)} → {name}")
    assert offenders == []


def test_vendor_is_unmodified_copy():
    import hashlib
    import importlib.util

    vendored = (STUDIO / "vendor" / "video_project.py").read_text(encoding="utf-8").split("\n", 4)[4]
    spec = importlib.util.find_spec("app.services.video_project")
    upstream = Path(spec.origin).read_text(encoding="utf-8")
    assert hashlib.sha256(vendored.encode()).hexdigest() == hashlib.sha256(upstream.encode()).hexdigest()
