# The narrated demo

The demo is a 7 min 30 s video of the Command Center, narrated by a local voice, from
[script.md](script.md). It comes with one page where a viewer watches it and asks questions,
answered by retrieval from the transcript and the project's documents.

## What is where

| File | Committed | What |
|---|---|---|
| docs/demo/script.md | yes | The script: narration, pages, highlights, sources, pronunciation |
| docs/demo/qa-questions.json | yes | The 30 evaluation questions, committed before the index was built |
| docs/demo/qa-results.md | yes | Their results, from one run |
| docs/demo/index.html | yes | The page, with the index and transcript inside (the video is not) |
| src/meridian/demo/, `meridian demo` | yes | Ledger copy, TTS, timing, captions, cards, ffmpeg assembly |
| web/demo/record.mjs, scenes.mjs | yes | The Playwright recorder and each scene's steps |
| web/demo/search.mjs, build-page.mjs, eval-qa.mjs | yes | Retrieval, the page and the evaluation |
| var/demo/demo.mp4 | no | The video: 1440 x 900, H.264 and AAC, 10 chapters |
| var/demo/captions.vtt, transcript.json | no | WebVTT captions and the timestamped transcript |
| var/demo/index.html | no | The page, next to the video: open this one |
| var/demo/ledger.sqlite3, plan.json, frames/, audio/, cards/ | no | Working files |
| var/demo/voice/ | no | The voice model |

## Chapters

| # | Chapter | Starts | Length |
|---|---|---:|---:|
| 1 | What this is | 0:00 | 46.5 s |
| 2 | The Command Center | 0:46 | 38.4 s |
| 3 | RBQM and traceability | 1:24 | 41.5 s |
| 4 | Containment | 2:06 | 44.2 s |
| 5 | Safety | 2:50 | 44.4 s |
| 6 | Preclinical, from the rat to the first dose | 3:34 | 82.1 s |
| 7 | Portfolio and the IND gate | 4:56 | 43.0 s |
| 8 | Audit and attribution | 5:39 | 37.0 s |
| 9 | Scale | 6:16 | 32.3 s |
| 10 | Honest limits and lessons | 6:49 | 40.3 s |
| | **Total** | | **449.5 s (7 min 30 s)** |

## Never writing, and never acting as a person

- **The ledger copy.** Recording reads only a copy of the ledger. `meridian demo prepare`
  copies it, and opens the real ledger read-only with SQLite's backup API.
  - The copy is verified (`ledger verify`).
  - Its last hash must equal the real ledger's hash at the same seq.
  - At the recording: 20,212 events, last hash `4783893682db5e03...`, equal to the real
    ledger's at seq 20212.
- **The read-only API.** The API serves that copy with `meridian api serve --port 8766
  --demo-read-only var/demo/refusals.jsonl --fixed-now 2026-11-20T12:00:00+00:00`.
  - In this mode any request other than GET, HEAD or OPTIONS gets a 403 before any route
    runs. Each refusal is appended to the log with its time.
  - tests/api/test_demo_read_only.py covers every write endpoint, and checks that the
    ledger file is unchanged.
- **The proof, during the recording.** In scene 3 the recorder makes a real decision
  request: `POST /api/v1/findings/RBQM-0134/decisions`, as the operator
  `operator:demo-recorder` (made by `operator_id`, like every operator id, never a person),
  with no role.
  - The gate does not depend on who asks. tests/api/test_demo_read_only.py gets 403 for
    that operator, for a person and with no identity at all.
  - The first recordings sent it as asha.rao. That was wrong: a tool must never call the
    API as a person, even against a copy and even when refused. It was changed, and the
    video is byte-identical, because no frame shows the request.
  - It must be refused with 403, or the recording stops.
  - The response and its time go to var/demo/frames/proof.json, and the API logs the same
    refusal.
  - Afterwards the copy's last seq and hash are checked again; they have not changed.
  - In the final recording, the attempt was refused with 403 at 2026-11-20T11:40:35Z (wall
    clock), at 1:55.9 in the video's time.
- **People.** Each scene is shown as the person whose role sees the page (the role
  switcher is development identity). No form is ever submitted. Where the narration
  speaks of a decision, the recorded decision is shown.

## Re-recording

You need:
- `uv tool install piper-tts==1.8.0`: Piper, the local TTS engine. It is installed as a
  uv tool, outside this project's dependencies and lockfile; the pipeline calls the
  `piper` executable.
- The voice model in var/demo/voice/ (below).
- ffmpeg on the system.
- Google Chrome (Playwright drives it through `channel: "chrome"`; no browser is
  downloaded).
- The web dependencies (`cd web && npm ci`).

```sh
uv run meridian demo prepare                  # copy and verify the ledger; narration; plan; cards
MERIDIAN_LEDGER_PATH=var/demo/ledger.sqlite3 uv run meridian api serve --port 8766 \
  --demo-read-only var/demo/refusals.jsonl --fixed-now 2026-11-20T12:00:00+00:00 &
(cd web && MERIDIAN_API_URL=http://127.0.0.1:8766 npx vite --port 5175 &)
(cd web && node demo/record.mjs)              # one still per step; var/demo/frames/
uv run meridian demo assemble                 # var/demo/demo.mp4 with chapters
node web/demo/build-page.mjs                  # the index and the page (both copies)
node web/demo/eval-qa.mjs                     # the Q&A evaluation (run once per recording)
```

Open var/demo/index.html. To record one scene while working on it, run
`DEMO_SCENES=6 node demo/record.mjs`.

**Deterministic.** The same ledger copy and the same script give the same video, byte for
byte. Four things make it so:
- **Fixed audio.** Piper runs with noise 0; with its default noise, the same sentence gave
  different audio twice.
- **Fixed clocks.** Both the browser's clock and the API's clock stand at
  2026-11-20T12:00:00Z.
- **Fixed rendering.** Chrome renders in software, with a fixed colour profile and no LCD
  text.
- **Stills, not a screen capture.** The video is made from still frames with exact
  durations, and ffmpeg encodes with one thread and bitexact flags.

How it was checked:
- Two recordings gave 85 identical PNGs.
- Clearing the audio cache and synthesising again gave the same narration, captions,
  transcript and video.

## Versions and licences

| Item | Version | Licence | Note |
|---|---|---|---|
| Piper (piper-tts) | 1.8.0 | GPL-3.0-or-later | A uv tool, run as a separate program; no Piper code is in this repository |
| Voice | en_US-libritts-high, speaker 0 | Dataset LibriTTS, CC BY 4.0 (model card) | Trained from scratch on LibriTTS train-clean-360 |
| @playwright/test | 1.65.0 (web devDependency, exact) | Apache-2.0 | |
| Browser | Google Chrome 157.0.8103.38 | | Playwright's own Chromium is not installed; none was downloaded |
| ffmpeg | 8.0.1-3ubuntu2+esm5 | | System package |

Voice files (from huggingface.co/rhasspy/piper-voices, en/en_US/libritts/high):

| File | sha256 |
|---|---|
| en_US-libritts-high.onnx | 9127a559e11603f10b366d1a20ac7426826081dbc521de4c2420c57728d73f0f |
| en_US-libritts-high.onnx.json | 2efdc6d7f954588b8180132cbd9b8001933fdd00932c92bc92fd0d2028a9eb3d |
| MODEL_CARD | c478d826aae8a6332f69509271a56083b9ece014a993e16fc79cb94968106a5f |

**Why not the voice first approved.** The first voice, en_US-libritts_r-medium, was
approved as CC BY 4.0. Its model card says it was fine-tuned from en_US-lessac-medium,
which was trained on the Blizzard 2013 Lessac data. That data's licence allows research use
only and excludes commercial use. The project lead chose en_US-libritts-high instead,
whose model card says it was trained from scratch on LibriTTS (CC BY 4.0).

**Credit,** on the closing card and in the page footer: "Narration voice: Piper
en_US-libritts-high, trained on LibriTTS (CC BY 4.0)." The dataset citation: Zen, H., Dang,
V., Clark, R., Zhang, Y., Weiss, R. J., Jia, Y., Chen, Z. and Wu, Y. (2019). LibriTTS: A
Corpus Derived from LibriSpeech for Text-to-Speech. Interspeech 2019.

## The question box

- **Retrieval, not generation.** The page holds an index of 147 passages:
  - transcript segments, each with its time;
  - sections of README.md, docs/project-summary.md, docs/architecture.md, docs/lessons.md
    and the six phase summaries.
- **How a question is answered.** It is scored with BM25 (web/demo/search.mjs; the page and
  the evaluation run the same code). The page shows the top 3 passages with their source,
  and "Play from here" for a transcript passage.
- **When it declines.** It declines when the best passage covers less than half of the
  question's idf weight, and then suggests chapters.
- **Self-contained.** No API key, no model, and no request beyond the video next to the
  page.
- **Captions** are built in the page from the transcript, because a page opened from disk
  may not load captions.vtt.

Results are reported as they came out, never tuned.

**Run 1** used the original decline rule, on the 30 questions in qa-questions.json:
[qa-results-run1.md](qa-results-run1.md). In that rule, a word in no passage weighed
nothing, so "Azure" could not lower the coverage, and only 1 of the 5 uncovered questions
was declined.

**Run 2** fixed the rule on principle. A word in no passage now weighs BM25's idf for
df = 0, the rarest weight there is, and no passage can cover it. The threshold stayed 0.5,
set before the run. The held-out set (qa-heldout.json) was written and committed before
the code changed. Both sets were run once:
[qa-results.md](qa-results.md) (the 30) and [qa-heldout-results.md](qa-heldout-results.md)
(the 20).

**How hits are counted:**
- **One-source question:** top-1 = the first passage is from an expected source; top-3 =
  any of the top 3 is.
- **Two-source question:** top-1 = the first passage is from either expected source; top-3
  = both expected sources are in the top 3. Top-3 is the stricter measure, so it can be
  lower than top-1.
- **Declines:** a declined answerable question is no hit, and counts as a false decline.

| Set | Run | Questions | Top-1 hits | Top-3 hits | False declines | Correct declines |
|---|---|---:|---:|---:|---:|---:|
| Original, one source | 1 | 20 | 11 | 14 | 3 | |
| Original, one source | 2 | 20 | 10 | 13 | 4 | |
| Original, two sources | 1 | 5 | 3 (first from either) | 1 (both in top 3) | 1 | |
| Original, two sources | 2 | 5 | 2 (first from either) | 1 (both in top 3) | 2 | |
| Original, not covered | 1 | 5 | | | | 1 |
| Original, not covered | 2 | 5 | | | | 4 |
| Held-out, answerable | 2 | 10 | 7 | 9 | 1 | |
| Held-out, not covered | 2 | 10 | | | | 10 |

**What run 2 costs.**
- **New false declines.** A question's ordinary words that the documents never use now
  count against it: a03 ("calculated"), t02 ("compare") and b02 were declined although the
  answer is there.
- **One uncovered question still answered.** u04 ("Which patients received the placebo?")
  is not declined: scene 2's narration uses its words without answering it.

## Publishing

The project was developed in a private repository, which stays private. The public
repository, bayesianHimanshu/meridian-fleet, is created fresh from a clean export, as a
single commit tagged v1.1.0. The video is never committed. It is a release asset, and the
Pages workflow (.github/workflows/pages.yml) is the only place the site is assembled:

- the page (docs/demo/index.html, as index.html);
- the three assets downloaded from the release and checked against
  docs/demo/assets.sha256;
- .nojekyll.

The project owner runs every step, in this order:

1. **Merge the release-pages PR** in the private repository.
2. **Rename the private repository** to meridian-fleet-private (Settings > General), and
   update the local remote:
   `git remote set-url origin https://github.com/bayesianHimanshu/meridian-fleet-private.git`.
3. **Export.** In the private clone, on an up-to-date main:
   `scripts/export-public.sh v1.1.0`. It exports main with git archive into a new empty
   directory, scans it, and stops on any match. The scan covers:
   - the GCP identifiers, by exact match against deploy/gke/env.sh;
   - secret shapes, real email addresses and home paths;
   - files over 5 MB.

   It then runs uv sync, the full checks and the web build in a throwaway copy, and prints
   the commands for step 4.
4. **Create and push the public repository and its tag** with the printed commands:
   - `git init`, and your noreply address as the local user.email;
   - one commit, "Meridian Agent Fleet v1.1.0";
   - `gh repo create bayesianHimanshu/meridian-fleet --public`, the push, then `git tag
     v1.1.0` and `git push origin v1.1.0`.
5. **Set Pages to GitHub Actions** on the public repository: Settings > Pages > Source:
   GitHub Actions.
6. **Publish the release.** In the public clone, on main at v1.1.0:
   `scripts/publish-demo-release.sh v1.1.0 <private clone>/var/demo`.
   - It checks the clean main, the tag on origin and the three files against the
     manifest.
   - It shows what it will upload and asks for "yes".
   - Then it runs `gh release create v1.1.0 --verify-tag` with docs/releases/v1.1.0.md as
     the notes. The uploads are demo.mp4, captions.vtt, transcript.json and
     meridian-demo-v1.1.0.zip.
7. **Check the run and open the page.** The release, created with your gh login, starts
   the Pages workflow; a release created with GITHUB_TOKEN would not. Check the run with
   `gh run list --repo bayesianHimanshu/meridian-fleet --workflow pages.yml`, then open
   https://bayesianhimanshu.github.io/meridian-fleet/.

   If the workflow did not start, run it by hand: Actions > Pages > Run workflow, with tag
   v1.1.0.

**Sizes:** demo.mp4 is 13,582,594 bytes, and the offline zip about 12.7 MB. Both are well
within a release asset's limit. The page itself is about 300 KB.

**Offline:** meridian-demo-v1.1.0.zip holds index.html and demo.mp4 side by side. Unzip it
and open index.html. Its Content-Security-Policy allows media from the page's own origin and
from file:, and the page's one inline style and one inline script by their sha256. It allows
nothing else, and no 'unsafe-inline'.
