// The demo page's question box: retrieval, not generation (docs/demo/README.md). Passages
// are transcript segments (with their times) and sections of the project's documents. A
// question is scored with BM25 over the passages. The page declines when the best passage
// covers less than half of the question's weight, each word weighted by how rare it is in
// the passages (its BM25 idf).
//
// Run 2 (the decline rule fixed on principle): a word that appears in no passage counts
// too, at the idf BM25 gives a word with no passage (df = 0), the rarest weight there is. A
// question about something the project never mentions ("Snowflake", "half-life") now
// carries weight that no passage can cover. Run 1 gave such words no weight, so "Azure"
// could not lower the coverage. The threshold stays 0.5, set before run 2 and not changed
// after. This one file is used by the page (inlined), by the index builder and by the
// evaluation, so all three use the same code.

export const K1 = 1.2;
export const B = 0.75;
export const DECLINE_BELOW = 0.5;
export const TOP = 3;

const STOP = new Set(
  (
    "a an and are as at be been but by can could did do does for from had has have how i if in " +
    "into is it its me my no not of on or our so than that the their them then there these " +
    "they this those to was we were what when where which who whom why will with would you " +
    "your about after all also any each just more most much only other over same some such " +
    "up very s t"
  ).split(" "),
);

/** Lower case, words and numbers, stop words dropped, a light plural and possessive strip. */
export function tokens(text) {
  const out = [];
  for (const raw of text.toLowerCase().normalize("NFKD").split(/[^a-z0-9]+/)) {
    if (!raw || STOP.has(raw)) continue;
    let w = raw;
    if (w.length > 4 && w.endsWith("ies")) w = `${w.slice(0, -3)}y`;
    else if (w.length > 3 && w.endsWith("s") && !w.endsWith("ss") && !/^\d+$/.test(w)) w = w.slice(0, -1);
    out.push(w);
  }
  return out;
}

/** passages: [{id, source, title, text, start?}] -> an index the page can embed as JSON. */
export function buildIndex(passages) {
  const df = {};
  const docs = passages.map((p) => {
    const tf = {};
    const words = tokens(`${p.title} ${p.text}`);
    for (const w of words) tf[w] = (tf[w] ?? 0) + 1;
    for (const w of Object.keys(tf)) df[w] = (df[w] ?? 0) + 1;
    return { ...p, len: words.length, tf };
  });
  const n = docs.length;
  const avg = docs.reduce((s, d) => s + d.len, 0) / Math.max(1, n);
  const idf = {};
  for (const [w, f] of Object.entries(df)) idf[w] = Math.log(1 + (n - f + 0.5) / (f + 0.5));
  return { version: 1, k1: K1, b: B, avg, idf, docs };
}

function score(index, doc, query) {
  let s = 0;
  for (const w of query) {
    const f = doc.tf[w];
    if (!f) continue;
    const norm = f + index.k1 * (1 - index.b + (index.b * doc.len) / index.avg);
    s += (index.idf[w] ?? 0) * ((f * (index.k1 + 1)) / norm);
  }
  return s;
}

/** The idf of a word in no passage: BM25's idf with df = 0. */
export function unseenIdf(index) {
  const n = index.docs.length;
  return Math.log(1 + (n + 0.5) / 0.5);
}

/** The weight of the question's words that a passage contains, out of all its weight;
 * a word in no passage weighs unseenIdf and can never be covered. */
export function coverage(index, doc, query) {
  const unique = [...new Set(query)];
  const unseen = unseenIdf(index);
  const total = unique.reduce((s, w) => s + (index.idf[w] ?? unseen), 0);
  if (total === 0) return 0;
  return unique.reduce((s, w) => s + (doc.tf[w] ? (index.idf[w] ?? 0) : 0), 0) / total;
}

/** {declined, results: top passages with score and coverage, chapters: suggestions}. */
export function search(index, question, top = TOP) {
  const query = tokens(question);
  const ranked = index.docs
    .map((d) => ({ d, s: score(index, d, query) }))
    .filter((r) => r.s > 0)
    .sort((a, b) => b.s - a.s || (a.d.id < b.d.id ? -1 : 1));
  const results = ranked.slice(0, top).map((r) => ({
    id: r.d.id,
    source: r.d.source,
    title: r.d.title,
    text: r.d.text,
    start: r.d.start ?? null,
    score: Math.round(r.s * 1000) / 1000,
    coverage: Math.round(coverage(index, r.d, query) * 1000) / 1000,
  }));
  const declined = results.length === 0 || results[0].coverage < DECLINE_BELOW;
  const chapters = [];
  for (const r of ranked) {
    if (r.d.scene && !chapters.includes(r.d.scene)) chapters.push(r.d.scene);
    if (chapters.length === 3) break;
  }
  return { declined, results, chapters };
}
