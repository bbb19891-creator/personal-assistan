"""The threat model must keep the five pilot properties."""
from pathlib import Path

DOC = Path(__file__).resolve().parents[1] / "docs" / "threat-model.md"

REQUIRED = (
    "## Month-1 access",
    "## Release",
    "## Support-access expiry",
    "## Manager data deletion",
    "## Self-host",
    "Zero outbound manager calls",
    "hard-disabled",
    "The bot process stays up",
    "20 USD",
    "1, 4, 24, or 72 hours",
    "status history, costs, requests, and invite codes",
    "PUBLIC_INSTALL_URL",
    "https://example.invalid/install.sh",
    "does not report",
    "pkg/agent/tests",
    "central/tests",
    "platform/tests",
    "pkg/host/check-selfhost.sh",
)


def test_threat_model_covers_the_pilot_properties() -> None:
    text = DOC.read_text(encoding="utf-8")
    missing = [item for item in REQUIRED if item not in text]
    assert not missing, "threat model is missing: " + ", ".join(missing)
    assert "not claim the imported baseline already" in text
