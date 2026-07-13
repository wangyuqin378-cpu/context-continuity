#!/usr/bin/env python3
"""Score handoff documents against the same protected-fact rubric."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


TRANSITION_WORDS = ("supersed", "replace", "changed", "->", "→")


def words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def token_normalize(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def has_fact_anchor(folded: str, normalized: str, anchor: str) -> bool:
    return anchor.casefold() in folded or token_normalize(anchor) in normalized


def has_transition(text: str, old: str, new: str) -> bool:
    folded = text.casefold()
    old_folded = old.casefold()
    new_folded = new.casefold()
    if new_folded not in folded:
        return False
    for line in folded.splitlines():
        if old_folded in line and any(word in line for word in TRANSITION_WORDS):
            return True
    return False


def score(path: Path, truth: dict, source_text: str, require_ids: bool) -> dict:
    text = path.read_text(encoding="utf-8")
    folded = text.casefold()
    normalized = token_normalize(text)
    missing_facts = []
    for fact in truth["facts"]:
        if not all(
            has_fact_anchor(folded, normalized, anchor)
            for anchor in fact["anchors"]
        ):
            missing_facts.append(fact["id"])

    stable_ids = [fact["id"] for fact in truth["facts"] if re.fullmatch(r"[A-Z]\d{3}", fact["id"])]
    missing_ids = [item_id for item_id in stable_ids if not re.search(rf"\b{item_id}\b", text)]
    missing_transitions = [
        f"{item['from']}->{item['to']}"
        for item in truth["required_transitions"]
        if not has_transition(text, item["from"], item["to"])
    ]
    noise_count = sum(folded.count(token.casefold()) for token in truth["noise_tokens"])
    output_words = words(text)
    source_words = words(source_text)
    fact_recall = (len(truth["facts"]) - len(missing_facts)) / len(truth["facts"])
    id_coverage = (len(stable_ids) - len(missing_ids)) / len(stable_ids)
    transition_recall = (
        len(truth["required_transitions"]) - len(missing_transitions)
    ) / len(truth["required_transitions"])
    compression = 1 - output_words / source_words
    byte_compression = 1 - len(text.encode()) / len(source_text.encode())
    thresholds = truth["thresholds"]
    passed = (
        fact_recall >= thresholds["fact_recall"]
        and noise_count <= thresholds["noise_count"]
        and compression >= thresholds["minimum_compression"]
        and byte_compression >= thresholds["minimum_compression"]
        and transition_recall == 1.0
        and (not require_ids or id_coverage == 1.0)
    )
    return {
        "file": str(path),
        "passed": passed,
        "fact_recall": round(fact_recall, 4),
        "id_coverage": round(id_coverage, 4),
        "transition_recall": round(transition_recall, 4),
        "compression": round(compression, 4),
        "byte_compression": round(byte_compression, 4),
        "source_words": source_words,
        "output_words": output_words,
        "noise_count": noise_count,
        "missing_facts": missing_facts,
        "missing_ids": missing_ids,
        "missing_transitions": missing_transitions,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("truth", type=Path)
    parser.add_argument("outputs", nargs="+", type=Path)
    parser.add_argument("--require-ids", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    source = args.truth.parent / truth["source"]
    source_text = source.read_text(encoding="utf-8")
    results = [score(path, truth, source_text, args.require_ids) for path in args.outputs]
    aggregate = {
        "passed": sum(item["passed"] for item in results),
        "trials": len(results),
        "mean_fact_recall": round(sum(item["fact_recall"] for item in results) / len(results), 4),
        "mean_compression": round(sum(item["compression"] for item in results) / len(results), 4),
        "mean_byte_compression": round(
            sum(item["byte_compression"] for item in results) / len(results), 4
        ),
    }
    print(json.dumps({"aggregate": aggregate, "results": results}, ensure_ascii=False, indent=2))
    return 1 if args.strict and aggregate["passed"] != aggregate["trials"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
