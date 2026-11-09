"""The term pair evaluation: the dev set, scoring, and the choice rule of the doc."""

import json
from collections import Counter
from pathlib import Path
from typing import Literal

from meridian.agents.local_model import InProcessLocalModelExecutor
from meridian.evals import term_pairs as tp
from meridian.llm.local import FakeLocalModelClient
from meridian.platform.config import LocalModelSettings

REPO = Path(__file__).resolve().parents[2]
DEV = REPO / "data" / "term_pairs" / "dev.jsonl"
MODEL = "m"
SETTINGS = LocalModelSettings(model=MODEL, digests={MODEL: "a" * 64})


def test_the_dev_set_is_what_the_doc_says() -> None:
    pairs = tp.load_pairs(DEV)
    assert len(pairs) == 61
    assert len({p.id for p in pairs}) == 61
    assert Counter(p.category for p in pairs) == {
        "word_order": 5,
        "spelling": 6,
        "abbreviation": 6,
        "specificity": 10,
        "verbatim": 3,
        "opposite": 13,
        "same_organ": 13,
        "different": 5,
    }
    assert all(
        (p.label == "yes") == (p.category not in tp.LOOKALIKE | {"different"}) for p in pairs
    )
    assert sum(p.lookalike for p in pairs) == 26
    # Every pair can be sent: the four terms only, no date or id.
    assert tp.build_job(pairs, SETTINGS).requests


def test_score_counts_the_metrics_of_the_doc() -> None:
    pairs = tp.load_pairs(DEV)[:2] + [p for p in tp.load_pairs(DEV) if p.lookalike][:2]
    answers = [
        json.dumps({"same_event": "yes", "reason": "Same words."}),  # correct yes
        json.dumps({"same_event": "unsure", "reason": "Cannot tell."}),  # not correct
        json.dumps({"same_event": "yes", "reason": "Wrong."}),  # wrong yes on a look-alike
        "not json",  # rejected
    ]
    client = FakeLocalModelClient(script=answers, digests={MODEL: "a" * 64})
    report = tp.run_model(pairs, SETTINGS, InProcessLocalModelExecutor(client))
    assert report.accuracy == 0.25
    assert report.wrong_yes_lookalike == 1
    assert report.no_recall_lookalike == 0.0
    assert report.unsure_rate == 0.25
    assert report.rejected == 1
    assert report.rejected_reasons == {"bad_json": 1}


def _report(model: str, accuracy_hits: int, wrong_yes: int, rejected: int = 0) -> tp.ModelReport:
    pairs = tp.load_pairs(DEV)
    look = [p for p in pairs if p.lookalike]
    results = []
    for i, p in enumerate(pairs):
        verdict = p.label if i < accuracy_hits else "unsure"
        if p in look[:wrong_yes]:
            verdict = "yes"
        status: Literal["accepted", "rejected"] = (
            "rejected" if i >= len(pairs) - rejected else "accepted"
        )
        results.append(
            tp.PairResult(p, status, verdict if status == "accepted" else None, None, (), 100)
        )
    return tp.ModelReport(model=model, digest="a" * 64, results=results)


def test_the_choice_rule_puts_the_safety_rule_before_accuracy() -> None:
    accurate_but_unsafe = _report("a", 61, wrong_yes=2)
    safe = _report("b", 50, wrong_yes=1)
    safest = _report("c", 45, wrong_yes=0)
    chosen = tp.choose([accurate_but_unsafe, safe, safest])
    assert chosen is not None
    assert chosen.model == "b"  # eligible, and the most accurate of the eligible
    assert tp.choose([accurate_but_unsafe]) is None
    assert tp.choose([_report("d", 61, wrong_yes=0, rejected=4)]) is None  # over 5% rejected


def test_placement_reads_gpu_and_cpu_shares() -> None:
    assert tp.Placement(100, 100, 4096).processor == "100% GPU"
    assert tp.Placement(3158548757, 2641824316, 4096).processor == "16%/84% CPU/GPU"
