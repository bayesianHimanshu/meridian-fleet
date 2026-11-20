// Build the demo page (docs/demo/README.md): the retrieval index over the transcript and
// the project's documents, and one self-contained HTML file. It writes:
// - var/demo/index.json, the index (used by eval-qa.mjs);
// - var/demo/index.html, next to demo.mp4, captions.vtt and transcript.json;
// - docs/demo/index.html, the same page committed (the video is not; see the README).
// The page makes no request but for the video and captions beside it: no CDN, no font,
// no API, no model.
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { buildIndex } from "./search.mjs";

const ROOT = resolve(import.meta.dirname, "..", "..");
const OUT = resolve(ROOT, "var/demo");
export const DOCS = [
  "README.md",
  "docs/project-summary.md",
  "docs/architecture.md",
  "docs/lessons.md",
  "docs/phase1-summary.md",
  "docs/phase2-summary.md",
  "docs/phase3-summary.md",
  "docs/phase4a-summary.md",
  "docs/phase4b-summary.md",
  "docs/phase5-summary.md",
];
const CHUNK_WORDS = 120;
const SEGMENT_WORDS = 45;
const CREDIT =
  "Narration voice: Piper en_US-libritts-high, trained on LibriTTS (CC BY 4.0). Zen, H., Dang, V., " +
  "Clark, R., Zhang, Y., Weiss, R. J., Jia, Y., Chen, Z. and Wu, Y. (2019). LibriTTS: A Corpus " +
  "Derived from LibriSpeech for Text-to-Speech. Interspeech 2019.";

const CDISC =
  "Study MRD-201 is derived from the CDISC SDTM/ADaM Pilot Project data (CDISCPILOT01), relabelled and " +
  "with synthetic additions, used under CDISC's terms of use. It is not a real study, and this project " +
  "is not affiliated with or endorsed by CDISC.";
const words = (s) => s.split(/\s+/).filter(Boolean).length;

/** Markdown to plain text for a passage: links keep their text, marks go. */
function plain(md) {
  return md
    .replace(/```[a-z]*\n?/g, "")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[*_`]/g, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/^\s*\|?[-:| ]+\|?\s*$/gm, "")
    .replace(/\|/g, " ")
    .replace(/[ \t]+/g, " ")
    .trim();
}

export function docPassages(file, text) {
  const out = [];
  const title = (text.match(/^# (.+)$/m) ?? [null, file])[1];
  const parts = text.split(/^## /m);
  const sections = [["(intro)", parts[0].replace(/^# .+$/m, "")]];
  for (const part of parts.slice(1)) {
    const nl = part.indexOf("\n");
    sections.push([part.slice(0, nl).trim(), part.slice(nl + 1)]);
  }
  for (const [heading, body] of sections) {
    const source = `${file}#${heading}`;
    let chunk = [];
    let sub = "";
    const flush = () => {
      const t = plain(chunk.join("\n\n"));
      if (words(t) >= 4) {
        out.push({
          id: `d-${file.replace(/[^a-z0-9]+/gi, "-")}-${String(out.length)}`,
          source,
          title: `${title}: ${heading === "(intro)" ? "introduction" : heading}${sub ? `, ${sub}` : ""}`,
          text: t,
        });
      }
      chunk = [];
    };
    for (const block of body.split(/\n\s*\n/)) {
      const h3 = block.match(/^### (.+)$/m);
      if (h3) {
        flush();
        sub = h3[1].trim();
      }
      const b = block.replace(/^### .+$/m, "").trim();
      if (!b) continue;
      if (chunk.length && words(chunk.join(" ")) + words(b) > CHUNK_WORDS) flush();
      chunk.push(b);
    }
    flush();
  }
  return out;
}

export function transcriptPassages(transcript) {
  const out = [];
  const byScene = new Map();
  for (const s of transcript.segments) {
    if (!byScene.has(s.scene)) byScene.set(s.scene, []);
    byScene.get(s.scene).push(s);
  }
  for (const [scene, segs] of byScene) {
    let group = [];
    const flush = () => {
      if (!group.length) return;
      out.push({
        id: `t-${String(scene).padStart(2, "0")}-${String(out.length)}`,
        source: `transcript:scene-${String(scene)}`,
        title: `Chapter ${String(scene)}. ${group[0].chapter}`,
        text: group.map((g) => g.text).join(" "),
        start: group[0].start,
        scene,
      });
      group = [];
    };
    for (const s of segs) {
      if (group.length && words(group.map((g) => g.text).join(" ")) + words(s.text) > SEGMENT_WORDS) flush();
      group.push(s);
    }
    flush();
  }
  return out;
}

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function page(index, transcript, searchSource) {
  const data = JSON.stringify({ index, transcript }).replace(/</g, "\\u003c");
  const script = searchSource.replace(/^export /gm, "").replace(/^import .*$/gm, "");
  const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="__CSP__">
<title>Meridian Agent Fleet: the demo</title>
<style>
:root { color-scheme: light dark; --bg:#f6f7f9; --panel:#fff; --line:#d8dde3; --text:#17202a;
  --muted:#5b6673; --accent:#1d5fbf; --hit:#fff1c7; --focus:#b35c00; }
@media (prefers-color-scheme: dark) { :root { --bg:#0f1720; --panel:#17212c; --line:#2b3a4a;
  --text:#e8eef4; --muted:#9fb0c0; --accent:#7fbfff; --hit:#3a3010; --focus:#ffb020; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font: 16px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header, main, footer { max-width: 1200px; margin: 0 auto; padding: 16px; }
h1 { font-size: 1.6rem; margin: .2rem 0; } h2 { font-size: 1.15rem; margin: 0 0 .5rem; }
p.lead { color: var(--muted); margin: 0; }
main { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 16px; }
section { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 14px; }
video { width: 100%; aspect-ratio: 16 / 10; background: #000; border-radius: 6px; display:block; }
.note { color: var(--muted); font-size: .92rem; }
ol.chapters { margin: 0; padding: 0; list-style: none; display: grid; gap: 4px; }
button { font: inherit; color: inherit; background: none; border: 1px solid var(--line);
  border-radius: 6px; padding: 6px 10px; cursor: pointer; text-align: left; }
button:hover { border-color: var(--accent); }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
ol.chapters button { width: 100%; display: flex; gap: 8px; }
.time { font-variant-numeric: tabular-nums; color: var(--muted); min-width: 3.2em; }
.current { background: var(--hit); }
#transcript { max-height: 420px; overflow-y: auto; }
#transcript p { margin: 0; }
#transcript button { border: none; width: 100%; padding: 4px 6px; display: flex; gap: 8px; }
form { display: flex; gap: 8px; flex-wrap: wrap; }
label { width: 100%; font-weight: 600; }
input[type=search] { flex: 1 1 220px; font: inherit; padding: 8px 10px; border-radius: 6px;
  border: 1px solid var(--line); background: var(--bg); color: var(--text); }
.answer { border-top: 1px solid var(--line); padding-top: 10px; margin-top: 10px; }
.answer h3 { font-size: 1rem; margin: 0 0 4px; }
.src { font-size: .85rem; color: var(--muted); }
.qa { grid-column: 1 / -1; }
footer { color: var(--muted); font-size: .85rem; }
@media (max-width: 760px) { main { grid-template-columns: minmax(0, 1fr); } #transcript { max-height: 300px; } }
</style>
</head>
<body>
<header>
<h1>Meridian Agent Fleet: a narrated demo</h1>
<p class="lead">Oversight agents for a fictional drug company, from a rat study to a first human
dose and a stage gate. About 7.5 minutes. Ask a question below: answers are passages from the
transcript and the project's documents, found by search, never written by a model.</p>
</header>
<main>
<section aria-labelledby="video-title">
<h2 id="video-title">Video</h2>
<video id="video" controls preload="metadata" src="demo.mp4"></video>
<p class="note" id="video-note" hidden>The video is not here. It is recorded into var/demo/
(docs/demo/README.md); open var/demo/index.html, which sits next to it. The transcript and the
questions work without it.</p>
</section>
<section aria-labelledby="chapters-title">
<h2 id="chapters-title">Chapters</h2>
<ol class="chapters" id="chapters"></ol>
</section>
<section aria-labelledby="transcript-title">
<h2 id="transcript-title">Transcript</h2>
<p class="note">Follows the video. Choose a line to play from it.</p>
<div id="transcript"></div>
</section>
<section class="qa" aria-labelledby="qa-title">
<h2 id="qa-title">Ask the demo</h2>
<form id="ask" role="search">
<label for="question">A question about the project</label>
<input type="search" id="question" name="question" autocomplete="off" placeholder="For example: who decides the NOAEL?">
<button type="submit">Search</button>
</form>
<div id="answers" aria-live="polite"></div>
</section>
</main>
<footer>
<p>${esc(CREDIT)}</p>
<p>${esc(CDISC)}</p>
<p>Adverse event reports and labeling are from openFDA (CC0). No endorsement by the FDA or the manufacturer is implied.</p>
<p>A learning project: fictional company, synthetic and public data, not a validated system. Code and documentation: Apache License 2.0.</p>
</footer>
<script id="data" type="application/json">${data}</script>
<script>
${script}
const { index, transcript } = JSON.parse(document.getElementById("data").textContent);
const video = document.getElementById("video");
const fmt = (t) => { const s = Math.floor(t); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };
function play(t) { try { video.currentTime = t; video.play(); } catch (e) { /* no video */ } video.focus(); }
video.addEventListener("error", () => { document.getElementById("video-note").hidden = false; }, true);
// Captions from the transcript (the same cues as captions.vtt, which a page opened from disk
// may not load): one cue per sentence, shown by default.
const track = video.addTextTrack("captions", "English", "en");
for (const s of transcript.segments) track.addCue(new VTTCue(s.start, s.end, s.text));
track.mode = "showing";
const chapterList = document.getElementById("chapters");
for (const c of transcript.chapters) {
  const li = document.createElement("li");
  const b = document.createElement("button");
  b.type = "button";
  b.dataset.start = c.start;
  b.innerHTML = '<span class="time"></span><span></span>';
  b.children[0].textContent = fmt(c.start);
  b.children[1].textContent = c.scene + ". " + c.title;
  b.addEventListener("click", () => play(c.start));
  li.appendChild(b);
  chapterList.appendChild(li);
}
const tBox = document.getElementById("transcript");
const lines = transcript.segments.map((s) => {
  const p = document.createElement("p");
  const b = document.createElement("button");
  b.type = "button";
  b.innerHTML = '<span class="time"></span><span></span>';
  b.children[0].textContent = fmt(s.start);
  b.children[1].textContent = s.text;
  b.setAttribute("aria-label", "Play from " + fmt(s.start) + ": " + s.text);
  b.addEventListener("click", () => play(s.start));
  p.appendChild(b);
  tBox.appendChild(p);
  return { s, b };
});
let current = -1;
video.addEventListener("timeupdate", () => {
  const t = video.currentTime;
  const i = lines.findIndex((l) => t >= l.s.start && t < l.s.end + 0.45);
  if (i === current) return;
  if (current >= 0) lines[current].b.classList.remove("current");
  current = i;
  if (i >= 0) {
    lines[i].b.classList.add("current");
    const box = tBox.getBoundingClientRect(), r = lines[i].b.getBoundingClientRect();
    if (r.top < box.top || r.bottom > box.bottom) tBox.scrollTop += r.top - box.top - 40;
  }
  [...chapterList.querySelectorAll("button")].forEach((b, k, all) => {
    const next = all[k + 1] ? Number(all[k + 1].dataset.start) : Infinity;
    b.classList.toggle("current", t >= Number(b.dataset.start) && t < next);
  });
});
const answers = document.getElementById("answers");
document.getElementById("ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = document.getElementById("question").value.trim();
  answers.replaceChildren();
  if (!q) return;
  const r = search(index, q);
  const head = document.createElement("p");
  if (r.declined) {
    head.textContent = "The demo and its documents do not cover that. These chapters may be closest:";
    answers.appendChild(head);
    const ul = document.createElement("ul");
    const picks = r.chapters.length ? r.chapters : transcript.chapters.map((c) => c.scene);
    for (const n of picks) {
      const c = transcript.chapters.find((x) => x.scene === n);
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = c.scene + ". " + c.title + " (" + fmt(c.start) + ")";
      b.addEventListener("click", () => play(c.start));
      li.appendChild(b);
      ul.appendChild(li);
    }
    answers.appendChild(ul);
    return;
  }
  head.textContent = "The " + r.results.length + " closest passages:";
  answers.appendChild(head);
  for (const p of r.results) {
    const div = document.createElement("div");
    div.className = "answer";
    const h = document.createElement("h3");
    h.textContent = p.title;
    const src = document.createElement("p");
    src.className = "src";
    src.textContent = p.start !== null ? "Transcript at " + fmt(p.start) : "From " + p.source.replace("#", ", section ");
    const text = document.createElement("p");
    text.textContent = p.text;
    div.append(h, src, text);
    if (p.start !== null) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = "Play from here (" + fmt(p.start) + ")";
      b.addEventListener("click", () => play(p.start));
      div.appendChild(b);
    }
    answers.appendChild(div);
  }
});
</script>
</body>
</html>
`;
  if (html.includes(String.fromCodePoint(0x2014))) throw new Error("an em dash in the page");
  return withCsp(html);
}

/** The page's Content-Security-Policy: nothing by default; media only from the page's own
 * origin (and file: for the offline zip); the one inline style and the one inline script
 * allowed by their sha256, no 'unsafe-inline'. The JSON data block is not executed. */
export function withCsp(html) {
  const one = (re) => {
    const all = [...html.matchAll(re)];
    if (all.length !== 1) throw new Error(`expected one block for ${String(re)}, found ${String(all.length)}`);
    return all[0][1];
  };
  const hash = (text) => `'sha256-${createHash("sha256").update(text, "utf8").digest("base64")}'`;
  const style = one(/<style>([\s\S]*?)<\/style>/g);
  const script = one(/<script>([\s\S]*?)<\/script>/g);
  const csp = [
    "default-src 'none'",
    "media-src 'self' file:",
    `style-src ${hash(style)}`,
    `script-src ${hash(script)}`,
    "base-uri 'none'",
    "form-action 'none'",
  ].join("; ");
  return html.replace("__CSP__", csp);
}

export function build() {
  const transcript = JSON.parse(readFileSync(resolve(OUT, "transcript.json"), "utf-8"));
  const passages = [
    ...transcriptPassages(transcript),
    ...DOCS.flatMap((f) => docPassages(f, readFileSync(resolve(ROOT, f), "utf-8"))),
  ];
  const index = buildIndex(passages);
  writeFileSync(resolve(OUT, "index.json"), JSON.stringify(index) + "\n");
  const html = page(index, transcript, readFileSync(resolve(import.meta.dirname, "search.mjs"), "utf-8"));
  writeFileSync(resolve(OUT, "index.html"), html);
  writeFileSync(resolve(ROOT, "docs/demo/index.html"), html);
  return { passages: passages.length, bytes: html.length };
}

if (process.argv[1] === import.meta.filename) {
  const r = build();
  process.stdout.write(`index of ${String(r.passages)} passages; page ${String(r.bytes)} bytes\n`);
}
