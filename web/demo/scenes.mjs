// The demo's scenes (docs/demo/script.md): what the recorder does, and when. Each step is
// tied to a sentence of the narration (`at`, its index, -1 for the scene's very start, with
// `frac` for a point inside it) or to a highlight cue of the script (`cue`, the cue's
// "When" text; the chip comes from the script). A screenshot follows every step.
// Nothing here fills in or submits a form.

const heading = (p, name) => p.getByRole("heading", { name, exact: true });
const byTestId = (p, id) => p.getByTestId(id);
const label = (p, name) => p.locator(`[aria-label="${name}"]`);

export const SCENES = {
  1: [
    { at: -1, do: (h) => h.card("title") },
    { at: 3, do: (h) => h.goto("asha.rao", "/review") },
    { cue: "a named person", do: (h, chip) => h.highlight(h.page.locator("#person"), chip) },
    { at: 5, do: (h) => h.clear() },
    { cue: "one append-only ledger", do: (h, chip) => h.highlight(h.page.getByRole("link", { name: "Event ledger" }), chip) },
    { at: 8, do: (h) => h.clear() },
  ],
  2: [
    { at: -1, do: (h) => h.goto("asha.rao", "/review") },
    { cue: "One rule in the API", do: (h, chip) => h.highlight(heading(h.page, "Review queue"), chip) },
    { at: 3, do: (h) => h.highlight(label(h.page, "Findings to review"), "Clinical ops lead: open findings, newest first") },
    { at: 4, do: (h) => h.goto("karan.mehta", "/review") }, // a fresh load: the nav count follows the role
    { cue: "the queue changes", do: (h, chip) => h.highlight(h.page.getByText(/^Showing \d+ of \d+ findings\.$/), chip) },
    { at: 5, do: (h) => h.goto("asha.rao", "/unblinded") },
    { cue: "does not exist", do: (h, chip) => h.highlight(h.page.getByText("The unblinded zone is only for the unblinded statistician."), chip) },
  ],
  3: [
    { at: -1, do: (h) => h.goto("asha.rao", "/rbqm") },
    { cue: "The heatmap", do: (h, chip) => h.highlight(heading(h.page, "Site heatmap"), chip) },
    { cue: "site 1008", do: (h, chip) => h.highlight(h.page.getByRole("row").filter({ hasText: /^\s*1008/ }), chip) },
    { at: 3, do: (h) => h.goto("asha.rao", "/findings/RBQM-0134") },
    { cue: "the evidence", frac: 0.3, do: (h, chip) => h.highlight(heading(h.page, "Evidence"), chip) },
    { cue: "image digest", do: (h, chip) => h.highlight(h.page.getByText("Image digest", { exact: true }), chip) },
    { cue: "hash of its sandbox policy", frac: 0.35, do: (h, chip) => h.highlight(h.page.getByText("applied policy sha256", { exact: true }), chip) },
    { cue: "ledger seq", frac: 0.75, do: (h, chip) => h.highlight(h.page.getByText(/^ledger seq \d+, hash/), chip) },
    { at: 5, do: async (h, _c, t) => { await h.clear(); await h.proveReadOnly(t); } },
    { cue: "a person decided", do: (h, chip) => h.highlight(byTestId(h.page, "closed-line"), chip) },
    { at: 6, frac: 0.6, do: (h) => h.highlight(heading(h.page, "Decision history").locator("xpath=following::table[1]"), "Who, when, why, and the ledger seq") },
  ],
  4: [
    { at: -1, do: (h) => h.goto("asha.rao", "/fleet") },
    { cue: "tries to escape", do: (h, chip) => h.highlight(heading(h.page, "Containment checks"), chip) },
    { at: 2, do: (h) => h.highlight(h.page.getByRole("heading", { name: "rbqm substrate passed" }).locator("xpath=.."), "Each check refused, and logged by the platform") },
    { cue: "eight hundred and forty-four", do: (h, chip) => h.highlight(heading(h.page, "Containment checks"), chip) },
    { at: 4, do: (h) => h.goto("asha.rao", "/ledger") },
    { cue: "recomputes every hash", frac: 0.4, do: async (h, chip) => { await h.click(h.page.getByRole("button", { name: "Verify the hash chain" })); await h.highlight(h.page.getByRole("button", { name: "Verify the hash chain" }).locator("xpath=.."), chip); } },
    { at: 5, do: (h) => h.card("leakcheck") },
    { cue: "positive control", do: (h, chip) => h.highlight(h.page.locator("td.control"), chip) },
    { at: 6, frac: 0.5, do: (h) => h.highlight(h.page.locator("tbody"), "Canary in no store; every control found") },
  ],
  5: [
    { at: -1, do: (h) => h.goto("karan.mehta", "/safety") },
    { cue: "there are none", do: (h, chip) => h.highlight(heading(h.page, "By check").locator("xpath=following::table[1]"), chip) },
    { at: 3, do: (h) => h.select(h.page.locator("main select").first(), { label: "DC-5833edad (evaluation)" }) },
    { at: 3, frac: 0.5, do: (h) => h.highlight(heading(h.page, "By check").locator("xpath=following::table[1]"), "Test cut DC-5833edad: planted discrepancies found") },
    { at: 4, do: (h) => h.goto("karan.mehta", "/findings/SAF-0475") },
    { cue: "two models", frac: 0.6, do: (h, chip) => h.highlight(label(h.page, "answers of assessment 3027"), chip) },
    { at: 6, do: (h) => h.highlight(byTestId(h.page, "assessment").first(), "yes only when both models say yes") },
    { cue: "seventy-eight signals", do: async (h, chip) => { await h.goto("karan.mehta", "/safety/signals"); await h.highlight(byTestId(h.page, "label-summary"), chip); } },
    { cue: "nine left", frac: 0.75, do: (h, chip) => h.highlight(label(h.page, "Signals by tier"), chip) },
  ],
  6: [
    { at: -1, do: (h) => h.goto("leela.nair", "/preclinical") },
    { at: 0, frac: 0.5, do: (h) => h.highlight(heading(h.page, "Study design"), "MRD-101-TOX-01: a 13-week rat study in SEND") },
    { at: 1, do: (h) => h.highlight(label(h.page, "Findings per dose group"), "Dunnett, Jonckheere-Terpstra, Cochran-Armitage") },
    { cue: "ninety-eight findings", do: (h, chip) => h.highlight(heading(h.page, "Findings"), chip) },
    { cue: "two milligrams", frac: 0.4, do: (h, chip) => h.highlight(label(h.page, "Current decisions"), chip) },
    { cue: "the record says so", do: (h, chip) => h.highlight(label(h.page, "Current decisions").getByText(/Assessment drafted by Claude Code/).first(), chip) },
    { cue: "fourteen earlier entries", do: async (h, chip) => { await h.open(h.page.getByText(/^Earlier decisions \(\d+\)$/)); await h.highlight(h.page.getByText(/^Earlier decisions \(\d+\)$/).locator("xpath=.."), chip); } },
    { at: 7, do: (h) => h.highlight(heading(h.page, "Dose translation"), "FDA 2005 method, step by step") },
    { cue: "safety factor of ten", frac: 0.35, do: (h, chip) => h.highlight(label(h.page, "The working, step by step"), chip) },
    { cue: "one point nine four milligrams", frac: 0.8, do: (h, chip) => h.highlight(byTestId(h.page, "dose-current"), chip) },
    { at: 10, do: (h) => h.highlight(byTestId(h.page, "translational-current"), "Liver and heart: a monitoring plan for the first human study") },
    { cue: "margins of ten and one hundred", frac: 0.7, do: (h, chip) => h.highlight(label(h.page, "Safety margins"), chip) },
  ],
  7: [
    { at: -1, do: (h) => h.goto("rahul.iyer", "/portfolio") },
    { cue: "top risks", frac: 0.5, do: (h, chip) => h.highlight(label(h.page, "Top risks of MRD-101"), chip) },
    { at: 1, do: (h) => h.highlight(heading(h.page, "MRD-101: IND submission"), "MRD-101: IND submission") },
    { cue: "six criteria", do: (h, chip) => h.highlight(label(h.page, "Criteria"), chip) },
    { cue: "one was not met", frac: 0.4, do: (h, chip) => h.highlight(byTestId(h.page, "criterion-ind_gaps_resolved"), chip) },
    { cue: "no verdict", do: (h, chip) => h.highlight(label(h.page, "Open items"), chip) },
    { cue: "a conditional go", do: (h, chip) => h.highlight(byTestId(h.page, "gate-decision"), chip) },
    { cue: "the person's", frac: 0.3, do: (h, chip) => h.highlight(byTestId(h.page, "gate-decision"), chip) },
  ],
  8: [
    { at: -1, do: (h) => h.goto("asha.rao", "/quality") },
    { at: 1, do: (h) => h.click(h.page.getByRole("tab", { name: "Audit trail" })) },
    { cue: "found something real", do: (h, chip) => h.highlight(h.page.getByRole("link", { name: "QA-0178" }).locator("xpath=ancestor::li[1]"), chip) },
    { at: 3, frac: 0.5, do: (h) => h.goto("karan.mehta", "/safety/signals") },
    { cue: "a correction event", do: (h, chip) => h.highlight(byTestId(h.page, "correction-note").first(), chip) },
    { at: 5, do: (h) => h.goto("asha.rao", "/ledger?type=signal_scan_requested&type=attribution_correction") },
    { cue: "operator identities", do: (h, chip) => h.highlight(h.page.getByRole("row").filter({ hasText: "20068" }).first(), chip) },
  ],
  9: [
    { at: -1, do: (h) => h.goto("asha.rao", "/fleet") },
    { cue: "a fresh gVisor sandbox", frac: 0.5, do: (h, chip) => h.highlight(byTestId(h.page, "substrate-placement").first(), chip) },
    { at: 1, do: (h) => h.card("scale") },
    { cue: "nine hundred and ten runs", frac: 0.2, do: (h, chip) => h.highlight(h.page.locator("table"), chip) },
    { cue: "byte for byte", frac: 0.8, do: (h, chip) => h.highlight(h.page.locator('[data-row="same"]'), chip) },
    { at: 3, do: (h) => h.card("stats") },
    { cue: "eight thousand five hundred", frac: 0.3, do: (h, chip) => h.highlight(h.page.locator("table"), chip) },
  ],
  10: [
    { at: -1, do: (h) => h.card("closing") },
    { at: 1, do: (h) => h.highlight(h.page.locator('[data-line="limit-0"]'), null) },
    { at: 2, do: (h) => h.highlight(h.page.locator('[data-line="limit-1"]'), null) },
    { at: 3, do: (h) => h.highlight(h.page.locator('[data-line="limit-2"]'), null) },
    { at: 4, do: (h) => h.highlight(h.page.locator('[data-line="limit-3"]'), null) },
    { at: 6, do: (h) => h.highlight(h.page.locator('[data-line="lesson-0"]'), null) },
    { at: 7, do: (h) => h.highlight(h.page.locator('[data-line="lesson-1"]'), null) },
    { at: 8, do: (h) => h.highlight(h.page.locator('[data-line="lesson-2"]'), null) },
    { at: 9, do: (h) => h.highlight(h.page.locator('[data-line="credit"]'), null) },
  ],
};
