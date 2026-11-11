"""Lock the secret-scan workflow so a later job cannot weaken it."""
from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / ".github" / "workflows" / "secrets.yml"
DENYLIST = ROOT / "scripts" / "denylist.b64"
GUARDED = (
    ROOT / ".github",
    ROOT / "docs" / "threat-model.md",
    ROOT / "tests",
)


def _terms() -> list[str]:
    terms: list[str] = []
    for line in DENYLIST.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        terms.append(base64.b64decode(raw, validate=True).decode("utf-8"))
    assert terms, "denylist is empty"
    return terms


def _guarded_files() -> list[Path]:
    files: list[Path] = []
    for item in GUARDED:
        if item.is_file():
            files.append(item)
        else:
            files.extend(path for path in item.rglob("*") if path.is_file())
    return files


def test_only_the_docker_bridge_example_is_allowed(tmp_path: Path) -> None:
    good = "172.17.0." + "1"
    other = "172.17.0." + "2"
    sample = tmp_path / "note.txt"
    sample.write_text(good + "\n" + other + "\n", encoding="utf-8")
    scan = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "secret_scan.py"), str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert scan.returncode == 1
    hits = [line for line in scan.stdout.splitlines() if "category=server-ip" in line]
    assert hits == ["note.txt:2 category=server-ip"]


def test_secret_scan_job_is_strict() -> None:
    text = SECRETS.read_text(encoding="utf-8")
    required = (
        "python3 scripts/secret_scan.py .",
        "gitleaks detect --source . --redact=100 --no-banner --exit-code 1",
        "gitleaks detect --no-git --source . --redact=100 --no-banner --exit-code 1",
        "python3 scripts/secret_scan_fixture.py",
        "contents: read",
    )
    missing = [line for line in required if line not in text]
    assert not missing, "secrets workflow is missing: " + ", ".join(missing)
    assert "continue-on-error" not in text
    assert "|| true" not in text


def test_added_files_have_no_denylist_plaintext() -> None:
    terms = _terms()
    for path in _guarded_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        lowered = text.lower()
        for term in terms:
            hit = term.lower() in lowered if term.isascii() else term in text
            assert not hit, f"{path.relative_to(ROOT)}: category=denylist"
