"""Nhập workflow JSON và trích dependency (09 · s6 "Nhập", G06).

Hai dạng file ComfyUI xuất ra:

- **GUI** (``Save``/``Export``): ``{"nodes": [...], "links": [...]}``; mỗi node có ``type`` và ``properties`` với
  ``cnr_id`` (gói trong Comfy Registry, ``comfy-core`` là lõi), ``aux_id`` (``owner/repo`` GitHub khi cài từ git),
  ``ver`` và ``models`` (``name``, ``url``, ``directory``, có khi ``hash``). Trích được dependency, nhưng thường chưa
  đủ để duyệt (URL model ``resolve/main``, thiếu dung lượng, license, ``requirements.txt``): mọi chỗ thiếu ghi vào
  ``gaps``. GUI JSON không gửi thẳng ``/prompt`` được; muốn chạy thì kèm bản ``Export (API)``.
- **API** (``Export (API)``): ``{node_id: {"class_type", "inputs"}}``; không có metadata model/node. Dependency
  không xác định được → ``needs_review``, không đoán (G06). Tên file model trong ``inputs`` chỉ hiện làm gợi ý.

Chỉ đọc JSON; không tra Registry hay mạng ngoài lúc nhập.
"""

from __future__ import annotations

import hashlib
import json
import re

from studio.gpu.provisioning import GITHUB_REPO, HF_RESOLVE, SHA1, SHA256

MAX_WORKFLOW_BYTES = 5 * 1024 * 1024
CORE_CNR = "comfy-core"
# Node chỉ có ở frontend, không chạy trên server (không phải dependency).
FRONTEND_ONLY = {"Note", "MarkdownNote", "Reroute", "PrimitiveNode", "PrimitiveString", "PrimitiveInt",
                 "PrimitiveFloat", "PrimitiveBoolean"}
MODEL_FILE = re.compile(r"[^/\\]+\.(?:safetensors|ckpt|pt|pth|bin|gguf|sft|onnx)", re.IGNORECASE)


class WorkflowError(ValueError):
    """File không phải workflow ComfyUI đọc được."""


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load(raw: bytes) -> dict:
    if len(raw) > MAX_WORKFLOW_BYTES:
        raise WorkflowError(f"file workflow lớn hơn {MAX_WORKFLOW_BYTES} byte")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WorkflowError(f"không phải JSON: {str(exc)[:200]}") from None
    if not isinstance(data, dict) or not data:
        raise WorkflowError("workflow phải là JSON object khác rỗng")
    return data


def detect(data: dict) -> str:
    """``gui`` | ``api``; dạng khác là lỗi."""
    if isinstance(data.get("nodes"), list) and ("links" in data or "version" in data or "last_node_id" in data):
        return "gui"
    if all(isinstance(v, dict) and isinstance(v.get("class_type"), str) and isinstance(v.get("inputs", {}), dict)
           for v in data.values()):
        return "api"
    raise WorkflowError("không nhận ra dạng workflow: cần GUI JSON (nodes, links) hoặc API JSON "
                        "({node_id: {class_type, inputs}})")


def check_api(prompt: dict) -> list[str]:
    """Kiểm cấu trúc prompt API: id node là chuỗi, link ``[node_id, slot]`` trỏ tới node có thật."""
    problems = []
    for node_id, node in prompt.items():
        if not isinstance(node, dict) or not isinstance(node.get("class_type"), str) or not node["class_type"]:
            problems.append(f"node {node_id}: thiếu class_type")
            continue
        for name, value in (node.get("inputs") or {}).items():
            is_link = isinstance(value, list) and len(value) == 2 and isinstance(value[1], int) and isinstance(value[0], str)
            if is_link and value[0] not in prompt:
                problems.append(f"node {node_id}.{name}: link tới node {value[0]} không có")
    return problems


def class_types(prompt: dict) -> list[str]:
    return sorted({node["class_type"] for node in prompt.values() if isinstance(node, dict) and node.get("class_type")})


def prompt_sha256(prompt: dict) -> str:
    return hashlib.sha256(json.dumps(prompt, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def model_hints(prompt: dict) -> list[str]:
    """Tên file giống model trong ``inputs`` của prompt API: chỉ để người duyệt đối chiếu, không phải dependency."""
    found = set()
    for node in prompt.values():
        for value in (node.get("inputs") or {}).values() if isinstance(node, dict) else ():
            if isinstance(value, str) and MODEL_FILE.fullmatch(value.strip()):
                found.add(value.strip())
    return sorted(found)


def _gui_nodes(data: dict) -> list[dict]:
    """Node của GUI JSON, gồm cả node trong subgraph (``definitions.subgraphs[].nodes``)."""
    nodes = [n for n in data.get("nodes") or [] if isinstance(n, dict)]
    for graph in ((data.get("definitions") or {}).get("subgraphs") or []):
        if isinstance(graph, dict):
            nodes += [n for n in graph.get("nodes") or [] if isinstance(n, dict)]
    return nodes


def extract_gui(data: dict) -> dict:
    """Dependency trích từ GUI JSON: ``{"custom_nodes", "models", "gaps", "class_types"}``.

    Custom node chỉ thành dependency đủ khi có ``aux_id`` ``owner/repo`` và ``ver`` là commit 40 hex; vẫn thiếu
    ``requirements.txt`` (không tải repo lúc nhập) nên luôn có gap để người duyệt điền. Model đủ khi URL là
    Hugging Face ghim commit và có SHA-256; dung lượng và license không có trong GUI JSON nên là gap.
    """
    subgraph_ids = {g.get("id") for g in ((data.get("definitions") or {}).get("subgraphs") or []) if isinstance(g, dict)}
    nodes, models, gaps, types = {}, {}, [], set()
    for node in _gui_nodes(data):
        kind = str(node.get("type") or "")
        if not kind or kind in FRONTEND_ONLY or kind in subgraph_ids:
            continue
        types.add(kind)
        props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        cnr, aux, ver = props.get("cnr_id"), props.get("aux_id"), str(props.get("ver") or "")
        if cnr == CORE_CNR:
            pass
        elif isinstance(aux, str) and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", aux):
            repo = f"https://github.com/{aux}"
            entry = nodes.setdefault(repo, {"repo": repo, "commit": ver, "dest": aux.split("/")[1], "requirements": None,
                                            "class_types": []})
            entry["class_types"] = sorted({*entry["class_types"], kind})
            if not SHA1.fullmatch(ver):
                gaps.append(f"{kind}: {aux} ver {ver or '?'} không phải commit 40 hex")
        elif cnr:
            gaps.append(f"{kind}: gói Registry {cnr} {ver or ''}".strip() + " chưa có repo GitHub + commit")
        else:
            gaps.append(f"{kind}: không có cnr_id/aux_id, không biết node thuộc gói nào")
        for model in props.get("models") or []:
            if not isinstance(model, dict) or not model.get("name"):
                continue
            dest = f"{model.get('directory') or '?'}/{model['name']}"
            digest = str(model.get("hash") or "").lower()
            hash_ok = digest and str(model.get("hash_type") or "sha256").lower() == "sha256" and SHA256.fullmatch(digest)
            models.setdefault(dest, {"url": str(model.get("url") or ""), "dest": dest, "size_bytes": None,
                                     "sha256": digest if hash_ok else None, "license": None})
    for model in models.values():
        if not HF_RESOLVE.fullmatch(model["url"]):
            gaps.append(f"model {model['dest']}: URL không ghim commit Hugging Face ({model['url'] or 'không có URL'})")
        if not model["sha256"]:
            gaps.append(f"model {model['dest']}: thiếu SHA-256")
        gaps.append(f"model {model['dest']}: thiếu dung lượng và license")
    for node in nodes.values():
        if not GITHUB_REPO.fullmatch(node["repo"]):
            gaps.append(f"{node['repo']}: không phải repo GitHub hợp lệ")
        gaps.append(f"{node['repo']}: chưa có requirements.txt ở commit {node['commit'][:12] or '?'}")
    return {"custom_nodes": list(nodes.values()), "models": list(models.values()), "gaps": sorted(set(gaps)),
            "class_types": sorted(types)}


def check_object_info(prompt: dict, object_info: dict, skip: set[tuple[str, str]] = frozenset()) -> tuple[list[str], list[str]]:
    """Kiểm prompt với ``/object_info`` của runtime (G07, nửa run): (class_type thiếu, input không hợp lệ).

    Input COMBO (lựa chọn liệt kê, ví dụ tên file model) có giá trị không nằm trong danh sách runtime báo là
    thiếu model trên máy: chặn trước khi submit, khỏi chờ ComfyUI trả ``node_errors``. ``skip``: (node, input) là ảnh
    input studio sẽ upload ngay trước khi gửi, nên chưa có trong danh sách của máy.
    """
    missing = sorted({t for t in class_types(prompt) if t not in object_info})
    invalid = []
    for node_id, node in sorted(prompt.items()):
        spec = object_info.get(node.get("class_type"))
        if not isinstance(spec, dict):
            continue
        declared = {}
        for group in ("required", "optional"):
            declared.update((spec.get("input") or {}).get(group) or {})
        for name, value in (node.get("inputs") or {}).items():
            if (node_id, name) in skip:
                continue
            choices = _combo(declared.get(name))
            if choices is not None and isinstance(value, str) and value not in choices:
                invalid.append(f"node {node_id} ({node['class_type']}).{name} = {value!r} không có trên runtime")
    return missing, invalid


def _combo(definition) -> list[str] | None:
    """Danh sách lựa chọn của input COMBO: ``[[a, b, …], {...}]`` (cũ) hoặc ``["COMBO", {"options": [...]}]`` (mới)."""
    if not isinstance(definition, list) or not definition:
        return None
    head = definition[0]
    if isinstance(head, list):
        return [str(v) for v in head]
    if head == "COMBO" and len(definition) > 1 and isinstance(definition[1], dict):
        options = definition[1].get("options")
        return [str(v) for v in options] if isinstance(options, list) else None
    return None
