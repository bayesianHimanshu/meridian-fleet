"""Term pair evaluation of safety.term_adjudicator (docs/local-model-eval.md).

Each pair is sent exactly as the adjudicator sends a finding's terms: the same prompt,
options and validator, one job per model through the safety.local_model sandbox. The
metrics are the ones the doc fixed before any run. VRAM is read on the host from
Ollama's /api/ps right after the job (the sandbox may call only /api/tags and
/api/chat).
"""

import json
import statistics
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from meridian.agents.local_model import LocalBatch, LocalModelExecutor
from meridian.agents.safety import term_adjudicator as ta
from meridian.llm.local_job import LocalModelJob
from meridian.platform.config import LocalModelSettings

LOOKALIKE = frozenset({"opposite", "same_organ"})


class TermRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    preferred_term: str
    verbatim_term: str


class TermPair(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    category: str
    label: Literal["yes", "no"]
    record_a: TermRecord
    record_b: TermRecord

    @property
    def lookalike(self) -> bool:
        return self.category in LOOKALIKE

    def model_input(self) -> dict[str, Any]:
        return ta.adjudicator_input(
            ta.Term(self.record_a.preferred_term, self.record_a.verbatim_term),
            ta.Term(self.record_b.preferred_term, self.record_b.verbatim_term),
        )


def load_pairs(path: Path) -> list[TermPair]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [TermPair.model_validate(json.loads(line)) for line in lines if line.strip()]


@dataclass(frozen=True, slots=True)
class PairResult:
    pair: TermPair
    status: Literal["accepted", "rejected", "error"]
    verdict: str | None
    reason: str | None
    rejection_reasons: tuple[str, ...]
    latency_ms: int | None
    error: str | None = None

    @property
    def correct(self) -> bool:
        return self.verdict == self.pair.label


@dataclass(frozen=True, slots=True)
class Placement:
    """Where Ollama holds the model after the job (/api/ps)."""

    size: int
    size_vram: int
    context: int | None

    @property
    def cpu_share(self) -> float:
        return 0.0 if self.size == 0 else (self.size - self.size_vram) / self.size

    @property
    def processor(self) -> str:
        if self.size_vram >= self.size:
            return "100% GPU"
        cpu = round(self.cpu_share * 100)
        return f"{cpu}%/{100 - cpu}% CPU/GPU"


@dataclass(slots=True)
class ModelReport:
    model: str
    digest: str | None
    results: list[PairResult]
    placement: Placement | None = None
    batch_error: str | None = None
    rejected_reasons: Counter[str] = field(default_factory=Counter)

    def _share(self, items: Sequence[PairResult], pred: Callable[[PairResult], bool]) -> float:
        return sum(1 for r in items if pred(r)) / len(items) if items else 0.0

    @property
    def lookalikes(self) -> list[PairResult]:
        return [r for r in self.results if r.pair.lookalike]

    @property
    def accuracy(self) -> float:
        return self._share(self.results, lambda r: r.correct)

    @property
    def no_recall_lookalike(self) -> float:
        return self._share(self.lookalikes, lambda r: r.verdict == "no")

    @property
    def wrong_yes_lookalike(self) -> int:
        return sum(1 for r in self.lookalikes if r.verdict == "yes")

    @property
    def wrong_yes_different(self) -> int:
        return sum(1 for r in self.results if r.pair.label == "no" and r.verdict == "yes")

    @property
    def unsure_rate(self) -> float:
        return self._share(self.results, lambda r: r.verdict == "unsure")

    @property
    def rejected(self) -> int:
        return sum(1 for r in self.results if r.status != "accepted")

    def latencies(self) -> tuple[int | None, float | None, float | None]:
        """(first call, median of warm calls, 95th percentile of warm calls) in ms."""
        times = [r.latency_ms for r in self.results if r.latency_ms is not None]
        if not times:
            return None, None, None
        warm = sorted(times[1:]) or times
        p95 = warm[min(len(warm) - 1, round(0.95 * (len(warm) - 1)))]
        return times[0], statistics.median(warm), float(p95)

    def eligible(self, max_wrong_yes: int, max_rejected: int) -> bool:
        return self.wrong_yes_lookalike <= max_wrong_yes and self.rejected <= max_rejected

    def summary(self) -> dict[str, Any]:
        first, median, p95 = self.latencies()
        return {
            "model": self.model,
            "digest": self.digest,
            "pairs": len(self.results),
            "lookalikes": len(self.lookalikes),
            "accuracy": round(self.accuracy, 3),
            "no_recall_lookalike": round(self.no_recall_lookalike, 3),
            "wrong_yes_lookalike": self.wrong_yes_lookalike,
            "wrong_yes_different": self.wrong_yes_different,
            "unsure_rate": round(self.unsure_rate, 3),
            "rejected": self.rejected,
            "rejected_reasons": dict(sorted(self.rejected_reasons.items())),
            "latency_first_ms": first,
            "latency_median_ms": median,
            "latency_p95_ms": p95,
            "size_bytes": None if self.placement is None else self.placement.size,
            "size_vram_bytes": None if self.placement is None else self.placement.size_vram,
            "processor": None if self.placement is None else self.placement.processor,
            "context": None if self.placement is None else self.placement.context,
            "batch_error": self.batch_error,
        }


def build_job(pairs: Sequence[TermPair], settings: LocalModelSettings) -> LocalModelJob:
    requests = []
    for pair in pairs:
        inp = pair.model_input()
        problems = ta.input_problems(inp)
        if problems:
            raise ValueError(f"pair {pair.id} cannot be sent: {problems}")
        requests.append(
            ta.build_request(
                inp,
                model=settings.model,
                seed=settings.seed,
                num_ctx=settings.num_ctx,
                num_predict=settings.num_predict,
                temperature=settings.temperature,
                think=settings.think,
            )
        )
    return LocalModelJob(
        model=settings.model, pinned_digest=settings.pinned(), requests=tuple(requests)
    )


def score(pairs: Sequence[TermPair], batch: LocalBatch, model: str) -> ModelReport:
    report = ModelReport(model=model, digest=batch.served_digest, results=[])
    for pair, result in zip(pairs, batch.results, strict=True):
        if result.response is None:
            report.results.append(
                PairResult(
                    pair, "error", None, None, (), None, f"{result.error_kind}: {result.error}"
                )
            )
            report.rejected_reasons[str(result.error_kind)] += 1
            continue
        j = ta.validate(result.response)
        status: Literal["accepted", "rejected"] = "accepted" if j.accepted else "rejected"
        for code in j.reasons:
            report.rejected_reasons[code] += 1
        report.results.append(
            PairResult(pair, status, j.verdict, j.reason, j.reasons, result.response.latency_ms)
        )
    return report


def run_model(
    pairs: Sequence[TermPair],
    settings: LocalModelSettings,
    executor: LocalModelExecutor,
) -> ModelReport:
    try:
        batch = executor.call(build_job(pairs, settings), settings.timeout_seconds)
    except Exception as exc:  # the report says so; nothing else is run for the model
        return ModelReport(model=settings.model, digest=None, results=[], batch_error=str(exc))
    finally:
        executor.drain()
    return score(pairs, batch, settings.model)


def placement(base_url: str, model: str, timeout: float = 10.0) -> Placement | None:
    """size, size_vram and context of the loaded model, from /api/ps on the host."""
    try:
        data = httpx.get(f"{base_url}/api/ps", timeout=timeout, trust_env=False).json()
    except httpx.HTTPError, ValueError:
        return None
    for m in data.get("models", []):
        if m.get("name") == model or m.get("model") == model:
            return Placement(
                size=int(m.get("size") or 0),
                size_vram=int(m.get("size_vram") or 0),
                context=m.get("context_length"),
            )
    return None


def choose(
    reports: Sequence[ModelReport], max_wrong_yes: int = 1, max_rejected: int = 3
) -> ModelReport | None:
    """The rule of docs/local-model-eval.md, fixed before any run."""
    eligible = [r for r in reports if r.results and r.eligible(max_wrong_yes, max_rejected)]
    if not eligible:
        return None

    def key(r: ModelReport) -> tuple[float, int, int, float]:
        on_gpu = 0 if r.placement is not None and r.placement.cpu_share == 0 else 1
        median = r.latencies()[1]
        return (-r.accuracy, r.wrong_yes_different, on_gpu, median or float("inf"))

    return sorted(eligible, key=key)[0]
