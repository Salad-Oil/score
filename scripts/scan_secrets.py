#!/usr/bin/env python3
"""Refuse to commit or publish a credential.

This is the *offline* half of the secret defence; CI runs gitleaks as an
independent second opinion (see ``.github/workflows/ci.yml``). It exists because
the previous guard -- ``scripts/publish.ps1`` -- matched only file *names* from
``git ls-files``. A real API key and secret, hardcoded inside a ``.py`` file,
passed that check unnoticed and were pushed to a public repository.

So this scans **contents**, in three places:

``--staged``
    The exact blobs that are about to be committed (read from the index, not the
    working tree, so a CRLF/clean filter cannot hide anything).
``--history``
    Every added line in every commit reachable from any ref. A key that was
    committed once and deleted later is still published forever.
``--worktree``
    Every tracked file as it currently sits on disk.

Usage::

    python scripts/scan_secrets.py --staged      # pre-commit
    python scripts/scan_secrets.py --all         # pre-push / publish.ps1

Exit code is 1 if anything was found, 0 otherwise, so it composes with ``&&``
and with ``set -e``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# What a secret looks like
# ---------------------------------------------------------------------------

# A keyword that means "the next token is a credential", then an assignment
# operator, then a quoted or bare value of at least 20 credential-ish characters.
_KEYWORD = r"(?:api[_-]?key|apikey|secret[_-]?key|secretkey|secret|token|passwd|password|pwd|access[_-]?key)"
_VALUE = r"[A-Za-z0-9_\-./+=]{20,}"

PATTERNS: tuple[tuple[str, str], ...] = (
    # Whatever `API_KEY = "..."` / `secret_key: '...'` looks like in any language.
    ("credential-assignment", rf"(?i)\b{_KEYWORD}\b\s*[:=]\s*[\"']?({_VALUE})[\"']?"),
    # Environment form: ROOSTOO_SECRET_KEY=abc123...
    ("env-credential", rf"(?im)^\s*(?:export\s+)?[A-Z0-9_]*{_KEYWORD.upper()}[A-Z0-9_]*\s*=\s*({_VALUE})\s*$"),
    # Private keys.
    ("private-key-block", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
    # Cloud / SaaS token shapes.
    ("aws-access-key-id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("slack-token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ("stripe-key", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}\b"),
)

COMPILED = tuple((name, re.compile(rx)) for name, rx in PATTERNS)

#: Values that are published examples, not credentials. Keep this list short and
#: each entry justified, or it becomes a way for a real key to hide.
ALLOWED_VALUES: frozenset[str] = frozenset(
    {
        # Roostoo's public API documentation example secret, pinned on purpose by
        # tests/test_signing.py so the HMAC implementation is checked against a
        # published vector (canonical string carries timestamp=1580774512000).
        "S1XP1e3UZj6A7H5fATj0jNhqPxxdSJYdInClVN65XAbvqqMKjVHjA7PZj4W12oep",
    }
)

#: Files whose *content* is pattern definitions or documentation about secrets,
#: not secrets themselves.
SKIP_PATH_PARTS = (
    ".gitleaks.toml",
    "scripts/scan_secrets.py",
    "docs/SECURITY.md",
)

#: Obvious placeholders, so templates do not trip the scanner.
PLACEHOLDER_PATTERNS = (
    re.compile(r"(?i)^(?:your|my|test|dummy|fake|example|changeme|placeholder|redacted|xxx+|\*+)"),
    re.compile(r"(?i)(?:your|my|example|placeholder|changeme)[_-]?(?:key|secret|token)"),
    re.compile(r"^[$<{].*[>}]$"),  # ${VAR}, <token>, {secret}
)


def _looks_like_placeholder(value: str) -> bool:
    if any(p.search(value) for p in PLACEHOLDER_PATTERNS):
        return True
    # A single repeated character, or a value with no digits at all alongside a
    # suspiciously round length, is far more likely to be a template.
    return len(set(value)) < 8


@dataclass(frozen=True)
class Finding:
    where: str  # "path" for content scans, "sha path:line" for history
    kind: str
    excerpt: str

    def render(self) -> str:
        return f"  [{self.kind}] {self.where}\n      {self.excerpt}"


def _scan_line(line: str) -> list[tuple[str, str]]:
    """Return ``(kind, matched_value)`` for each credential-shaped match."""
    out: list[tuple[str, str]] = []
    for kind, rx in COMPILED:
        for match in rx.finditer(line):
            value = match.group(1) if match.groups() else match.group(0)
            if value in ALLOWED_VALUES or _looks_like_placeholder(value):
                continue
            out.append((kind, value))
    return out


def _excerpt(line: str, values: Iterable[str]) -> str:
    """A one-line excerpt with every matched value removed *before* truncating.

    Redacting after truncating would leave the head of the secret on screen --
    which is exactly what the first version of this script did, printing 62
    characters of the key while claiming to redact it. A secret scanner that
    echoes the secret into a CI transcript is worse than no scanner.
    """
    text = line.strip()
    for value in values:
        text = text.replace(value, "<redacted>")
    return text[:160]


def scan_text(text: str, where: str) -> list[Finding]:
    """Find credential-shaped strings in ``text``."""
    out: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        hits = _scan_line(line)
        if not hits:
            continue
        out.append(
            Finding(
                f"{where}:{lineno}",
                ", ".join(kind for kind, _ in hits),
                _excerpt(line, [value for _, value in hits]),
            )
        )
    return out


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr}")
    return result.stdout


def _skip(path: str) -> bool:
    normalised = path.replace("\\", "/")
    return any(part in normalised for part in SKIP_PATH_PARTS)


def scan_staged() -> list[Finding]:
    """The blobs that would actually be committed."""
    out: list[Finding] = []
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACM").split()
    for name in names:
        if _skip(name):
            continue
        try:
            # Read the index version, not the working tree: that is what commits.
            blob = _git("show", f":{name}")
        except SystemExit:
            continue
        out.extend(scan_text(blob, name))
    return out


def scan_worktree() -> list[Finding]:
    out: list[Finding] = []
    for name in _git("ls-files").split():
        if _skip(name):
            continue
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        try:
            out.extend(scan_text(path.read_text(encoding="utf-8", errors="replace"), name))
        except OSError:
            continue
    return out


def _history_lines() -> Iterator[tuple[str, str, int, str]]:
    """Yield ``(sha, path, lineno, added_line)`` for every added line in history."""
    current_sha = "unknown"
    current_path = "unknown"
    lineno = 0
    for raw in _git("log", "--all", "-p", "--unified=0", "--no-color", "--format=%x00%H").splitlines():
        if raw.startswith("\x00"):
            current_sha = raw[1:].strip() or current_sha
            current_path = "unknown"
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            current_path = target[2:] if target.startswith("b/") else target
            lineno = 0
            continue
        if raw.startswith("@@"):
            # @@ -a,b +c,d @@ -- track the new-file line number for reporting.
            match = re.search(r"\+(\d+)", raw)
            lineno = int(match.group(1)) - 1 if match else 0
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            lineno += 1
            yield current_sha, current_path, lineno, raw[1:]


def scan_history() -> list[Finding]:
    out: list[Finding] = []
    seen: set[str] = set()
    for sha, path, lineno, line in _history_lines():
        if _skip(path):
            continue
        hits = _scan_line(line)
        if not hits:
            continue
        # History repeats the same blob across many commits; report it once, at
        # the commit that introduced it.
        key = f"{path}:{lineno}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Finding(
                f"{sha[:10]} {path}:{lineno}",
                ", ".join(kind for kind, _ in hits),
                _excerpt(line, [value for _, value in hits]),
            )
        )
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true", help="scan the index (what would be committed)")
    group.add_argument("--history", action="store_true", help="scan every commit reachable from any ref")
    group.add_argument("--worktree", action="store_true", help="scan tracked files on disk")
    group.add_argument("--all", action="store_true", help="staged + worktree + history")
    args = parser.parse_args(argv)

    if not any((args.staged, args.history, args.worktree, args.all)):
        args.all = True

    findings: list[Finding] = []
    scanned: list[str] = []
    if args.staged or args.all:
        findings += scan_staged()
        scanned.append("index")
    if args.worktree or args.all:
        findings += scan_worktree()
        scanned.append("worktree")
    if args.history or args.all:
        findings += scan_history()
        scanned.append("history")

    if not findings:
        print(f"no credentials found ({', '.join(scanned)})")
        return 0

    print(f"REFUSING: {len(findings)} credential-shaped string(s) found in {', '.join(scanned)}:\n", file=sys.stderr)
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    print(
        "\nIf one of these is real: rotate it with the issuer first -- deleting the\n"
        "commit does not unpublish it -- then purge it from history (docs/SECURITY.md).\n"
        "If it is a published example, add the exact value to ALLOWED_VALUES in\n"
        "scripts/scan_secrets.py with a comment saying where it is published.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
