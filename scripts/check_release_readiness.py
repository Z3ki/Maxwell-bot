"""Fail closed before publishing a candidate with unfinished operator review."""

from pathlib import Path
import json
import sys

REQUIRED = (
    "operator_identity",
    "contract_and_law",
    "private_support_test",
    "discord_portal_links",
    "provider_inventory_and_terms",
    "lawful_bases_and_transfers",
    "age_eligibility",
    "encryption_at_rest",
    "log_and_backup_retention",
    "workspace_plugin_deletion",
    "backup_restore_suppression",
    "premium_disabled_or_billing_terms",
)


def release_blockers(root):
    try:
        data = json.loads((root / "release-readiness.json").read_text())
    except (OSError, ValueError):
        return ["Missing or unreadable release review"]
    blockers = []
    if data.get("candidate") != (root / "VERSION").read_text().strip():
        blockers.append("Review does not match VERSION")
    if data.get("reviewed") is not True or data.get("status") != "approved":
        blockers.append("Operator review is not approved")
    evidence = data.get("evidence", {})
    for name in REQUIRED:
        value = evidence.get(name) if isinstance(evidence, dict) else None
        if (
            not isinstance(value, str)
            or len(value.strip()) < 12
            or any(
                word in value.lower()
                for word in ("todo", "placeholder", "pending", "tbd")
            )
        ):
            blockers.append("Missing reviewed evidence: " + name)
    if data.get("blockers"):
        blockers.extend(str(item) for item in data["blockers"])
    for name in ("terms", "privacy", "contact"):
        text = (root / "web" / name / "index.html").read_text()
        if (
            "review pending" in text.lower()
            or "release candidate has not completed" in text.lower()
            or "[Governing law:" in text
        ):
            blockers.append(f"{name} page still marked for operator review")
    return blockers


if __name__ == "__main__":
    problems = release_blockers(Path(__file__).resolve().parents[1])
    if problems:
        print("Release publication blocked:\n" + "\n".join("- " + p for p in problems))
        sys.exit(1)
    print("Operator evidence supplied; publication may proceed after CI.")
