// The demo recorder (docs/demo/README.md). It drives the Command Center scene by scene at
// 1440 x 900 and saves one still frame per step, with the time the narration reaches it.
// var/demo/frames/frames.json lists every frame and its time; `meridian demo assemble`
// turns them and the narration into the video. Stills, not a screen capture: the same
// ledger copy and script give the same frames, whatever the machine's speed.
//
// It never writes: the API it reads runs with --demo-read-only against a copy of the
// ledger, and once, during scene 3, the recorder makes a real decision request, as the
// operator operator:demo-recorder (never a person), that must be refused with 403. The time and the response go to var/demo/frames/proof.json. Forms
// are never submitted.
import { chromium } from "@playwright/test";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { SCENES } from "./scenes.mjs";

const ROOT = resolve(import.meta.dirname, "..", "..");
const OUT = resolve(ROOT, "var/demo");
const BASE = process.env.DEMO_BASE_URL ?? "http://127.0.0.1:5175";
const ACTOR_KEY = "meridian.command-center.actor";
const FIXED_TIME = new Date("2026-11-20T12:00:00Z");
const ONLY = process.env.DEMO_SCENES ? process.env.DEMO_SCENES.split(",").map(Number) : null;

const plan = JSON.parse(readFileSync(resolve(OUT, "plan.json"), "utf-8"));
const framesDir = resolve(OUT, "frames");
rmSync(framesDir, { recursive: true, force: true });
mkdirSync(framesDir, { recursive: true });

const STYLE = `
  *, *::before, *::after { transition: none !important; animation: none !important;
    caret-color: transparent !important; scroll-behavior: auto !important; }
  #demo-overlay { position: fixed; inset: 0; pointer-events: none; z-index: 2147483647; }
  #demo-overlay .box { position: absolute; border: 3px solid #ffb020; border-radius: 6px;
    box-shadow: 0 0 0 4px rgba(255, 176, 32, 0.25); }
  #demo-overlay .chip { position: absolute; background: #ffb020; color: #1a1a1a;
    font: 600 17px/1.3 system-ui, sans-serif; padding: 6px 12px; border-radius: 999px;
    max-width: 640px; white-space: normal; }
`;

const browser = await chromium.launch({
  channel: "chrome",
  // Software rendering with fixed colour and font settings: the same page gives the same pixels.
  args: [
    "--hide-scrollbars",
    "--disable-gpu",
    "--force-color-profile=srgb",
    "--disable-lcd-text",
    "--font-render-hinting=none",
    "--disable-partial-raster",
  ],
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 1,
  locale: "en-GB",
  timezoneId: "UTC",
  colorScheme: "light",
  reducedMotion: "reduce",
});
const page = await context.newPage();
await page.clock.setFixedTime(FIXED_TIME);

/** Wait until the page has loaded its data: no request in flight and no "Loading ..." text. */
async function settled() {
  await page.waitForTimeout(400); // let a click or a role change start its requests
  await page.waitForLoadState("networkidle");
  await page
    .waitForFunction(() => !/(^|\n)Loading /.test(document.body.innerText), null, { timeout: 60000 })
    .catch(() => {
      throw new Error(`still loading: ${page.url()}`);
    });
  await page.addStyleTag({ content: STYLE });
  await page.evaluate(() => document.fonts.ready);
}

const h = {
  page,
  async goto(actor, path) {
    await page.goto(`${BASE}/`);
    await page.evaluate(([k, a]) => localStorage.setItem(k, a), [ACTOR_KEY, actor]);
    await page.goto(`${BASE}${path}`);
    await settled();
  },
  async card(name) {
    await page.goto(`file://${resolve(OUT, "cards", `${name}.html`)}`);
    await page.addStyleTag({ content: STYLE });
  },
  async scroll(locator, block = "start") {
    await locator.first().evaluate((el, b) => el.scrollIntoView({ block: b }), block);
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => r(null))));
  },
  async click(locator) {
    await h.clear();
    await locator.first().click();
    await settled();
  },
  async select(locator, value) {
    await h.clear();
    await locator.first().selectOption(value);
    await settled();
  },
  async open(summary) {
    await summary.first().evaluate((el) => {
      const d = el.closest("details");
      if (d) d.open = true;
    });
  },
  /** Outline an element and show the cue's chip next to it; scrolls it into view. */
  async highlight(locator, chip) {
    const el = locator.first();
    await el.waitFor({ state: "attached", timeout: 30000 });
    const visible = await el.evaluate((e) => {
      const r = e.getBoundingClientRect();
      return r.top >= 60 && r.bottom <= window.innerHeight - 20;
    });
    if (!visible) await el.evaluate((e) => e.scrollIntoView({ block: "center" }));
    await el.evaluate((e, text) => {
      let o = document.getElementById("demo-overlay");
      if (!o) {
        o = document.createElement("div");
        o.id = "demo-overlay";
        document.body.appendChild(o);
      }
      o.replaceChildren();
      const r = e.getBoundingClientRect();
      const pad = 6;
      const box = document.createElement("div");
      box.className = "box";
      Object.assign(box.style, {
        left: `${Math.max(2, r.left - pad)}px`,
        top: `${Math.max(2, r.top - pad)}px`,
        width: `${Math.min(window.innerWidth - 8, r.width + 2 * pad)}px`,
        height: `${Math.min(window.innerHeight - 8, r.height + 2 * pad)}px`,
      });
      o.appendChild(box);
      if (text) {
        const chip = document.createElement("div");
        chip.className = "chip";
        chip.textContent = text;
        const right = r.right + 20 + 420 < window.innerWidth && r.height < 120;
        const below = r.bottom + 14 + 40 < window.innerHeight;
        Object.assign(
          chip.style,
          right
            ? { left: `${r.right + 20}px`, top: `${Math.max(8, r.top + r.height / 2 - 18)}px` }
            : {
                left: `${Math.min(Math.max(8, r.left), window.innerWidth - 660)}px`,
                top: below ? `${r.bottom + 14}px` : `${Math.max(8, r.top - 54)}px`,
              },
        );
        o.appendChild(chip);
      }
    }, chip);
  },
  async clear() {
    await page.evaluate(() => document.getElementById("demo-overlay")?.replaceChildren());
  },
  /** The proof of the read-only mode: a real decision request, refused with 403. */
  async proveReadOnly(sceneStart) {
    // As an operator (plan.recorder_identity, operator:demo-recorder), never as a person:
    // a tool never calls the API under a person's name, even when the call is refused.
    const actor = plan.recorder_identity;
    if (!actor?.startsWith("operator:")) throw new Error(`not an operator identity: ${String(actor)}`);
    const response = await page.request.post(`${BASE}/api/v1/findings/RBQM-0134/decisions`, {
      headers: { "X-Meridian-Actor": actor },
      data: { action: "accepted" },
    });
    const proof = {
      attempted_at: new Date().toISOString(),
      video_time: sceneStart,
      request: `POST /api/v1/findings/RBQM-0134/decisions as ${actor} (no role)`,
      status: response.status(),
      mode: response.headers()["x-meridian-mode"] ?? null,
      body: await response.json(),
    };
    writeFileSync(resolve(framesDir, "proof.json"), JSON.stringify(proof, null, 2) + "\n");
    if (proof.status !== 403) throw new Error(`a write was not refused: ${JSON.stringify(proof)}`);
  },
};

const frames = [];
let n = 0;
async function shoot(t) {
  const png = `f${String(++n).padStart(4, "0")}.png`;
  await page.screenshot({ path: resolve(framesDir, png), animations: "disabled", caret: "hide" });
  if (frames.length && frames.at(-1).t >= t) frames.pop(); // the later state wins
  frames.push({ png, t: Math.round(t * 1000) / 1000 });
}

for (const scene of plan.scenes) {
  if (ONLY && !ONLY.includes(scene.number)) continue;
  const steps = SCENES[scene.number];
  if (!steps) throw new Error(`no steps for scene ${scene.number}`);
  const chips = Object.fromEntries(scene.cues.map((c) => [c.when, c]));
  const covered = new Set(steps.filter((s) => s.cue).map((s) => s.cue));
  for (const cue of scene.cues) {
    if (!covered.has(cue.when)) throw new Error(`scene ${scene.number}: no step for cue "${cue.when}"`);
  }
  for (const step of steps) {
    const cue = step.cue ? chips[step.cue] : null;
    if (step.cue && !cue) throw new Error(`scene ${scene.number}: no cue "${step.cue}" in the script`);
    const index = cue ? cue.sentence : (step.at ?? 0);
    const s = index < 0 ? { start: 0, end: 0 } : scene.sentences[index];
    const t = scene.start + s.start + (step.frac ?? 0) * (s.end - s.start);
    await step.do(h, cue ? cue.chip : null, t);
    await shoot(t);
  }
  process.stdout.write(`scene ${scene.number}: ${String(steps.length)} steps\n`);
}
writeFileSync(
  resolve(framesDir, "frames.json"),
  JSON.stringify({ base: BASE, fixed_time: FIXED_TIME.toISOString(), frames }, null, 2) + "\n",
);
await browser.close();
process.stdout.write(`${String(frames.length)} frames, ${plan.duration} s\n`);
