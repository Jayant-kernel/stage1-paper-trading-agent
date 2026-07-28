from __future__ import annotations

import hashlib
import json
import math
import os
import random
import statistics
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import polars as pl


FactorFunction = Callable[[list[dict[str, Any]], int], float | None]
_REQUIRED = {
    "symbol",
    "session_date",
    "dataset_split",
    "high",
    "low",
    "close",
    "volume",
}


@dataclass(frozen=True)
class FactorSpec:
    factor_id: str
    description: str
    minimum_history: int
    decision_timing: str = "NEXT_SESSION_ONLY"


@dataclass(frozen=True)
class CausalityAudit:
    factor_count: int
    audited_rows: int
    cutoff_session: str
    passed: bool


@dataclass(frozen=True)
class FactorEvaluation:
    summary: pl.DataFrame
    factor_panel: pl.DataFrame
    sealed_test_rows: int


@dataclass(frozen=True)
class FactorResearchArtifact:
    panel_relative_path: str
    panel_sha256: str
    summary_relative_path: str
    summary_sha256: str
    report_relative_path: str
    report_sha256: str


class LookaheadViolation(RuntimeError):
    """Raised when future-row perturbation changes an earlier factor value."""


class FactorRegistry:
    """Small immutable registry of explainable, paper-research-only factors."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[FactorSpec, FactorFunction]] = {}

    def register(self, spec: FactorSpec, function: FactorFunction) -> None:
        if spec.factor_id in self._entries:
            raise ValueError(f"duplicate factor id: {spec.factor_id}")
        if not spec.factor_id or spec.minimum_history < 1:
            raise ValueError("factor id and minimum history are required")
        self._entries[spec.factor_id] = (spec, function)

    def list_specs(self) -> tuple[FactorSpec, ...]:
        return tuple(self._entries[key][0] for key in sorted(self._entries))

    def compute(self, factor_id: str, rows: list[dict[str, Any]], index: int) -> float | None:
        try:
            spec, function = self._entries[factor_id]
        except KeyError as exc:
            raise KeyError(f"unknown factor: {factor_id}") from exc
        if index + 1 < spec.minimum_history:
            return None
        value = function(rows, index)
        if value is None or not math.isfinite(float(value)):
            return None
        return float(value)


def default_factor_registry() -> FactorRegistry:
    registry = FactorRegistry()
    registry.register(
        FactorSpec("momentum_5", "Five-session close momentum", 6),
        lambda rows, index: _return_over(rows, index, 5),
    )
    registry.register(
        FactorSpec("momentum_20", "Twenty-session close momentum", 21),
        lambda rows, index: _return_over(rows, index, 20),
    )
    registry.register(
        FactorSpec("reversal_1", "One-session return reversal", 2),
        lambda rows, index: -_return_over(rows, index, 1),
    )
    registry.register(
        FactorSpec("low_volatility_20", "Negative twenty-session realized volatility", 21),
        _low_volatility_20,
    )
    registry.register(
        FactorSpec("volume_surprise_20", "Volume relative to the prior twenty sessions", 21),
        _volume_surprise_20,
    )
    registry.register(
        FactorSpec("range_position_20", "Close position inside the trailing twenty-session range", 20),
        _range_position_20,
    )
    return registry


def build_factor_panel(
    bars: pl.DataFrame,
    *,
    registry: FactorRegistry | None = None,
) -> pl.DataFrame:
    missing = sorted(_REQUIRED - set(bars.columns))
    if missing:
        raise ValueError(f"factor input is missing columns: {missing}")
    registry = registry or default_factor_registry()
    if bars.is_empty():
        return pl.DataFrame()

    output: list[dict[str, object]] = []
    for symbol_frame in bars.sort(["symbol", "session_date"]).partition_by("symbol"):
        rows = symbol_frame.to_dicts()
        for index, row in enumerate(rows):
            next_row = rows[index + 1] if index + 1 < len(rows) else None
            same_split_target = (
                next_row is not None
                and str(next_row["dataset_split"]) == str(row["dataset_split"])
            )
            forward_return = None
            if same_split_target:
                close = float(row["close"])
                next_close = float(next_row["close"])
                if close > 0:
                    forward_return = (next_close / close) - 1.0
            for spec in registry.list_specs():
                value = registry.compute(spec.factor_id, rows, index)
                if value is None:
                    continue
                output.append(
                    {
                        "factor_id": spec.factor_id,
                        "symbol": str(row["symbol"]),
                        "session_date": str(row["session_date"]),
                        "dataset_split": str(row["dataset_split"]),
                        "factor_value": value,
                        "forward_return_1": forward_return,
                        "decision_timing": spec.decision_timing,
                    }
                )
    if not output:
        return pl.DataFrame()
    return pl.DataFrame(output).sort(["factor_id", "session_date", "symbol"])


def audit_factor_causality(
    bars: pl.DataFrame,
    *,
    cutoff_session: str,
    registry: FactorRegistry | None = None,
) -> CausalityAudit:
    registry = registry or default_factor_registry()
    original = build_factor_panel(bars, registry=registry)
    future_mask = pl.col("session_date").cast(pl.String) > cutoff_session
    perturbed = bars.with_columns(
        pl.when(future_mask).then(pl.col("high") * 7.0).otherwise(pl.col("high")).alias("high"),
        pl.when(future_mask).then(pl.col("low") * 0.2).otherwise(pl.col("low")).alias("low"),
        pl.when(future_mask).then(pl.col("close") * 5.0).otherwise(pl.col("close")).alias("close"),
        pl.when(future_mask).then(pl.col("volume") * 1000.0).otherwise(pl.col("volume")).alias("volume"),
    )
    changed = build_factor_panel(perturbed, registry=registry)
    key_columns = ["factor_id", "symbol", "session_date"]
    original_prior = original.filter(pl.col("session_date") <= cutoff_session).sort(key_columns)
    changed_prior = changed.filter(pl.col("session_date") <= cutoff_session).sort(key_columns)
    if original_prior.select(key_columns).to_dicts() != changed_prior.select(key_columns).to_dicts():
        raise LookaheadViolation("future perturbation changed the earlier factor row set")
    original_values = original_prior["factor_value"].to_list()
    changed_values = changed_prior["factor_value"].to_list()
    for index, (before, after) in enumerate(zip(original_values, changed_values)):
        if not math.isclose(float(before), float(after), rel_tol=1e-12, abs_tol=1e-12):
            row = original_prior.row(index, named=True)
            raise LookaheadViolation(
                "future perturbation changed an earlier factor value: "
                f"{row['factor_id']} {row['symbol']} {row['session_date']}"
            )
    return CausalityAudit(
        factor_count=len(registry.list_specs()),
        audited_rows=original_prior.height,
        cutoff_session=cutoff_session,
        passed=True,
    )


def evaluate_factors_strict(
    factor_panel: pl.DataFrame,
    *,
    random_seeds: Iterable[int] = (11, 29, 47, 71, 97),
    minimum_symbols_per_session: int = 5,
    minimum_sessions_per_split: int = 8,
    alpha_t_threshold: float = 2.5,
) -> FactorEvaluation:
    required = {
        "factor_id",
        "symbol",
        "session_date",
        "dataset_split",
        "factor_value",
        "forward_return_1",
    }
    missing = sorted(required - set(factor_panel.columns))
    if missing:
        raise ValueError(f"factor panel is missing columns: {missing}")
    seeds = tuple(int(seed) for seed in random_seeds)
    if not seeds:
        raise ValueError("at least one deterministic random-control seed is required")
    if minimum_symbols_per_session < 3 or minimum_sessions_per_split < 2:
        raise ValueError("strict factor evaluation sample gates are too small")

    sealed_test_rows = factor_panel.filter(pl.col("dataset_split") == "TEST").height
    research = factor_panel.filter(
        pl.col("dataset_split").is_in(["TRAIN", "VALIDATION"])
        & pl.col("forward_return_1").is_not_null()
    )
    rows: list[dict[str, object]] = []
    for factor_id in sorted(research["factor_id"].unique().to_list()):
        frame = research.filter(pl.col("factor_id") == factor_id)
        split_alpha: dict[str, list[float]] = {"TRAIN": [], "VALIDATION": []}
        for split in split_alpha:
            split_frame = frame.filter(pl.col("dataset_split") == split)
            for session_frame in split_frame.partition_by("session_date"):
                session_rows = session_frame.drop_nulls("forward_return_1").sort("symbol").to_dicts()
                if len(session_rows) < minimum_symbols_per_session:
                    continue
                signal = [float(row["factor_value"]) for row in session_rows]
                target = [float(row["forward_return_1"]) for row in session_rows]
                actual_ic = _spearman(signal, target)
                if actual_ic is None:
                    continue
                random_ics: list[float] = []
                session = str(session_rows[0]["session_date"])
                for seed in seeds:
                    shuffled = list(signal)
                    deterministic_seed = int(
                        hashlib.sha256(
                            f"{factor_id}|{split}|{session}|{seed}".encode("utf-8")
                        ).hexdigest()[:16],
                        16,
                    )
                    random.Random(deterministic_seed).shuffle(shuffled)
                    random_ic = _spearman(shuffled, target)
                    if random_ic is not None:
                        random_ics.append(random_ic)
                if random_ics:
                    split_alpha[split].append(actual_ic - statistics.fmean(random_ics))

        train = split_alpha["TRAIN"]
        validation = split_alpha["VALIDATION"]
        train_t = _t_stat(train)
        validation_t = _t_stat(validation)
        category = _category(
            train_t=train_t,
            validation_t=validation_t,
            train_count=len(train),
            validation_count=len(validation),
            minimum_sessions=minimum_sessions_per_split,
            threshold=alpha_t_threshold,
        )
        rows.append(
            {
                "factor_id": factor_id,
                "category": category,
                "train_alpha_t": round(train_t, 6),
                "validation_alpha_t": round(validation_t, 6),
                "train_session_count": len(train),
                "validation_session_count": len(validation),
                "train_paired_alpha_mean": round(statistics.fmean(train), 8) if train else None,
                "validation_paired_alpha_mean": (
                    round(statistics.fmean(validation), 8) if validation else None
                ),
                "random_control_seed_count": len(seeds),
                "alpha_t_threshold": alpha_t_threshold,
                "test_metrics_exposed": False,
                "eligible_for_live_rules": False,
            }
        )
    summary = pl.DataFrame(rows)
    if not summary.is_empty():
        summary = summary.sort(["category", "validation_alpha_t", "factor_id"], descending=[False, True, False])
    return FactorEvaluation(
        summary=summary,
        factor_panel=factor_panel,
        sealed_test_rows=sealed_test_rows,
    )


def write_factor_research(
    *,
    project_root: str | Path,
    evaluation: FactorEvaluation,
    audit: CausalityAudit,
    source_dataset: str,
) -> FactorResearchArtifact:
    if evaluation.factor_panel.is_empty() or evaluation.summary.is_empty():
        raise ValueError("cannot write empty factor research")
    if not audit.passed:
        raise LookaheadViolation("refusing to write factor research without a causal audit")
    root = Path(project_root).resolve()
    destination_root = root / "data" / "historical" / "factor-research"
    destination_root.mkdir(parents=True, exist_ok=True)
    panel_path, panel_hash = _write_parquet(destination_root, "factor-panel", evaluation.factor_panel)
    summary_path, summary_hash = _write_parquet(
        destination_root, "factor-summary", evaluation.summary
    )
    report = {
        "schema_version": "factor_research_report_v1",
        "paper_only": True,
        "source_dataset": source_dataset,
        "causality_audit": {
            "passed": audit.passed,
            "factor_count": audit.factor_count,
            "audited_rows": audit.audited_rows,
            "cutoff_session": audit.cutoff_session,
        },
        "sealed_test_rows": evaluation.sealed_test_rows,
        "test_metrics_exposed": False,
        "live_rules_modified": False,
        "warning": "Research categories are not profit promises and cannot self-promote into live rules.",
        "panel": {
            "relative_path": panel_path.relative_to(root).as_posix(),
            "sha256": panel_hash,
            "rows": evaluation.factor_panel.height,
        },
        "summary": {
            "relative_path": summary_path.relative_to(root).as_posix(),
            "sha256": summary_hash,
            "rows": evaluation.summary.height,
            "categories": evaluation.summary.group_by("category").len().sort("category").to_dicts(),
        },
    }
    report_bytes = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode("utf-8")
    report_hash = hashlib.sha256(report_bytes).hexdigest()
    report_path = destination_root / f"factor-report-{report_hash[:16]}.json"
    _write_immutable(report_path, report_bytes)
    return FactorResearchArtifact(
        panel_relative_path=panel_path.relative_to(root).as_posix(),
        panel_sha256=panel_hash,
        summary_relative_path=summary_path.relative_to(root).as_posix(),
        summary_sha256=summary_hash,
        report_relative_path=report_path.relative_to(root).as_posix(),
        report_sha256=report_hash,
    )


def _return_over(rows: list[dict[str, Any]], index: int, periods: int) -> float:
    previous = float(rows[index - periods]["close"])
    return (float(rows[index]["close"]) / previous) - 1.0


def _low_volatility_20(rows: list[dict[str, Any]], index: int) -> float:
    returns = [_return_over(rows, cursor, 1) for cursor in range(index - 19, index + 1)]
    return -statistics.pstdev(returns)


def _volume_surprise_20(rows: list[dict[str, Any]], index: int) -> float | None:
    history = [float(rows[cursor]["volume"]) for cursor in range(index - 20, index)]
    mean = statistics.fmean(history)
    if mean <= 0:
        return None
    return (float(rows[index]["volume"]) / mean) - 1.0


def _range_position_20(rows: list[dict[str, Any]], index: int) -> float:
    window = rows[index - 19 : index + 1]
    low = min(float(row["low"]) for row in window)
    high = max(float(row["high"]) for row in window)
    if high == low:
        return 0.5
    return (float(rows[index]["close"]) - low) / (high - low)


def _rank(values: list[float]) -> list[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(indexed):
        end = cursor + 1
        while end < len(indexed) and indexed[end][1] == indexed[cursor][1]:
            end += 1
        average_rank = (cursor + 1 + end) / 2.0
        for position in range(cursor, end):
            ranks[indexed[position][0]] = average_rank
        cursor = end
    return ranks


def _spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) != len(right) or len(left) < 3:
        return None
    left_rank = _rank(left)
    right_rank = _rank(right)
    left_mean = statistics.fmean(left_rank)
    right_mean = statistics.fmean(right_rank)
    numerator = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(left_rank, right_rank)
    )
    left_ss = sum((value - left_mean) ** 2 for value in left_rank)
    right_ss = sum((value - right_mean) ** 2 for value in right_rank)
    denominator = math.sqrt(left_ss * right_ss)
    return None if denominator == 0 else numerator / denominator


def _t_stat(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    deviation = statistics.stdev(values)
    if deviation == 0:
        return 0.0
    return statistics.fmean(values) / (deviation / math.sqrt(len(values)))


def _category(
    *,
    train_t: float,
    validation_t: float,
    train_count: int,
    validation_count: int,
    minimum_sessions: int,
    threshold: float,
) -> str:
    if train_count < minimum_sessions or validation_count < minimum_sessions:
        return "INSUFFICIENT_EVIDENCE"
    if train_t >= threshold and validation_t >= threshold:
        return "CONFIRMED_VALIDATION"
    if train_t >= threshold and validation_t <= -threshold:
        return "REVERSED_VALIDATION"
    if train_t >= threshold:
        return "TRAIN_ONLY"
    return "NO_EVIDENCE"


def _write_parquet(root: Path, prefix: str, frame: pl.DataFrame) -> tuple[Path, str]:
    temporary = root / f".tmp-{uuid.uuid4().hex}.parquet"
    frame.write_parquet(temporary, compression="zstd", statistics=True)
    digest = _sha256(temporary)
    destination = root / f"{prefix}-{digest[:16]}.parquet"
    if destination.exists():
        temporary.unlink(missing_ok=True)
    else:
        os.replace(temporary, destination)
    return destination, digest


def _write_immutable(path: Path, content: bytes) -> None:
    if path.exists():
        if path.read_bytes() != content:
            raise RuntimeError("refusing to replace immutable factor research")
        return
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
