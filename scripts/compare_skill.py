"""Compare a staged skill with an installed skill without modifying either."""

import argparse
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sys


IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
IGNORED_FILES = {".DS_Store", "Thumbs.db"}
MAX_TEXT_BYTES = 2 * 1024 * 1024


def reject_link(path):
    if path.is_symlink() or (
        path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400
    ):
        raise ValueError(f"Linked paths are not supported: {path}")


def check_ancestors(path):
    absolute = path.absolute()
    for item in (absolute, *absolute.parents):
        reject_link(item)


def fail_walk(error):
    raise error


def snapshot(root, allow_missing=False):
    check_ancestors(root)
    if not root.exists():
        if allow_missing:
            return None
        raise ValueError(f"Staged skill does not exist: {root}")
    if not root.is_dir() or not (root / "SKILL.md").is_file():
        raise ValueError(f"Missing skill directory or SKILL.md: {root}")
    files = {}
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=fail_walk):
        folder = Path(directory)
        for name in dirs + names:
            reject_link(folder / name)
        dirs[:] = sorted(name for name in dirs if name not in IGNORED_DIRS)
        for name in sorted(names):
            if name in IGNORED_FILES or name.endswith((".pyc", ".pyo")):
                continue
            path = folder / name
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            files[path.relative_to(root).as_posix()] = digest.hexdigest()
    return files


def fingerprint(files):
    if files is None:
        return None
    encoded = json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_baseline(path, installed):
    if path is None:
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict):
        raise ValueError("Baseline must be a JSON object.")
    recorded_path = record.get("installed_path")
    if not isinstance(recorded_path, str) or (
        os.path.normcase(os.path.abspath(recorded_path))
        != os.path.normcase(os.path.abspath(installed))
    ):
        raise ValueError("Baseline belongs to another installation path.")
    files = record.get("files")
    if not isinstance(files, dict) or not files or any(
        not isinstance(name, str)
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        for name, digest in files.items()
    ):
        raise ValueError("Baseline must contain a complete file hash mapping.")
    if not isinstance(record.get("source"), str) or not record["source"]:
        raise ValueError("Baseline source is missing.")
    return record


def text_changes(staged, installed, names, limit):
    result = {}
    for name in names:
        paths = (installed / name, staged / name)
        versions = []
        reason = None
        for path in paths:
            if not path.exists():
                versions.append("")
                continue
            if path.stat().st_size > MAX_TEXT_BYTES:
                reason = "File exceeds the text preview size limit."
                break
            data = path.read_bytes()
            if b"\x00" in data:
                reason = "Binary file; content preview unavailable."
                break
            try:
                versions.append(data.decode("utf-8"))
            except UnicodeDecodeError:
                reason = "Non-UTF-8 file; content preview unavailable."
                break
        if reason:
            result[name] = {"preview": None, "reason": reason}
            continue
        diff = list(difflib.unified_diff(
            versions[0].splitlines(keepends=True),
            versions[1].splitlines(keepends=True),
            fromfile="installed/" + name,
            tofile="staged/" + name,
        ))
        result[name] = {
            "preview": "".join(diff[:limit]),
            "truncated": len(diff) > limit,
        }
    return result


def compare(staged, installed, source, baseline=None, include_diff=False, limit=120):
    staged_files = snapshot(staged)
    installed_files = snapshot(installed, allow_missing=True)
    local = installed_files or {}
    added = sorted(staged_files.keys() - local.keys())
    deleted = sorted(local.keys() - staged_files.keys())
    modified = sorted(
        name for name in staged_files.keys() & local.keys()
        if staged_files[name] != local[name]
    )
    source_conflict = bool(
        baseline and installed_files is not None and baseline["source"] != source
    )
    if installed_files is None:
        status = "not_installed"
    elif source_conflict:
        status = "source_conflict"
    elif staged_files == installed_files:
        status = "identical"
    else:
        status = "different"
    result = {
        "status": status,
        "source": source,
        "previous_source": baseline["source"] if baseline else None,
        "added": added,
        "modified": modified,
        "deleted": deleted,
        "remote_changed": staged_files != baseline["files"] if baseline else None,
        "local_changed": installed_files != baseline["files"] if baseline else None,
        "staged_fingerprint": fingerprint(staged_files),
        "installed_fingerprint": fingerprint(installed_files),
        "staged_files": staged_files,
        "installed_files": installed_files,
    }
    if include_diff:
        result["text_diffs"] = text_changes(
            staged, installed, sorted(added + modified + deleted), limit
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", required=True, type=Path)
    parser.add_argument("--installed", required=True, type=Path)
    parser.add_argument("--source", required=True, help="owner/repo:relative-skill-path")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--max-diff-lines", type=int, default=120)
    args = parser.parse_args()
    if args.max_diff_lines < 1:
        parser.error("--max-diff-lines must be positive")
    try:
        baseline = read_baseline(args.baseline, args.installed)
        result = compare(
            args.staged, args.installed, args.source, baseline,
            args.diff, args.max_diff_lines
        )
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=True))
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
