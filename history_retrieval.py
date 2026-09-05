"""Bounded retained-history ranking. No provider, Telegram or session side effects.

The caller owns authorization, embedding calls and publication budgets. Internal
snapshots contain original records solely to reuse the memory fusion policy;
only the allowlisted projection in HistoryRetrievalResult may be published.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
import struct
from typing import Sequence

from memory import MemorySearchBatch, MemoryStore, SemanticMemoryResult, fuse_memory_search_batches


HISTORY_SCAN_LIMIT = 8192
HISTORY_EMBEDDING_MODEL = "text-embedding-3-small"
HISTORY_EMBEDDING_DIMENSIONS = 512


def _date(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("History dates require a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class HistorySearchScope:
    """Host-owned scope; optional filters may only narrow its request boundary."""

    chat_id: int
    cutoff_memory_id: int
    cutoff_created_at: str
    participant_id: int | None = None
    after: str = ""
    before: str = ""
    authored_only: bool = False

    def __post_init__(self):
        for value in (self.chat_id, self.cutoff_memory_id, self.participant_id):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                      or not -(2 ** 63) <= value < 2 ** 63):
                raise ValueError("Invalid history identity")
        if self.cutoff_memory_id <= 0:
            raise ValueError("Invalid request boundary")
        for name in ("cutoff_created_at", "after", "before"):
            value = getattr(self, name)
            if value:
                object.__setattr__(self, name, _date(value))
        if not self.cutoff_created_at or self.after and self.before and self.after >= self.before:
            raise ValueError("Invalid history interval")


@dataclass(frozen=True)
class HistoryRetrievalCoverage:
    status: str
    scoped_rows: int
    indexed_rows: int
    unusable_index_rows: int
    scan_limit: int = HISTORY_SCAN_LIMIT
    retained_archive_only: bool = True
    complete_history: bool = False

    @property
    def can_embed(self) -> bool:
        return self.status == "ready" and self.indexed_rows > 0


@dataclass(frozen=True)
class HistoryRetrievalResult:
    """Rows remain strongest-first; trim from the end BEFORE chronological display."""

    rows: tuple[dict[str, object], ...]
    coverage: HistoryRetrievalCoverage
    status: str
    applied_mode: str
    embeddings_used: bool
    fusion_policy: str = ""
    fallback_reason: str = ""
    has_more_matching: bool = False
    # IDs only: host metadata, never substitute for successfully published IDs.
    relevance_order: tuple[int, ...] = ()


def normalize_history_vector(values: Sequence[float] | None) -> tuple[float, ...] | None:
    if values is None or len(values) != HISTORY_EMBEDDING_DIMENSIONS:
        return None
    try:
        vector = tuple(float(value) for value in values)
        if not all(math.isfinite(value) for value in vector):
            return None
        norm = math.sqrt(math.fsum(value * value for value in vector))
        if not math.isfinite(norm) or norm <= 0:
            return None
        return tuple(value / norm for value in vector)
    except (TypeError, ValueError, OverflowError):
        return None


def _read_candidates(store: MemoryStore, scope: HistorySearchScope, query: str = ""):
    return store.read_history_retrieval_candidates(
        chat_id=scope.chat_id, cutoff_memory_id=scope.cutoff_memory_id,
        cutoff_created_at=scope.cutoff_created_at, participant_id=scope.participant_id,
        after=scope.after, before=scope.before, authored_only=scope.authored_only,
        scan_limit=HISTORY_SCAN_LIMIT, query=query,
    )


def _candidate_vector(store: MemoryStore, scope: HistorySearchScope, item, metadata):
    """One shared validity contract for admission and final ranked evidence."""
    if metadata["embedding_model"] is None:
        return None
    try:
        canonical = store.searchable_text_for_item(item)
        blob = metadata["embedding_blob"]
        valid = (
            metadata["embedding_chat_id"] == scope.chat_id
            and metadata["embedding_model"] == HISTORY_EMBEDDING_MODEL
            and metadata["embedding_dimensions"] == HISTORY_EMBEDDING_DIMENSIONS
            and metadata["embedding_hash"] == store.content_hash(canonical)
            and canonical and not item.is_bot
            and _date(metadata["embedded_at"]) <= scope.cutoff_created_at
            and isinstance(blob, bytes) and len(blob) == HISTORY_EMBEDDING_DIMENSIONS * 4
            and (not scope.authored_only or canonical == " ".join(item.text.split()))
        )
        if valid:
            return normalize_history_vector(struct.unpack(f"<{HISTORY_EMBEDDING_DIMENSIONS}f", blob))
    except (TypeError, ValueError, struct.error, AttributeError):
        pass
    return None


def history_query_embedding_available(store: MemoryStore, scope: HistorySearchScope) -> bool:
    """Provider admission only: cap-check the whole scope, stop at a valid vector.

    This is not an index-coverage count and does not authorize any source. Final
    retrieval still re-reads and validates every candidate after the provider
    await. The diagnostic preflight API below retains its complete counts.
    """
    rows, overflow, _, _ = _read_candidates(store, scope)
    return not overflow and any(_candidate_vector(store, scope, item, metadata) is not None
                                for item, metadata in rows)


def _read_snapshot(store: MemoryStore, scope: HistorySearchScope, query: str = ""):
    rows, overflow, fts_scores, fts_available = _read_candidates(store, scope, query)
    if overflow:
        return (), HistoryRetrievalCoverage("scope_too_large", HISTORY_SCAN_LIMIT + 1, 0, 0), {}, False
    candidates = []
    indexed = unusable = 0
    for item, metadata in rows:
        vector = _candidate_vector(store, scope, item, metadata)
        if metadata["embedding_model"] is not None:
            if vector is None:
                unusable += 1
        if vector is not None:
            indexed += 1
        # Lexical evidence is restricted to fields the model can actually inspect.
        lexical = item.text if scope.authored_only else " ".join((item.text, item.source_text, item.vision_summary))
        candidates.append((item, lexical, vector, metadata["sort_time"], metadata["evidence_digest"]))
    status = "ready" if indexed else "no_usable_index" if rows else "empty_scope"
    return tuple(candidates), HistoryRetrievalCoverage(status, len(rows), indexed, unusable), fts_scores, fts_available


def preflight_history_retrieval(store: MemoryStore, scope: HistorySearchScope) -> HistoryRetrievalCoverage:
    """Aggregate-only check before reserving a provider call; never caches content."""
    return _read_snapshot(store, scope)[1]


def _project(item, *, authored_only: bool, sort_time: float, evidence_digest: str) -> dict[str, object]:
    return {
        "id": item.id, "message_id": item.message_id, "user_id": item.user_id,
        "created_at": item.created_at, "sender_label": item.sender_label[:97],
        "text": item.text[:1001], "source_text": "" if authored_only else item.source_text[:1001],
        "attachment_type": item.attachment_type[:49],
        "attachment_summary": "" if authored_only else item.vision_summary[:1001],
        "reply_to_message_id": item.reply_to_message_id, "sort_time": sort_time,
        "is_forwarded": bool(item.forward_origin), "evidence_digest": evidence_digest,
    }


def retrieve_history(
    store: MemoryStore, *, scope: HistorySearchScope, query: str,
    query_vector: Sequence[float] | None = None, mode: str = "hybrid", limit: int = 10,
) -> HistoryRetrievalResult:
    """Read a fresh snapshot AFTER any provider await, then rank without I/O.

    Semantic neighbors have no calibrated answerability threshold. Nonempty
    results mean nearest retained evidence, never proof that an answer exists.
    Missing/invalid embeddings fall back to literal AND-term search explicitly.
    Explicit dates govern this read; the ordinary 30-day prefetch is untouched.
    """
    if mode not in {"semantic", "hybrid"}:
        raise ValueError("Unknown semantic history mode")
    query = str(query).strip()
    if not query or len(query) > 256:
        raise ValueError("History query must contain 1 to 256 characters")
    limit = max(1, min(20, int(limit)))
    candidates, coverage, fts_scores, fts_available = _read_snapshot(store, scope, query)
    if coverage.status == "scope_too_large":
        return HistoryRetrievalResult((), coverage, "scope_too_large", mode, False)
    vector = normalize_history_vector(query_vector)
    use_vectors = vector is not None and coverage.indexed_rows > 0
    terms = tuple(dict.fromkeys(re.findall(r"[^\W_]+", query.casefold(), flags=re.UNICODE)))[:8]
    keyword = []
    semantic = []
    fts = []
    times = {}
    digests = {}
    for item, lexical, candidate_vector, sort_time, digest in candidates:
        times[item.id] = sort_time
        digests[item.id] = digest
        if terms and all(term in lexical.casefold() for term in terms):
            keyword.append(SemanticMemoryResult(item, lexical, 100., "keyword"))
        if use_vectors and candidate_vector is not None:
            score = math.fsum(a * b for a, b in zip(vector, candidate_vector))
            semantic.append(SemanticMemoryResult(item, lexical, score, "semantic"))
        if item.id in fts_scores and math.isfinite(fts_scores[item.id]):
            fts.append(SemanticMemoryResult(item, lexical, fts_scores[item.id], "fts"))
    keyword.sort(key=lambda result: (times[result.item.id], result.item.id), reverse=True)
    keyword = [SemanticMemoryResult(result.item, result.search_text, 100. - i * .001, "keyword")
               for i, result in enumerate(keyword)]
    semantic.sort(key=lambda result: (-result.score, result.item.id))
    fts.sort(key=lambda result: (-result.score, result.item.id))
    policy = reason = ""
    if not use_vectors:
        ranked = keyword
        applied_mode = "lexical_fallback"
        reason = "no_usable_index" if not coverage.indexed_rows else "query_embedding_unavailable"
    elif mode == "semantic":
        ranked, applied_mode = semantic, "semantic"
    else:
        source_limit = min(40, limit * 2)
        outcome = fuse_memory_search_batches(
            [MemorySearchBatch("keyword", keyword[:source_limit]),
             MemorySearchBatch("semantic", semantic[:source_limit]),
             MemorySearchBatch("fts", fts[:source_limit])],
            limit=limit + 1, policy="rrf", protect_numeric=bool(re.search(r"(?<!\w)\d+(?!\w)", query)),
        )
        ranked, policy, reason = outcome.results, outcome.applied_policy, outcome.fallback_reason
        if not fts_available and not reason:
            reason = "fts_unavailable"
        applied_mode = "hybrid"
    selected = ranked[:limit]
    rows = tuple(_project(result.item, authored_only=scope.authored_only, sort_time=times[result.item.id],
                          evidence_digest=digests[result.item.id])
                 for result in selected)
    return HistoryRetrievalResult(rows, coverage, "ok" if rows else "no_match", applied_mode,
                                  use_vectors, policy, reason, len(ranked) > limit,
                                  tuple(result.item.id for result in selected))
