"""Private, provider-independent state and scoring for recall admission studies.

The SQLite files created here are new evaluation artifacts, never runtime data.
Reservations survive crashes and are shared with a subsequent fusion study.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import random
import sqlite3
import time
import uuid


MODEL = "text-embedding-3-small"
DIMENSIONS = 512
NORMALIZATION = "aigan_clip_text_4000_l2_v1"
NANO_USD_PER_TOKEN = 20
SHARED_CAP_NANO_USD = 5_000_000_000
BASELINE_SHA = "a0d5028bab91ba64daff379375b1f52a46c9dd0b"
PROTOCOL = {
    "version": "recall-admission-eval-v1", "baseline_sha": BASELINE_SHA,
    "model": MODEL, "dimensions": DIMENSIONS, "normalization": NORMALIZATION,
    "provider_concurrency": 1, "max_retries": 0, "timeout_seconds": 45,
    "api_retry_timeout_parity": False, "shared_budget_usd": 5,
    "price_usd_per_million_input_tokens": .02,
    "reservation": "utf8_bytes_plus_1024_tokens_per_request",
    "prefetch": "all_unique_fixture_prompts_and_existing_archetypes_before_cache_only_paired_replay",
    "bootstrap_repetitions": 10000, "bootstrap_seed": 179,
    "bootstrap_unit": "class_stratified_concept_family_all_three_languages",
    "gates": {"tp_min": 33, "tp_gain_min": 8, "fp_max": 1,
              "critical_fp_max": 0, "boundary_violations_max": 0},
    "label_authority": "independent_agent_synthetic", "human_gold": False,
    "boundary_scope": "actual_text_message_invocation_admission_with_downstream_spy",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_private_json(path, value):
    """Exclusive creation: evidence is never silently replaced."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def private_directory(path, *, repository=None, create=False):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("private_path_must_not_be_symlink")
    if repository is not None and path.resolve().is_relative_to(Path(repository).resolve()):
        raise ValueError("private_artifact_inside_repository")
    if create:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
    if not path.is_dir() or (os.name != "nt" and path.stat().st_mode & 0o077):
        raise ValueError("private_directory_requires_owner_only_permissions")
    return path


def provider_input(text):
    text = " ".join(text.split())
    return text if len(text) <= 4000 else text[:3976].rstrip() + " [trimmed]"


def vector_key(text):
    return digest([MODEL, DIMENSIONS, NORMALIZATION, provider_input(text)])


def valid_vector(vector):
    if len(vector) != DIMENSIONS or any(not math.isfinite(x) for x in vector):
        raise ValueError("invalid_embedding_shape_or_number")
    length = math.sqrt(sum(x * x for x in vector))
    if length <= 0:
        raise ValueError("zero_embedding")
    return [x / length for x in vector]


class BudgetRefused(RuntimeError):
    pass


class EvaluationState:
    """Atomic shared spending admission and content-addressed embedding cache."""

    def __init__(self, directory, *, cap_nano_usd=SHARED_CAP_NANO_USD):
        if not 0 < cap_nano_usd <= SHARED_CAP_NANO_USD:
            raise ValueError("invalid_shared_cap")
        self.directory = private_directory(directory)
        self.path = self.directory / "provider-state.sqlite3"
        if self.path.is_symlink():
            raise ValueError("state_symlink")
        # Precreate with restrictive mode; SQLite sidecars inherit file mode.
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        if os.name != "nt" and self.path.stat().st_mode & 0o077:
            raise ValueError("state_requires_owner_only_permissions")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS state_identity(
                    id INTEGER PRIMARY KEY CHECK(id=1), ledger_uuid TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts(
                    id TEXT PRIMARY KEY, phase TEXT NOT NULL, request_hash TEXT NOT NULL,
                    reserved INTEGER NOT NULL, actual INTEGER, tokens INTEGER,
                    status TEXT NOT NULL, error_class TEXT, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS vectors(
                    key TEXT PRIMARY KEY, vector_json TEXT NOT NULL, vector_sha256 TEXT NOT NULL,
                    attempt_id TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO meta VALUES('cap',?)", (cap_nano_usd,))
            db.execute("INSERT OR IGNORE INTO meta VALUES('stopped',0)")
            db.execute("INSERT OR IGNORE INTO state_identity VALUES(1,?)", (uuid.uuid4().hex,))
            if db.execute("SELECT value FROM meta WHERE key='cap'").fetchone()[0] != cap_nano_usd:
                raise ValueError("shared_budget_cap_mismatch")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def identity(self):
        return state_identity(self.directory)

    def reserve(self, amount, *, phase, request_hash):
        if not isinstance(amount, int) or amount <= 0:
            raise ValueError("invalid_reservation")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            settings = dict(db.execute("SELECT key,value FROM meta"))
            spent = db.execute("SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM attempts").fetchone()[0]
            if settings["stopped"] or spent + amount > settings["cap"]:
                raise BudgetRefused("shared_provider_budget_refused")
            # Also serialize API calls across processes/future study consumers.
            if db.execute("SELECT 1 FROM attempts WHERE status='pending'").fetchone():
                raise BudgetRefused("pending_provider_attempt_requires_disposition")
            token = uuid.uuid4().hex
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,NULL,NULL,'pending',NULL,?)",
                       (token, phase, request_hash, amount, time.time()))
        return token

    def settle(self, token, *, tokens=None, error_class=None, cost_nano_usd=None):
        if tokens is not None and (not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0):
            raise ValueError("invalid_provider_usage")
        if cost_nano_usd is not None and (
            tokens is None or not isinstance(cost_nano_usd, int)
            or isinstance(cost_nano_usd, bool) or cost_nano_usd < 0
        ):
            raise ValueError("invalid_accounted_provider_cost")
        overrun = False
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT reserved,status FROM attempts WHERE id=?", (token,)).fetchone()
            if row is None or row[1] != "pending":
                raise ValueError("attempt_not_pending")
            # Embedding calls keep their exact historic rate. Other independently
            # priced trial providers supply their observed integer nano-USD cost.
            actual = None if tokens is None else (
                tokens * NANO_USD_PER_TOKEN if cost_nano_usd is None else cost_nano_usd
            )
            db.execute("UPDATE attempts SET actual=?,tokens=?,status=?,error_class=? WHERE id=?",
                       (actual, tokens, "unknown" if tokens is None else "known", error_class, token))
            overrun = actual is not None and actual > row[0]
            if overrun:
                db.execute("UPDATE meta SET value=1 WHERE key='stopped'")
        if overrun:
            raise BudgetRefused("actual_provider_cost_exceeded_reservation")

    def accounting(self):
        with self.connect() as db:
            counts = dict(db.execute("SELECT status,COUNT(*) FROM attempts GROUP BY status"))
            known, reserved = db.execute("SELECT COALESCE(SUM(actual),0),COALESCE(SUM(CASE WHEN actual IS NULL THEN reserved ELSE 0 END),0) FROM attempts").fetchone()
            cap = db.execute("SELECT value FROM meta WHERE key='cap'").fetchone()[0]
        return {"attempts": sum(counts.values()), "status_counts": counts,
                "known_usd": known / 1e9, "unknown_reserved_usd": reserved / 1e9,
                "cap_usd": cap / 1e9}

    def cached(self, text):
        with self.connect() as db:
            row = db.execute("SELECT vector_json,vector_sha256 FROM vectors WHERE key=?", (vector_key(text),)).fetchone()
        if row is None:
            return None
        values = json.loads(row[0])
        if digest(values) != row[1]:
            raise ValueError("cache_integrity_failure")
        valid_vector(values)
        if abs(sum(x*x for x in values) - 1) > 1e-6:
            raise ValueError("cache_vector_not_normalized")
        return values

    def put(self, text, vector, attempt_id):
        normalized = valid_vector(vector)
        with self.connect() as db:
            row = db.execute("SELECT status FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None or row[0] != "known":
                raise ValueError("cache_requires_accounted_provider_attempt")
            db.execute("INSERT OR IGNORE INTO vectors VALUES(?,?,?,?)",
                       (vector_key(text), json.dumps(normalized), digest(normalized), attempt_id))

    def cache_manifest(self):
        with self.connect() as db:
            stored = list(db.execute("SELECT key,vector_json,vector_sha256,attempt_id FROM vectors ORDER BY key"))
        rows = []
        for key, serialized, checksum, attempt in stored:
            vector = json.loads(serialized)
            if digest(vector) != checksum:
                raise ValueError("cache_integrity_failure")
            valid_vector(vector)
            if abs(sum(x*x for x in vector) - 1) > 1e-6:
                raise ValueError("cache_vector_not_normalized")
            rows.append([key, checksum, attempt])
        return {"model": MODEL, "dimensions": DIMENSIONS, "normalization": NORMALIZATION,
                "count": len(rows), "entries_sha256": digest(rows), "entries": rows}

    def verify_cache_manifest(self, expected, *, allow_additions=False):
        """Preserve committed vectors while explicitly allowing the shared fusion cache to grow."""
        current = self.cache_manifest()
        entries = expected.get("entries")
        if (not isinstance(entries, list)
                or any(not isinstance(row, list) or len(row) != 3
                       or not all(isinstance(value, str) for value in row) for row in entries)
                or any(expected.get(key) != current[key] for key in ("model", "dimensions", "normalization"))
                or expected.get("count") != len(entries)
                or expected.get("entries_sha256") != digest(entries)):
            raise ValueError("invalid_committed_cache_manifest")
        committed = {row[0]: row[1:] for row in entries}
        available = {row[0]: row[1:] for row in current["entries"]}
        if len(committed) != len(entries) or any(available.get(key) != value for key, value in committed.items()):
            raise ValueError("committed_cache_entries_missing_or_changed")
        added = len(available) - len(committed)
        if added and not allow_additions:
            raise ValueError("unapproved_cache_additions")
        return {"committed_entries": len(committed), "current_entries": len(available),
                "additional_entries": added, "allow_additions": allow_additions,
                "committed_entries_unchanged": True}


class CachedEmbeddingProvider:
    def __init__(self, state, *, phase, client=None):
        self.state, self.phase, self.client = state, phase, client
        self.cache_hits = self.cache_misses = 0
        self.failure = None

    def embed(self, texts):
        if self.failure:
            raise RuntimeError("embedding_session_already_failed")
        inputs = [provider_input(text) for text in texts]
        missing = list(dict.fromkeys(text for text in inputs if self.state.cached(text) is None))
        self.cache_hits += len(inputs) - len(missing)
        self.cache_misses += len(missing)
        if missing:
            if self.client is None:
                self.failure = "offline_cache_miss"
                raise RuntimeError(self.failure)
            reservation = (sum(len(text.encode("utf-8")) for text in missing) + 1024) * NANO_USD_PER_TOKEN
            try:
                attempt = self.state.reserve(reservation, phase=self.phase, request_hash=digest([MODEL, DIMENSIONS, missing]))
            except Exception as exc:
                self.failure = type(exc).__name__
                raise
            settled = False
            try:
                response = self.client.embeddings.create(model=MODEL, dimensions=DIMENSIONS,
                                                         input=missing, encoding_format="float")
                usage = getattr(response, "usage", None)
                tokens = getattr(usage, "total_tokens", None)
                if not isinstance(tokens, int) or tokens < 0:
                    raise ValueError("provider_usage_unavailable")
                # Charge known usage before interpreting model identity or vectors.
                settled = True
                self.state.settle(attempt, tokens=tokens)
                if response.model != MODEL:
                    raise ValueError("provider_model_mismatch")
                data = sorted(response.data, key=lambda item: item.index)
                if [item.index for item in data] != list(range(len(missing))):
                    raise ValueError("provider_embedding_count_or_index_mismatch")
                vectors = [list(item.embedding) for item in data]
                for vector in vectors:
                    valid_vector(vector)  # Validate the entire batch before any cache writes.
                for text, vector in zip(missing, vectors):
                    self.state.put(text, vector, attempt)
            except BaseException as exc:
                self.failure = type(exc).__name__
                if not settled:
                    self.state.settle(attempt, error_class=type(exc).__name__)
                raise
        return [self.state.cached(text) for text in inputs]


def state_identity(directory):
    path = private_directory(directory).resolve()
    with sqlite3.connect((path / "provider-state.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT ledger_uuid FROM state_identity WHERE id=1").fetchone()
    if row is None or not isinstance(row[0], str) or len(row[0]) != 32:
        raise ValueError("invalid_shared_state_identity")
    return {"state_directory_sha256": digest(str(path)), "state_ledger_uuid": row[0]}


def claim_holdout(directory, authorization, bindings):
    """Call before parsing a held-out payload; a failed run retains its claim."""
    required = {"schema_version": "recall179-holdout-authorization-v1",
                "authorized": True, "scope": "one_holdout_run_no_tuning"}
    if any(authorization.get(key) != value for key, value in required.items()):
        raise ValueError("invalid_holdout_authorization")
    if not authorization.get("nonce") or any(authorization.get(key) != value for key, value in bindings.items()):
        raise ValueError("holdout_authorization_binding_mismatch")
    if state_identity(directory) != {key: bindings.get(key) for key in
                                     ("state_directory_sha256", "state_ledger_uuid")}:
        raise ValueError("holdout_state_binding_mismatch")
    path = Path(directory) / ("holdout-" + bindings["fixture_sha256"] + ".claim.json")
    write_private_json(path, {"authorization_sha256": digest(authorization), "bindings": bindings,
                              "nonce": authorization["nonce"], "claimed_at": time.time()})
    return path


def validate_fixture(fixture):
    if fixture.get("schema_version") != "recall-admission-fixture-v1" or fixture.get("baseline_sha") != BASELINE_SHA:
        raise ValueError("fixture_schema_or_baseline_mismatch")
    if fixture.get("split") not in {"development", "holdout"} or fixture.get("human_gold") is not False:
        raise ValueError("fixture_split_or_label_authority_mismatch")
    cases = fixture["cases"]
    if len(cases) != 72 or len(fixture["boundary_cases"]) != 12:
        raise ValueError("fixture_population_mismatch")
    ids = [row["case_id"] for row in cases + fixture["boundary_cases"]]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate_case_id")
    families = defaultdict(list)
    for row in cases:
        families[row["family_id"]].append(row)
        if row["context"]["chat_type"] != "private" or not row["context"]["invoked"]:
            raise ValueError("efficacy_requires_admitted_private_context")
    if len(families) != 24 or any(Counter(r["language"] for r in rows) != Counter({"ua": 1, "ru": 1, "en": 1}) for rows in families.values()):
        raise ValueError("fixture_family_language_mismatch")
    if sum(row["expected"]["is_recall"] for row in cases) != 36:
        raise ValueError("fixture_class_balance_mismatch")
    if sum(row["expected"]["critical_negative"] for row in cases) != 15:
        raise ValueError("fixture_critical_count_mismatch")
    if any(row["expected"]["is_recall"] and row["expected"]["critical_negative"] for row in cases):
        raise ValueError("positive_labeled_critical_negative")


def confusion(rows, arm):
    counts = {key: 0 for key in ("tp", "fp", "tn", "fn", "critical_fp", "degraded")}
    for row in rows:
        positive, predicted = row["expected"], row[arm]["is_recall"]
        counts[("t" if positive == predicted else "f") + ("p" if predicted else "n")] += 1
        counts["critical_fp"] += int(row["critical_negative"] and predicted)
        counts["degraded"] += int(row[arm]["degraded"])
    counts["precision"] = counts["tp"] / (counts["tp"] + counts["fp"]) if counts["tp"] + counts["fp"] else None
    counts["recall"] = counts["tp"] / (counts["tp"] + counts["fn"]) if counts["tp"] + counts["fn"] else None
    negative_count = counts["tn"] + counts["fp"]
    counts["specificity"] = counts["tn"] / negative_count if negative_count else None
    counts["false_positive_rate"] = counts["fp"] / negative_count if negative_count else None
    return counts


def paired_intervals(rows, *, repetitions=10000, seed=179):
    families = defaultdict(list)
    for row in rows:
        families[row["family_id"]].append(row)
    keys, rng = sorted(families), random.Random(seed)
    strata = [[key for key in keys if families[key][0]["expected"] == value] for value in (True, False)]
    if any(len({row["expected"] for row in family}) != 1 for family in families.values()):
        raise ValueError("family_crosses_label_strata")
    samples = {name: defaultdict(list) for name in ("baseline", "candidate", "delta")}
    metrics = ("precision", "recall", "specificity", "false_positive_rate", "tp", "fp")
    for _ in range(repetitions):
        selected = [row for stratum in strata for _ in stratum for row in families[rng.choice(stratum)]]
        baseline, candidate = confusion(selected, "baseline"), confusion(selected, "candidate")
        for metric in metrics:
            for arm, values in (("baseline", baseline), ("candidate", candidate)):
                if values[metric] is not None:
                    samples[arm][metric].append(values[metric])
            if baseline[metric] is not None and candidate[metric] is not None:
                samples["delta"][metric].append(candidate[metric] - baseline[metric])
    def intervals(arm):
        result = {}
        for metric in metrics:
            values = sorted(samples[arm][metric])
            result[metric] = {"low": values[int(.025 * (len(values)-1))] if values else None,
                              "high": values[int(.975 * (len(values)-1))] if values else None,
                              "defined_repetitions": len(values)}
        return result
    return {"unit": "family", "stratified_by_expected_label": True, "families": len(keys),
            "repetitions": repetitions, "seed": seed,
            "per_arm": {arm: intervals(arm) for arm in ("baseline", "candidate")},
            "delta_candidate_minus_baseline": intervals("delta")}


def paired_case_outcomes(rows):
    """Descriptive wording-level wins/losses, not independent statistical trials."""
    result = {}
    for label, selected in (("all", rows), ("positive", [r for r in rows if r["expected"]]),
                            ("negative", [r for r in rows if not r["expected"]])):
        counts = {key: 0 for key in ("both_correct", "both_incorrect", "baseline_only_correct", "candidate_only_correct")}
        for row in selected:
            before = row["baseline"]["is_recall"] == row["expected"]
            after = row["candidate"]["is_recall"] == row["expected"]
            key = ("both_correct" if before else "both_incorrect") if before == after else (
                "baseline_only_correct" if before else "candidate_only_correct")
            counts[key] += 1
        result[label] = {"pairs": len(selected), **counts}
    return result


def summarize(rows, boundaries):
    baseline, candidate = confusion(rows, "baseline"), confusion(rows, "candidate")
    violations = sum(not row["passed"] for row in boundaries)
    return {"cases": len(rows), "families": len({row["family_id"] for row in rows}),
            "baseline": baseline, "candidate": candidate,
            "by_language": {lang: {arm: confusion([r for r in rows if r["language"] == lang], arm)
                                    for arm in ("baseline", "candidate")} for lang in ("ua", "ru", "en")},
            "paired_family_bootstrap": paired_intervals(rows),
            "paired_case_outcomes": paired_case_outcomes(rows),
            "boundary_cases": len(boundaries), "boundary_violations": violations,
            "gate_pass": len(rows) == 72 and len(boundaries) == 24 and candidate["tp"] >= 33
                         and candidate["tp"] - baseline["tp"] >= 8 and candidate["fp"] <= 1
                         and candidate["critical_fp"] == 0 and not violations
                         and not baseline["degraded"] and not candidate["degraded"]}
