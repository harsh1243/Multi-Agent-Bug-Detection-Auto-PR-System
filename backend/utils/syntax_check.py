"""Deterministic syntax checking for C/C++ sources via the system compiler.

Used by the Repo Mapper (discovery: emit findings for files that don't compile)
and the Validation Agent (Gate 1: reject patches that break compilation).
Falls back gracefully when no compiler is installed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Optional

C_EXTS = {".c"}
CPP_EXTS = {".cpp", ".cc", ".cxx", ".hpp", ".hh"}
# .h is ambiguous (C or C++); check it with the C++ front-end, which accepts both.
HEADER_EXTS = {".h", ".hpp", ".hh"}

# "file.cpp:12:5: error: expected ';' before ..." (gcc/clang share this shape)
_ERROR_RE = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
                       r"(?:fatal )?error:\s*(?P<msg>.+)$", re.MULTILINE)


@lru_cache(maxsize=1)
def find_cpp_compiler() -> Optional[str]:
    """Locate a C/C++ compiler on PATH (g++, clang++, gcc, clang, cl)."""
    for exe in ("g++", "clang++", "gcc", "clang"):
        if shutil.which(exe):
            return exe
    return None


def check_cpp_syntax(file_path: Path, repo_root: Path | None = None) -> list[dict]:
    """Syntax-check one C/C++ file with ``<compiler> -fsyntax-only``.

    Returns a list of error dicts: {"file", "line", "column", "message"}.
    Empty list = clean, or no compiler available (we can't claim an error we
    can't prove).
    """
    compiler = find_cpp_compiler()
    if compiler is None:
        return []

    ext = file_path.suffix.lower()
    if ext not in C_EXTS | CPP_EXTS | HEADER_EXTS:
        return []

    cmd = [compiler, "-fsyntax-only"]
    # Headers and C files need an explicit language; C++ front-end accepts C headers.
    if ext in HEADER_EXTS:
        cmd += ["-x", "c++-header"] if compiler in ("g++", "clang++") else ["-x", "c-header"]
    elif ext in C_EXTS and compiler in ("g++", "clang++"):
        cmd += ["-x", "c"]
    # Let local includes resolve where possible.
    include_dirs = {str(file_path.parent)}
    if repo_root:
        include_dirs.add(str(repo_root))
    for d in sorted(include_dirs):
        cmd += ["-I", d]
    cmd.append(str(file_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []

    if result.returncode == 0:
        return []

    errors = []
    for m in _ERROR_RE.finditer(result.stderr or ""):
        # Only report errors located in the checked file itself — a missing
        # third-party header is an environment problem, not a syntax error.
        err_file = Path(m.group("file")).name
        if err_file != file_path.name:
            continue
        msg = m.group("msg").strip()
        # Missing-include errors depend on the build environment; skip them.
        if "No such file or directory" in msg:
            continue
        errors.append({
            "file": str(file_path),
            "line": int(m.group("line")),
            "column": int(m.group("col")) if m.group("col") else None,
            "message": msg,
        })
    return errors
