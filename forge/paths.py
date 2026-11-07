"""glob matching for private path rules.

patterns are posix relative paths with `*`, `?` and `**`. `**/x` matches x at
any depth, `a/**` matches everything under a, `*.key` matches only at the root.
matching is done on the path as it appears in the tree, so a rule never leaks
across the repository boundary.
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = ["matches", "matches_any", "compile_pattern", "static_prefix", "could_match_under"]


@lru_cache(maxsize=4096)
def compile_pattern(pattern: str) -> re.Pattern:
    p = pattern.strip().strip("/")
    out: list[str] = []
    i, n = 0, len(p)
    while i < n:
        c = p[i]
        if c == "*":
            if p[i : i + 3] == "**/":
                out.append("(?:.*/)?")
                i += 3
            elif p[i : i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = p.find("]", i)
            if j == -1:
                out.append(re.escape(c))
                i += 1
            else:
                out.append(p[i : j + 1])
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def matches(path: str, pattern: str) -> bool:
    return bool(compile_pattern(pattern).match(path.strip("/")))


def matches_any(path: str, patterns) -> bool:
    return any(matches(path, p) for p in patterns)


@lru_cache(maxsize=4096)
def static_prefix(pattern: str) -> str:
    """the literal directory part of a pattern, before any wildcard."""
    p = pattern.strip().strip("/")
    cut = len(p)
    for k, ch in enumerate(p):
        if ch in "*?[":
            cut = k
            break
    head = p[:cut]
    if "/" in head:
        return head.rsplit("/", 1)[0]
    return ""


def could_match_under(pattern: str, directory: str) -> bool:
    """true when some path inside `directory` could match `pattern`.

    used to skip whole subtrees while pruning, so a rule for `secrets/**`
    never walks the rest of the repository.
    """
    directory = directory.strip("/")
    if directory == "":
        return True
    prefix = static_prefix(pattern)
    if prefix == "":
        return True
    return (
        prefix == directory
        or prefix.startswith(directory + "/")
        or directory.startswith(prefix + "/")
    )
