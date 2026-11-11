#!/usr/bin/env python3
"""Build a temporary fixture and require the secret scanners to reject it.

Plaintext samples are assembled only inside the temp directory. This file and
the workflow store no denylist terms and no sample secrets in plaintext.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCAN = Path(__file__).resolve().parent / "secret_scan.py"
DENYLIST = Path(__file__).resolve().parent / "denylist.b64"


def chars(*codes: int) -> str:
    return "".join(chr(code) for code in codes)


def load_terms() -> list[str]:
    terms: list[str] = []
    for line in DENYLIST.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        terms.append(base64.b64decode(raw, validate=True).decode("utf-8"))
    if not terms:
        raise SystemExit("denylist is empty")
    return terms


def main() -> int:
    terms = load_terms()
    email = chars(112, 101, 114, 115, 111, 110, 64, 101, 120, 97, 109, 112, 108, 101, 46, 99, 111, 109)
    phone = chars(43, 49, 53, 53, 53, 53, 53, 53, 53, 48, 49, 48, 48)
    server_ip = chars(56, 46, 56, 46, 56, 46, 56)
    # Keyword plus a non-example key shape. The well-known sample key is ignored by gitleaks.
    api_key = "token=" + chars(
        65, 75, 73, 65, 49, 50, 51, 52, 53, 54, 55, 56, 57, 48, 65, 66, 67, 68, 69, 70
    )
    temp = Path(tempfile.mkdtemp(prefix="secret-scan-fixture-"))
    try:
        sample = temp / "sample.txt"
        lines = list(terms) + [email, phone, server_ip, api_key]
        sample.write_text("\n".join(lines) + "\n", encoding="utf-8")

        scan = subprocess.run(
            [sys.executable, str(SCAN), str(temp)],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if scan.returncode == 0:
            print("secret scan accepted the fixture", file=sys.stderr)
            return 1
        if scan.returncode != 1:
            print(f"secret scan failed to run (exit {scan.returncode})", file=sys.stderr)
            return 1
        found = set()
        for line in scan.stdout.splitlines():
            if "category=" in line:
                found.add(line.rsplit("category=", 1)[1].strip())
        # Category names only. Never print the fixture or the scanner stdout.
        required = {"denylist", "email", "phone", "server-ip", "api-key"}
        missing = sorted(required - found)
        if missing:
            print("fixture was missing categories: " + ", ".join(missing), file=sys.stderr)
            return 1
        denylist_hits = sum(1 for line in scan.stdout.splitlines() if "category=denylist" in line)
        if denylist_hits < len(terms):
            print("fixture did not trip every denylist entry", file=sys.stderr)
            return 1

        try:
            leaks = subprocess.run(
                [
                    "gitleaks",
                    "detect",
                    "--no-git",
                    "--source",
                    str(temp),
                    "--redact=100",
                    "--no-banner",
                    "--exit-code",
                    "1",
                ],
                cwd=temp,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            print("gitleaks is not on PATH", file=sys.stderr)
            return 1
        if leaks.returncode == 0:
            print("gitleaks accepted the fixture", file=sys.stderr)
            return 1
        if leaks.returncode != 1:
            print(f"gitleaks failed to run (exit {leaks.returncode})", file=sys.stderr)
            return 1
        print("fixture rejected")
        return 0
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
