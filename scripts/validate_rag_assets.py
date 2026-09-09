"""只读验收 RAG v1.1 Shadow 资产，不改写输入文件。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.loader import RagAssetLoader  # noqa: E402
from app.rag.emergency_shadow import V14EmergencyShadowMatcher  # noqa: E402


def default_asset_path() -> Path:
    return PROJECT_ROOT / "assets" / "rag" / "v1_8"


def build_report(asset_path: Path, *, require_production: bool = False) -> dict:
    report = RagAssetLoader(asset_path).load()
    pending_reviews = sum(card.get("review", {}).get("status") == "pending" for card in report.cards)
    production_eligible = sum(card.get("production_eligible") is True for card in report.cards)
    errors = list(report.errors)
    emergency_matcher = V14EmergencyShadowMatcher.load(asset_path, report.asset_format)
    emergency_rules = (
        emergency_matcher.report.rules if report.asset_format in ("v1_4", "v1_5", "v1_6", "v1_7", "v1_8") else ()
    )
    if report.asset_format in ("v1_4", "v1_5", "v1_6", "v1_7"):
        errors.extend(emergency_matcher.report.errors)
    pending_emergency_reviews = sum(
        (rule.get("veterinary_review") or {}).get("status") == "pending"
        for rule in emergency_rules
    )
    production_emergency_rules = sum(
        rule.get("production_eligible") is True for rule in emergency_rules
    )
    total_records = len(report.cards) + len(emergency_rules)
    total_production_eligible = production_eligible + production_emergency_rules
    if require_production and total_production_eligible != total_records:
        errors.append("production_gate_failed")
    return {
        "asset_path": str(asset_path),
        "asset_format": report.asset_format,
        "index_version": report.index_version,
        "card_count": len(report.cards),
        "emergency_rule_count": len(emergency_rules),
        "total_record_count": total_records,
        "source_count": len(report.source_ids),
        "pending_card_reviews": pending_reviews,
        "pending_emergency_reviews": pending_emergency_reviews,
        "production_eligible_cards": production_eligible,
        "production_eligible_emergency_rules": production_emergency_rules,
        "shadow_ready": report.ready,
        "production_ready": (
            not errors
            and total_records > 0
            and total_production_eligible == total_records
        ),
        "errors": list(dict.fromkeys(errors)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=default_asset_path())
    parser.add_argument("--require-production", action="store_true")
    args = parser.parse_args()
    result = build_report(args.assets, require_production=args.require_production)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["shadow_ready"] and not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())