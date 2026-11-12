#!/usr/bin/env python3
"""Scan a tree for secrets and encoded denylist terms.

Terms are read from scripts/denylist.b64 and decoded only in memory.
Findings are printed as path, line, and category. Matched text is never printed.
"""
from __future__ import annotations

import argparse
import base64
import ipaddress
import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ico", ".pdf", ".zip", ".gz",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
EMAIL_DOMAIN_ALLOW = {"g.us", "s.whatsapp.net", "whatsapp.net"}

PHONE_E164 = re.compile(r"(?<!\w)\+[1-9]\d{7,14}\b")
PHONE_IL = re.compile(r"(?<!\w)0(?:5\d|[23489]\d?)(?:[\s.\-]?\d){7}\b")
PHONE_SEP = re.compile(r"\b\d{3}[\s.\-]\d{3}[\s.\-]\d{4}\b")
PHONE_BARE = re.compile(r"\b\d{10,15}\b")

IP_RE = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"
)
PEM_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
TOKEN_RES = (
    ("api-key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}")),
    ("token", re.compile(r"\b(?:ghp_|gho_|ghu_|ghs_|github_pat_|glpat-)[A-Za-z0-9_\-]{10,}")),
    ("token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("api-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("token", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{8,}")),
)

DOC_NETS = (
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
)
LOOPBACK = ipaddress.ip_address("127.0.0.1")
UNSPECIFIED = ipaddress.ip_address("0.0.0.0")


def denylist_path() -> Path:
    return Path(__file__).resolve().parent / "denylist.b64"


def load_denylist(path: Path) -> list[tuple[str, bool]]:
    if not path.is_file():
        raise SystemExit(f"missing denylist file: {path}")
    items: list[tuple[str, bool]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            term = base64.b64decode(raw, validate=True).decode("utf-8")
        except Exception as exc:
            raise SystemExit(f"denylist entry is not valid base64: {exc}") from exc
        if not term:
            raise SystemExit("denylist contains an empty entry")
        items.append((term, term.isascii()))
    if not items:
        raise SystemExit(f"denylist is empty: {path}")
    return items


def ip_allowed(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return True
    if ip in (LOOPBACK, UNSPECIFIED):
        return True
    return any(ip in net for net in DOC_NETS)


def in_url(line: str, start: int) -> bool:
    for scheme in ("https://", "http://"):
        idx = line.rfind(scheme, 0, start)
        if idx == -1:
            continue
        chunk = line[idx:start]
        if " " not in chunk and "\t" not in chunk:
            return True
    return False


def scan_text(rel: str, text: str, denylist: list[tuple[str, bool]]) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for number, line in enumerate(text.splitlines(), 1):
        categories: set[str] = set()
        lowered = line.lower()
        for term, is_ascii in denylist:
            if is_ascii:
                if term.lower() in lowered:
                    categories.add("denylist")
            elif term in line:
                categories.add("denylist")
        for match in EMAIL_RE.finditer(line):
            domain = match.group(0).split("@", 1)[1].lower()
            if domain not in EMAIL_DOMAIN_ALLOW:
                categories.add("email")
        if PHONE_E164.search(line) or PHONE_IL.search(line) or PHONE_SEP.search(line):
            categories.add("phone")
        for match in PHONE_BARE.finditer(line):
            end = match.end()
            if end < len(line) and line[end] == "@":
                continue
            if in_url(line, match.start()):
                continue
            if line[max(0, match.start() - 2):match.start()].lower().endswith("id"):
                categories_skip = True
            else:
                categories_skip = False
            if not categories_skip:
                categories.add("phone")
        for match in IP_RE.finditer(line):
            if not ip_allowed(match.group(0)):
                categories.add("server-ip")
        if PEM_RE.search(line):
            categories.add("private-key")
        for category, pattern in TOKEN_RES:
            if pattern.search(line):
                categories.add(category)
        for category in sorted(categories):
            hits.append((rel, number, category))
    return hits


def iter_files(root: Path):
    if root.is_file():
        yield root, root.name
        return
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        yield path, str(path.relative_to(root))


def scan_root(root: Path, denylist: list[tuple[str, bool]]) -> list[tuple[str, int, str]]:
    hits: list[tuple[str, int, str]] = []
    for path, rel in iter_files(root):
        data = path.read_bytes()
        if b"\0" in data[:4096]:
            continue
        text = data.decode("utf-8", errors="replace")
        hits.extend(scan_text(rel, text, denylist))
    return hits


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Scan for secrets and denylist terms.")
    parser.add_argument("root", nargs="?", default=".", help="file or directory to scan")
    args = parser.parse_args(argv)
    root = Path(args.root)
    if not root.exists():
        print(f"scan root does not exist: {root}", file=sys.stderr)
        return 2
    denylist = load_denylist(denylist_path())
    hits = scan_root(root, denylist)
    for rel, number, category in hits:
        print(f"{rel}:{number} category={category}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
