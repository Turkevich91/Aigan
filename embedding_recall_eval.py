"""Provider-free orchestration for a small dimension-sensitive intent screen."""
from __future__ import annotations
import asyncio
from dataclasses import replace


def summarize(rows):
    """Failed/degraded provider results are not counted as quality observations."""
    result = {}
    for dim in (512, 1536):
        selected = [r for r in rows if r["dimensions"] == dim]
        valid = [r for r in selected if not r["degraded"]]
        result[str(dim)] = {
            "cases": len(selected), "non_degraded": len(valid),
            "correct": sum(r["observed_recall"] == r["expected_recall"] for r in valid),
            "false_positive": sum(r["observed_recall"] and not r["expected_recall"] for r in valid),
            "false_negative": sum(not r["observed_recall"] and r["expected_recall"] for r in valid),
            "failures": len(selected) - len(valid),
            "administrative_aborts": sum(bool(r.get("administrative_abort")) for r in selected),
        }
    paired = {}
    for row in rows:
        paired.setdefault(row["id"], {})[row["dimensions"]] = row
    complete = [pair for pair in paired.values()
                if set(pair) == {512, 1536} and not any(r["degraded"] for r in pair.values())]
    result["paired"] = {
        "complete_cases": len(complete),
        "changed_decisions": sum(p[512]["observed_recall"] != p[1536]["observed_recall"] for p in complete),
        "candidate_improved": sum(p[512]["observed_recall"] != p[512]["expected_recall"]
                                  and p[1536]["observed_recall"] == p[1536]["expected_recall"] for p in complete),
        "candidate_regressed": sum(p[512]["observed_recall"] == p[512]["expected_recall"]
                                   and p[1536]["observed_recall"] != p[1536]["expected_recall"] for p in complete),
    }
    return result


def evaluate(app, embed, config, cases):
    """Call the actual recall-intent component using a budgeted embed callback.

    embed(texts, dimensions, stage) returns real provider vectors. The caller
    owns API budget, privacy, failure capture and the isolated memory store.
    No Telegram application is started and no memory record is written.
    """
    from tests.support import FakeMessage
    if app.MEMORY is None:
        raise ValueError("isolated_memory_required")
    ids = [c["id"] for c in cases]
    if len(set(ids)) != len(ids) or any(type(c["expected_recall"]) is not bool for c in cases):
        raise ValueError("invalid_intent_fixture")
    old_config, old_create = app.CONFIG, app.create_embeddings
    old_cache = app.recall_intent_embedding_cache
    old_error = app.last_embedding_error
    app.recall_intent_embedding_cache = {}
    rows = []
    try:
        for index, case in enumerate(cases):
            for dim in ((512, 1536) if index % 2 == 0 else (1536, 512)):
                app.CONFIG = replace(config, memory_embedding_dimensions=dim)
                administrative_abort = False
                async def create(texts, **kwargs):
                    nonlocal administrative_abort
                    try:
                        return embed(texts, dim, "recall_intent")
                    except Exception as exc:
                        if str(exc) in {"budget_exceeded", "budget_stopped", "provider_usage_exceeded_reservation"}:
                            administrative_abort = True
                        raise
                app.create_embeddings = create
                observed = asyncio.run(app.detect_memory_recall_intent(
                    FakeMessage(case["text"], chat_type="private"), case["text"]))
                rows.append({"id": case["id"], "language": case["language"], "dimensions": dim,
                             "expected_recall": case["expected_recall"],
                             "observed_recall": bool(observed.is_recall),
                             "degraded": bool(observed.degraded), "reason": observed.reason,
                             "administrative_abort": administrative_abort,
                             "confidence": float(observed.confidence)})
    finally:
        app.CONFIG, app.create_embeddings = old_config, old_create
        app.recall_intent_embedding_cache = old_cache
        app.last_embedding_error = old_error
    return {
        "label": "SYNTHETIC_RECALL_INTENT_COMPONENT_SCREEN",
        "thresholds": {"strong": config.memory_recall_intent_threshold,
                       "ambiguous": config.memory_recall_intent_ambiguous_threshold},
        "metrics": summarize(rows), "cases": rows,
        "limitations": ["This is a small synthetic component screen, not field calibration.",
                        "It measures recall-intent decisions, not the entire application route.",
                        "No threshold tuning or runtime promotion is performed."],
    }
