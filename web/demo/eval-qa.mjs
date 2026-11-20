// Run a committed question set against the built index, once, with the page's own search
// code: node eval-qa.mjs <questions.json> <results.md> "<title>". Run 2 ran the original 30
// (docs/demo/qa-questions.json, results docs/demo/qa-results.md) and the held-out 20
// (docs/demo/qa-heldout.json, results docs/demo/qa-heldout-results.md). Both sets were
// committed before the code they evaluate; nothing here may be tuned on them.
//
// Hits. One-source question: top-1 = the first passage is from an expected source; top-3 =
// any of the top 3 is. Two-source question: top-1 = the first passage is from either of the
// two expected sources; top-3 = BOTH expected sources are among the top 3 (stricter, so it
// can be lower than top-1). A declined question is no hit. Not covered: correct when
// declined.
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { DECLINE_BELOW, search } from "./search.mjs";

const ROOT = resolve(import.meta.dirname, "..", "..");
const index = JSON.parse(readFileSync(resolve(ROOT, "var/demo/index.json"), "utf-8"));
const transcript = JSON.parse(readFileSync(resolve(ROOT, "var/demo/transcript.json"), "utf-8"));
const [qFile, outFile, title] = process.argv.slice(2);
if (!qFile || !outFile || !title) throw new Error("usage: eval-qa.mjs <questions.json> <results.md> <title>");
const { questions } = JSON.parse(readFileSync(resolve(ROOT, qFile), "utf-8"));

const inGroup = (source, group) => group.includes(source);
const fmt = (t) => `${String(Math.floor(t / 60))}:${String(Math.floor(t % 60)).padStart(2, "0")}`;

const rows = [];
for (const q of questions) {
  const r = search(index, q.question);
  const got = r.results.map((p) => p.source);
  let top1 = null;
  let top3 = null;
  let declinedOk = null;
  if (q.kind === "uncovered") {
    declinedOk = r.declined;
  } else if (q.kind === "answerable") {
    top1 = !r.declined && got.length > 0 && inGroup(got[0], q.expected[0]);
    top3 = !r.declined && got.some((s) => inGroup(s, q.expected[0]));
  } else {
    const [g1, g2] = q.expected;
    top1 = !r.declined && got.length > 0 && (inGroup(got[0], g1) || inGroup(got[0], g2));
    top3 = !r.declined && got.some((s) => inGroup(s, g1)) && got.some((s) => inGroup(s, g2));
  }
  const t = r.results.find((p) => p.start !== null);
  rows.push({
    id: q.id,
    kind: q.kind,
    question: q.question,
    declined: r.declined,
    top1,
    top3,
    declined_ok: declinedOk,
    best_coverage: r.results[0]?.coverage ?? 0,
    retrieved: r.results.map((p) => ({ source: p.source, start: p.start, score: p.score, coverage: p.coverage })),
    first_transcript_time: t ? fmt(t.start) : null,
  });
}

const sum = (kind, key) => rows.filter((x) => x.kind === kind && x[key]).length;
const count = (kind) => rows.filter((x) => x.kind === kind).length;
const summary = {
  answerable: { n: count("answerable"), top1: sum("answerable", "top1"), top3: sum("answerable", "top3") },
  two_source: { n: count("two_source"), top1: sum("two_source", "top1"), top3_both: sum("two_source", "top3") },
  uncovered: { n: count("uncovered"), declined: sum("uncovered", "declined_ok") },
  declined_wrongly: rows.filter((x) => x.kind !== "uncovered" && x.declined).map((x) => x.id),
  threshold: DECLINE_BELOW,
  passages: index.docs.length,
  video_duration: transcript.duration,
};
writeFileSync(resolve(ROOT, outFile.replace(/^docs\/demo\//, "var/demo/").replace(/\.md$/, ".json")), JSON.stringify({ summary, rows }, null, 2) + "\n");

const yn = (v) => (v === null ? "" : v ? "yes" : "no");
const falseDeclines = rows.filter((x) => x.kind !== "uncovered" && x.declined);
const md = [
  `# ${title}`,
  "",
  `Questions: [${qFile.replace(/^docs\/demo\//, "")}](${qFile.replace(/^docs\/demo\//, "")}), committed before the code`,
  "they evaluate, run once with the page's own search (web/demo/search.mjs: BM25; it declines",
  `when the best passage covers less than ${String(DECLINE_BELOW)} of the question's idf weight, a word in`,
  "no passage weighing as BM25's idf for df = 0). Nothing was tuned on them. Written by",
  "`node web/demo/eval-qa.mjs`.",
  "",
  "Hits. One-source: top-1 = the first passage is from an expected source; top-3 = any of the",
  "top 3 is. Two-source: top-1 = the first passage is from either expected source; top-3 =",
  "both expected sources are in the top 3 (stricter). A declined question is no hit.",
  "",
  "| Set | Questions | Top-1 hits | Top-3 hits | False declines | Correct declines |",
  "|---|---:|---:|---:|---:|---:|",
  ...(summary.answerable.n
    ? [`| Answerable from one source | ${String(summary.answerable.n)} | ${String(summary.answerable.top1)} | ${String(summary.answerable.top3)} | ${String(rows.filter((x) => x.kind === "answerable" && x.declined).length)} | |`]
    : []),
  ...(summary.two_source.n
    ? [`| Needing two sources | ${String(summary.two_source.n)} | ${String(summary.two_source.top1)} (first from either) | ${String(summary.two_source.top3_both)} (both in top 3) | ${String(rows.filter((x) => x.kind === "two_source" && x.declined).length)} | |`]
    : []),
  `| Not covered | ${String(summary.uncovered.n)} | | | | ${String(summary.uncovered.declined)} |`,
  "",
  `False declines (answerable, but declined): ${falseDeclines.map((x) => x.id).join(", ") || "none"}.`,
  "",
  `The index holds ${String(summary.passages)} passages (transcript segments and document sections).`,
  "",
  "## Every question",
  "",
  "| Id | Question | Declined | Top-1 | Top-3 | Best coverage | First retrieved source |",
  "|---|---|---|---|---|---:|---|",
  ...rows.map(
    (x) =>
      `| ${x.id} | ${x.question} | ${yn(x.declined)} | ${yn(x.top1)} | ${x.kind === "uncovered" ? (x.declined_ok ? "correctly declined" : "NOT declined") : yn(x.top3)} | ${x.best_coverage.toFixed(2)} | ${x.retrieved[0] ? x.retrieved[0].source + (x.retrieved[0].start !== null ? ` (${fmt(x.retrieved[0].start)})` : "") : ""} |`,
  ),
  "",
];
const text = md.join("\n");
if (text.includes(String.fromCodePoint(0x2014))) throw new Error("an em dash in the results");
writeFileSync(resolve(ROOT, outFile), text);
process.stdout.write(JSON.stringify(summary, null, 2) + "\n");
