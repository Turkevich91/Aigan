"""Small, provider-free helpers for the dimension development experiment."""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict


LABEL = "DEVELOPMENT_CONTROLLED_WITH_REAL_DISTRACTORS"
RATE = 0.02 / 1_000_000


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class Budget:
    """Reserve UTF-8 bytes as a conservative token bound, including failed calls."""
    def __init__(self, limit):
        self.limit = float(limit)
        self.reserved = 0.0
        self.actual = 0.0
        self.unknown = 0.0
        self.calls = 0
        self.stopped = False

    def reserve(self, amount):
        if self.stopped or amount < 0 or self.actual + self.unknown + self.reserved + amount > self.limit:
            raise ValueError("budget_exceeded")
        self.reserved += amount
        self.calls += 1

    def settle(self, reserved, actual=None):
        self.reserved -= reserved
        if actual is None:
            self.unknown += reserved
        else:
            self.actual += actual
            if actual > reserved + 1e-9:
                self.stopped = True
                raise ValueError("provider_usage_exceeded_reservation")

    def report(self):
        return {"limit_usd": self.limit, "known_usd": self.actual,
                "unknown_call_upper_bound_usd": self.unknown, "calls": self.calls}


def source_metrics(ranked, relevant):
    """Known-source hit metrics, not exhaustive relevance recall."""
    relevant = set(relevant)
    ranks = [i + 1 for i, source in enumerate(ranked) if source in relevant]
    best = min(ranks, default=0)
    result = {f"source_hit_at_{k}": float(bool(best and best <= k)) for k in (1, 6, 12)}
    result["reciprocal_rank"] = 1 / best if best else 0.0
    dcg = sum(1 / math.log2(rank + 1) for rank in ranks if rank <= 12)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(12, len(relevant))))
    result["known_source_ndcg_at_12"] = dcg / ideal if ideal else 0.0
    return result


def paired_family_interval(rows, metric, repetitions=2000):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["family"]].append(row["1536"][metric] - row["512"][metric])
    values = [sum(v) / len(v) for v in grouped.values()]
    if not values:
        return None
    rng = random.Random(176)
    samples = sorted(sum(rng.choice(values) for _ in values) / len(values) for _ in range(repetitions))
    return {"delta": sum(values) / len(values), "lower_95": samples[int(.025 * repetitions)],
            "upper_95": samples[min(repetitions - 1, int(.975 * repetitions))],
            "families": len(values), "method": "paired_family_bootstrap_descriptive_only"}


def validate_source_probe(result, source, title=""):
    if result.get("eligible") is not True:
        return "generator_ineligible"
    question = result.get("question", "")
    answer = result.get("answer_span", "")
    if not isinstance(question, str) or not isinstance(answer, str):
        return "invalid_types"
    if not 8 <= len(question) <= 240 or not 3 <= len(answer) <= 100:
        return "invalid_length"
    if answer not in source:
        return "answer_not_verbatim"
    if answer.casefold() in question.casefold():
        return "answer_leaked"
    if len(title.strip()) >= 4 and title.casefold() in question.casefold():
        return "title_leaked"
    return ""
