"""Evaluate the v1.4 lexical Shadow retriever on seed and safety probes."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.rag.loader import RagAssetLoader  # noqa: E402
from app.rag.emergency_shadow import V14EmergencyShadowMatcher  # noqa: E402
from app.rag.retriever import ShadowRetriever  # noqa: E402

DEFAULT_ASSETS = PROJECT_ROOT / "assets" / "rag" / "v1_8"

REJECTION_PROBES = (
    ("我家猫怎么了", "cat"),
    ("狗正常吃饭喝水", "dog"),
    ("今天天气不错", "cat"),
    ("你好", "dog"),
)


def evaluate(asset_path: Path, *, threshold: float = 0.24) -> dict:
    report = RagAssetLoader(asset_path).load()
    if not report.ready:
        return {"shadow_ready": False, "errors": list(report.errors)}
    retriever = ShadowRetriever(report, top_k=4, threshold=threshold)
    benchmark_name = {
        "v1_4": "benchmark_seed.v1_4.csv",
        "v1_5": "benchmark_seed.v1_5.csv",
        "v1_6": "benchmark_seed.v1_6.csv",
        "v1_7": "benchmark_seed.v1_7.csv",
        "v1_8": "benchmark_seed.v1_8.csv",
    }.get(report.asset_format, "benchmark_seed.v1_4.csv")
    benchmark_path = asset_path / benchmark_name
    rows = list(csv.DictReader(benchmark_path.open(encoding="utf-8", newline="")))
    top1 = 0
    top4 = 0
    reciprocal_rank = 0.0
    species_mismatches = 0
    card_species = {card["id"]: set(card.get("species", [])) for card in report.cards}
    for row in rows:
        species = row["species"].split("|")[0]
        result = retriever.search(row["query_text"], species=species)
        hit_ids = [hit.card_id for hit in result.hits]
        expected = row["expected_card_id"]
        if hit_ids and hit_ids[0] == expected:
            top1 += 1
        if expected in hit_ids:
            rank = hit_ids.index(expected) + 1
            top4 += 1
            reciprocal_rank += 1 / rank
        species_mismatches += sum(
            species not in card_species.get(card_id, set()) for card_id in hit_ids
        )

    rejected = 0
    rejection_details = []
    for query, species in REJECTION_PROBES:
        result = retriever.search(query, species=species)
        is_rejected = result.decision.value == "insufficient"
        rejected += int(is_rejected)
        rejection_details.append(
            {
                "query": query,
                "species": species,
                "decision": result.decision.value,
                "top_score": result.top_score,
            }
        )
    emergency_matcher = V14EmergencyShadowMatcher.load(asset_path, report.asset_format)
    trigger_total = 0
    trigger_hits = 0
    negation_false_positives = 0
    species_false_positives = 0
    for rule in emergency_matcher.report.rules:
        species = rule["species"][0]
        other_species = "dog" if species == "cat" else "cat"
        for trigger in rule["triggers"]:
            trigger_total += 1
            matched = emergency_matcher.search(trigger, species=species).matched_rule_ids
            trigger_hits += int(rule["id"] in matched)
            negated = emergency_matcher.search(
                f"目前没有{trigger}", species=species
            ).matched_rule_ids
            negation_false_positives += int(rule["id"] in negated)
            if len(rule["species"]) == 1:
                wrong_species = emergency_matcher.search(
                    trigger, species=other_species
                ).matched_rule_ids
                species_false_positives += int(rule["id"] in wrong_species)
    count = len(rows)
    return {
        "shadow_ready": True,
        "asset_version": report.index_version,
        "threshold": threshold,
        "seed_benchmark": {
            "queries": count,
            "recall_at_1": top1 / count if count else 0.0,
            "recall_at_4": top4 / count if count else 0.0,
            "mrr_at_4": reciprocal_rank / count if count else 0.0,
            "species_mismatches": species_mismatches,
            "limitation": (
                "Seed queries are copied from card titles/user_phrases; "
                "these metrics are regression checks, not real-user performance."
            ),
        },
        "rejection_probes": {
            "passed": rejected,
            "total": len(REJECTION_PROBES),
            "details": rejection_details,
        },
        "emergency_shadow": {
            "rules": len(emergency_matcher.report.rules),
            "trigger_phrases": trigger_total,
            "trigger_recall": trigger_hits / trigger_total if trigger_total else 0.0,
            "negation_false_positives": negation_false_positives,
            "species_false_positives": species_false_positives,
            "limitation": (
                "Exact asset trigger phrases are regression probes; paraphrase recall and "
                "real-world false-positive rates remain unmeasured."
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, default=DEFAULT_ASSETS)
    parser.add_argument("--threshold", type=float, default=0.24)
    args = parser.parse_args()
    result = evaluate(args.assets, threshold=args.threshold)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("shadow_ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())