"""Reference-document tools: read-only access to ``.md`` files below one directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

MAX_DOC_CHARS = 60_000


def _is_inside(base: Path, target: Path) -> bool:
    try:
        return target.resolve().is_relative_to(base.resolve())
    except (OSError, ValueError):
        return False


def _title(path: Path) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:80]
    except (OSError, UnicodeDecodeError):
        pass
    return path.name


def list_documents(docs_dir: Path | None) -> dict[str, Any]:
    if docs_dir is None or not docs_dir.is_dir():
        return {"ok": True, "documents": []}
    documents = [
        {"doc_name": path.name, "title": _title(path)}
        for path in sorted(docs_dir.glob("*.md"))
        if path.is_file() and _is_inside(docs_dir, path)
    ]
    return {"ok": True, "documents": documents}


def read_document(docs_dir: Path | None, name: str) -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        return {"ok": False, "error": "doc_name 不能为空"}
    available = [d["doc_name"] for d in list_documents(docs_dir)["documents"]]
    # Only a bare file name is accepted: no separators, no drive letters, no parent references.
    if name != Path(name).name or not name.lower().endswith(".md") or docs_dir is None:
        return {"ok": False, "error": f"文档 '{name}' 不存在", "available_documents": available}
    target = docs_dir / name
    if not target.is_file() or not _is_inside(docs_dir, target):
        return {"ok": False, "error": f"文档 '{name}' 不存在", "available_documents": available}
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"ok": False, "error": f"文档 '{name}' 无法读取"}
    truncated = len(content) > MAX_DOC_CHARS
    return {
        "ok": True,
        "doc_name": name,
        "content": content[:MAX_DOC_CHARS],
        **({"truncated": True} if truncated else {}),
    }
