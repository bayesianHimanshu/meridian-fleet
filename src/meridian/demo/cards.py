"""The demo's card pages: facts with no Command Center view (the leak check, the stats, the
kind and GKE measurements) and the title and closing cards. Each is generated from a real
file at record time, never typed in. Plain HTML, no external requests, no em dashes."""

import html
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from meridian.demo.script import EM_DASH

CREDIT = (
    "Narration voice: Piper en_US-libritts-high, trained on LibriTTS (CC BY 4.0). "
    "Zen, H., Dang, V., Clark, R., Zhang, Y., Weiss, R. J., Jia, Y., Chen, Z. and Wu, Y. "
    "(2019). LibriTTS: A Corpus Derived from LibriSpeech for Text-to-Speech. Interspeech "
    "2019. Piper 1.8.0 (GPL-3.0-or-later), run as a separate program."
)

STYLE = """
:root { color-scheme: dark; --bg:#0f1720; --panel:#17212c; --line:#2b3a4a; --text:#e8eef4;
  --muted:#9fb0c0; --accent:#5fb3f9; --good:#4cc38a; --bad:#f2705e; }
* { box-sizing: border-box; }
body { margin:0; width:1440px; height:900px; background:var(--bg); color:var(--text);
  font: 20px/1.45 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { padding: 64px 80px; height: 100%; display:flex; flex-direction:column; gap:24px; }
h1 { font-size: 44px; margin:0; } h2 { font-size: 26px; margin: 0; color: var(--muted);
  font-weight: 500; }
.kicker { color: var(--accent); font-size: 18px; letter-spacing: .08em; text-transform: uppercase; }
table { border-collapse: collapse; width: 100%; font-size: 19px; }
th, td { text-align: left; padding: 10px 14px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.yes { color: var(--good); font-weight: 600; } .no { color: var(--bad); font-weight: 600; }
.source { color: var(--muted); font-size: 16px; margin-top: auto; }
ul.lines { list-style: none; padding: 0; margin: 0; display: grid; gap: 14px; font-size: 26px; }
ul.lines li { padding: 12px 18px; border-left: 4px solid var(--line); background: var(--panel); }
.cols { display:grid; grid-template-columns: 1fr 1fr; gap: 40px; }
.credit { color: var(--muted); font-size: 15px; }
"""


CDISC = (
    "Study MRD-201 is derived from the CDISC SDTM/ADaM Pilot Project data (CDISCPILOT01), "
    "relabelled and with synthetic additions, used under CDISC's terms of use. It is not a "
    "real study, and this project is not affiliated with or endorsed by CDISC."
)


class CardError(ValueError):
    pass


def page(title: str, body: str) -> str:
    out = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>\n"
    )
    if EM_DASH in out:
        raise CardError(f"card {title!r} contains an em dash")
    return out


def esc(value: object) -> str:
    return html.escape(str(value))


def title_card() -> str:
    return page(
        "Meridian Agent Fleet",
        '<div class="kicker">A learning project</div><h1>Meridian Agent Fleet v1.0</h1>'
        "<h2>Oversight agents for a fictional drug company, from a rat study to a first "
        "human dose and a stage gate. Agents compute and cite; people decide.</h2>"
        '<p class="source">Every number in this video comes from the project\'s ledger or '
        "its phase summaries. Fictional company, synthetic and public data.</p>",
    )


def leakcheck_card(result: dict[str, Any], name: str) -> str:
    rows = "".join(
        f'<tr data-row="{esc(s["source"])}"><td>{esc(s["source"])}</td>'
        f'<td class="{"yes" if not s["canary_hits"] else "no"}">'
        f"{'none' if not s['canary_hits'] else 'FOUND'}</td>"
        f'<td class="control {"yes" if s["control_found"] else "no"}">'
        f"{'found' if s['control_found'] else 'not found'}</td>"
        f"<td>{esc(s['positive_control'])}</td></tr>"
        for s in result["sources"]
    )
    verdict = "passed" if result["passed"] else "failed"
    return page(
        "Leak check",
        '<div class="kicker">Containment: a leak check</div>'
        f"<h1>Canary in no store; every positive control found</h1>"
        f"<h2>Zones {esc(', '.join(result['zones']))}, {len(result['runs'])} runs, "
        f"{esc(verdict)}</h2>"
        "<table><thead><tr><th>Store searched</th><th>Canary</th><th>Positive control</th>"
        f"<th>What the control was</th></tr></thead><tbody>{rows}</tbody></table>"
        f'<p class="source">From var/leakcheck/{esc(name)} (`meridian leakcheck`).</p>',
    )


def stats_card(stats: dict[str, Any]) -> str:
    runs = stats["runs"]
    rows = "".join(
        f'<tr><td>{esc(k)}</td><td class="num">{v:,}</td></tr>'
        for k, v in runs["by_executor"].items()
    )
    scope = ", ".join(f"{k} {v:,}" for k, v in runs["by_cut_scope"].items())
    return page(
        "Runs by executor",
        '<div class="kicker">Scale: from meridian stats</div>'
        f"<h1>{runs['completed']:,} completed runs</h1>"
        f"<h2>By cut scope: {esc(scope)}</h2>"
        f"<table><thead><tr><th>Executor</th><th>Runs</th></tr></thead><tbody>{rows}"
        "</tbody></table>"
        f'<p class="source">`meridian stats` on the demo\'s copy of the ledger, computed at '
        f"seq {stats['computed_at_seq']}; the ledger verifies "
        f"({stats['verification']['events_checked']:,} events).</p>",
    )


def _table(markdown: str, heading: str) -> list[list[str]]:
    section = markdown.split(f"## {heading}", 1)
    if len(section) != 2:
        raise CardError(f"no section {heading!r}")
    rows = []
    for line in section[1].split("\n## ", 1)[0].splitlines():
        if line.startswith("|") and not set(line.replace("|", "").strip()) <= {"-", ":"}:
            rows.append([c.strip().replace("**", "") for c in line.strip("|").split("|")])
    return rows


def scale_card(phase4b: str) -> str:
    measured = _table(phase4b, "Measurements, kind and GKE")
    proofs = _table(phase4b, "The proofs and their evidence")
    same = next(r for r in proofs if r[0].startswith("Same drafts as kind"))
    head, *body = measured
    trs = "".join(
        "<tr>" + "".join(f"<td>{esc(c)}</td>" for c in r) + "</tr>"
        for r in body
        if r[0] in ("Per run p50 / p95", "Full pass", "Runs per second")
    )
    return page(
        "Kind and GKE",
        '<div class="kicker">Scale: Agent Substrate on kind and on GKE</div>'
        "<h1>A full pass of 910 runs in 270 s on GKE</h1>"
        "<table><thead><tr>" + "".join(f"<th>{esc(c)}</th>" for c in head) + "</tr></thead>"
        f"<tbody>{trs}</tbody></table>"
        f'<p data-row="same"><strong>{esc(same[0])}:</strong> {esc(same[1])}</p>'
        '<p class="source">docs/phase4b-summary.md, "Measurements, kind and GKE" and '
        '"The proofs and their evidence".</p>',
    )


def closing_card(limits: Sequence[str], lessons: Sequence[str]) -> str:
    def items(values: Sequence[str], kind: str) -> str:
        return "".join(f'<li data-line="{kind}-{i}">{esc(v)}</li>' for i, v in enumerate(values))

    return page(
        "Limits and lessons",
        '<div class="cols"><div><div class="kicker">Honest limits</div>'
        f'<ul class="lines">{items(limits, "limit")}</ul></div>'
        '<div><div class="kicker">Lessons</div>'
        f'<ul class="lines">{items(lessons, "lesson")}</ul></div></div>'
        '<p class="source">README.md, "Honest limits"; docs/lessons.md. The repository holds '
        "the code, every number in this video, and the misses.</p>"
        f'<p class="credit" data-line="credit">{esc(CREDIT)}</p>'
        f'<p class="credit" data-line="cdisc">{esc(CDISC)}</p>',
    )


LIMITS = (
    "Synthetic and public data only",
    "Not a validated system for regulated use",
    "Development identity only",
    "Some evaluations missed their targets",
)
LESSONS = (
    "Measure the measurement first",
    "A positive control makes an empty search mean something",
    "Identity is data",
)


def write_all(out: Path, leak_file: Path, stats: dict[str, Any], phase4b_doc: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    cards = {
        "title.html": title_card(),
        "leakcheck.html": leakcheck_card(
            json.loads(leak_file.read_text(encoding="utf-8")), leak_file.name
        ),
        "stats.html": stats_card(stats),
        "scale.html": scale_card(phase4b_doc.read_text(encoding="utf-8")),
        "closing.html": closing_card(LIMITS, LESSONS),
    }
    paths = []
    for name, text in cards.items():
        if re.search(EM_DASH, text):
            raise CardError(name)
        (out / name).write_text(text, encoding="utf-8")
        paths.append(out / name)
    return paths
