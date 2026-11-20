# Demo script: Meridian Agent Fleet v1.0

**Status:** approved with edits (review of 20 Nov 2026).

**Target:** 7 to 8 minutes, measured from the audio: 7 min 30 s (see Timing). Each
scene's length follows its measured audio. If the audio had run past 8 minutes, scenes 6
and 3 would have been cut first.

**Viewer:** a technical person who is new to clinical development.

**The thread:** one rat study becomes a toxicologist's NOAEL, then the first human dose,
then a monitoring plan, then an IND gate decision. Containment and review run around it.

## Ground rules for the recording

- **Read-only.** The API runs in demo mode against a verified copy of the ledger. Every
  write endpoint returns 403. Forms are shown filled in and are never submitted.
- **Window.** The Command Center runs at 1440 x 900 in Chrome (/usr/bin/google-chrome,
  driven by Playwright).
- **Highlights.** A 3 px outline and a short caption chip are drawn over the element the
  narration names, for as long as the sentence lasts. The chip text is in each scene's
  "Highlights" column.
- **Pages without a Command Center view.** Some facts have no page: the leak check, the
  stats, and the phase measurements. For these the recorder opens a local "card" page,
  generated at record time from the real file or command output, as named in each scene.
  No terminal is filmed.
- **Numbers.** Every number comes from one of two places:
  - `docs/project-stats.md`, computed at seq 20158 ("stats" below);
  - the doc and section named in the sources table.

  Numbers shown on screen are what the copy of the ledger shows at record time. The copy
  is taken after seq 20212, and differs from the stats only by the conformance run of the
  wrap-up.
- **People.** Each scene is shown as the person whose role sees that page, using the role
  switcher. Development identity is a header, not a sign-in, and nothing is recorded under
  that person.

---

## Scene 1: What this is (about 45 s)

**Page:** a title card (local page), then the Command Center home page as Asha Rao
(clinical ops lead).

**Narration:**
> Meridian Agent Fleet is a learning project. It is a fleet of oversight agents for a
> fictional drug company, following one compound from a rat toxicology study to its
> first dose in people. It also follows a second compound through a Phase 2 trial. Three
> principles hold it together. First, agents compute and cite, but people decide: every
> finding waits for a named person. Second, containment comes from infrastructure, not
> from instructions. Each agent runs in a sandbox that reaches only what its zone's policy
> allows, and most zones reach nothing at all. Third, there is one append-only ledger. Every agent output and every human
> decision is an event in a hash chain, more than twenty thousand events so far.

**Actions:**
1. The title card for 4 s: "Meridian Agent Fleet v1.0".
2. Open `/` as asha.rao.

**Highlights:**

| When | Element | Chip |
|---|---|---|
| "a named person" | the role switcher | Development identity |
| "one append-only ledger" | the Ledger nav item | Ledger |

**Sources:**
- "more than twenty thousand": stats, events total 20,158 (seq 20158).
- The principles: docs/project-summary.md, "Design principles".

## Scene 2: The Command Center (about 45 s)

**Pages:**
1. `/review` as asha.rao (clinical ops lead).
2. `/review` as karan.mehta (medical monitor).
3. `/unblinded` as asha.rao, which is refused ("only for the unblinded statistician").

**Narration:**
> The Command Center is how people work with the fleet. Each person sees what their role
> allows. One rule in the API decides it, and it is tested as a property. This is the
> review queue for the clinical operations lead: open findings, newest first. Switch to
> the medical monitor and the queue changes: now it holds the safety signals to review. The trial
> is blinded. Who received the drug and who received placebo is hidden from everyone but
> the unblinded statistician, and the clinical lead cannot open that zone at all. A hidden
> finding is not merely greyed out. For the wrong role it does not exist.

**Actions:**
1. Open the queue as asha.rao and scroll its first three items.
2. Switch the role to karan.mehta and wait for the queue to reload.
3. Open `/unblinded` as asha.rao and show the refusal.

**Highlights:**

| When | Element | Chip |
|---|---|---|
| "One rule in the API" | the page header | src/meridian/api/visibility.py |
| "the queue changes" | the queue count | Count per role |
| "does not exist" | the refusal or empty state | Not shown to this role |

**Sources:**
- The single module and the property test: docs/phase3-summary.md, "What was built", 1.
- "a hidden finding is 404, never 403": the same section.
- The queue counts are read on screen at record time: the medical monitor had 9 at the
  wrap-up (docs/phase5-summary.md, signal scout 1.0.1).

## Scene 3: RBQM and traceability (about 50 s)

**Pages:**
1. `/rbqm` as asha.rao, cut DC-BASE.
2. `/findings/RBQM-0134`, kri_monitor's site 1008 deviation finding on DC-BASE, dismissed
   by Asha Rao (clinical ops lead) at seq 788 on 7 Nov 2026.

**Narration:**
> Risk-based quality monitoring, or RBQM, watches the trial's sites for signs of trouble.
> The heatmap shows each site against each risk indicator. This finding says site 1008
> reports more protocol deviations than expected. Open it, and the evidence is the measured
> rate, the expected rate and the test, not a sentence from a model. Below that is the
> traceability: the agent and version, the image digest it ran in, the hash of its sandbox
> policy, the hash of its inputs, and the ledger seq. No fault was planted at this site,
> yet it has more deviations than its peers. Whether that matters is a judgement, so a
> person decided, and the record shows who and why.

**Actions:**
1. On `/rbqm`, hover the site 1008 row of the heatmap.
2. Click the finding and scroll to Evidence, then to Traceability.
3. Scroll to "Decision history" and show the recorded dismissal: who (asha.rao, clinical
   ops lead), when, the reason, and seq 788. No form is touched.

**Highlights:**

| When | Element | Chip |
|---|---|---|
| "The heatmap" | `Site heatmap` | Site heatmap |
| "site 1008" | the 1008 cell | 1008: deviations |
| "the evidence" | the `Evidence` panel | Evidence |
| "image digest" | Traceability: image digest | Image digest |
| "hash of its sandbox policy" | Traceability: policy sha256 | Policy hash |
| "ledger seq" | Traceability: seq | Ledger seq |
| "a person decided" | `Decision history` | Dismissed by asha.rao, seq 788 |

**Sources:**
- Site 1008, with no fault planted and a deviation z of 3.6: docs/phase1-summary.md,
  "Known limitations".
- The dismissal: ledger seq 788 ("DC-BASE is the clean base cut; the deviation excess at
  1008 is a known natural breach (phase 1 eval)").
- The traceability fields: docs/phase4a-summary.md, "What moved", traceability.

## Scene 4: Containment (about 55 s)

**Pages:**
1. `/fleet` as asha.rao, the "Containment checks" panel.
2. `/ledger`, the verify panel.
3. A card generated from var/leakcheck/kind-20261119T202520Z.json: its zones, its runs,
   each store searched and each positive control.

**Narration:**
> Containment is proved by running it, not by reading a policy. Conformance starts a real
> sandbox for each zone and tries to escape: reaching an outside address, the host, another
> actor, the cluster's own API, writing outside its folder. Every attempt must be refused,
> and the platform's own log must say so. The ledger has recorded eight hundred and
> forty-four refusals. The ledger checks itself, too: verify recomputes every hash in the
> chain. A leak check plants a unique marker in a copy of the inputs, runs the agents, and
> searches every place data could linger. The search counts only because each place also
> has a positive control: a planted copy that the search must find.

**Actions:**
1. On `/fleet`, scroll to "Containment checks" and expand the newest row (the wrap-up
   conformance) to show each check refused.
2. On `/ledger`, run the verify panel. It is a read-only GET, and the result reads "ok".
3. Show the leak check card, with the "positive control: found" cells highlighted one by
   one.

**Highlights:**

| When | Element | Chip |
|---|---|---|
| "tries to escape" | the check rows | Refused |
| "eight hundred and forty-four" | a card overlay | 844 refusals, stats seq 20158 |
| "recomputes every hash" | the verify result | Chain intact |
| "positive control" | the control column | Control found |

**Sources:**
- 844 refusals: stats, "Containment".
- The conformance checks: docs/phase4b-summary.md, "The proofs".
- The leak check method and its positive controls: docs/phase4b-summary.md, the leak check
  sources table.
- This leak check: var/leakcheck/kind-20261119T202520Z.json (passed).

## Scene 5: Safety (about 55 s)

**Pages:**
1. `/safety` as karan.mehta, on DC-BASE and then on the evaluation cut DC-5833edad, which
   has 6 planted discrepancies (the most of any cut in the ledger copy).
2. `/findings/SAF-0475`, the "Same-event assessment" panel.
3. `/safety/signals`, the Triage panel and "Against the US label".

**Narration:**
> Serious adverse events are recorded in both the trial database and the safety database,
> and the two must agree. The reconciler matches them and lists every
> discrepancy. On the real cut there are none. On a test cut with planted faults, it finds
> them. When two event terms might name the same event, a local model judges, and here
> there are two models. The answer is yes or no only when both agree; otherwise a person
> looks. Both answers are shown. Last, public adverse event reports from the US FDA, through
> openFDA: seventy-eight signals for the approved oral combination of the same molecule
> with trospium, sorted into tiers, compared with its label, and only nine left for the
> medical monitor to review.

**Actions:**
1. Show `/safety` on DC-BASE (0 discrepancies), then switch to the evaluation cut and show
   its discrepancies table.
2. Open SAF-0475 and scroll to "Same-event assessment", where both models' answers are
   visible.
3. Open `/safety/signals` and scroll through Triage, then "Against the US label".

**Highlights:**

| When | Element | Chip |
|---|---|---|
| "there are none" | the DC-BASE count | 0 on DC-BASE |
| "two models" | each model's answer | phi4-mini / qwen3.5 |
| "seventy-eight signals" | the scan summary | 78 signals |
| "nine left" | the priority tier | 9 to review |

**Sources:**
- DC-BASE reconciliation, 0 mismatches: docs/stage-gate-notes.md, results (the MRD-201
  facts).
- Two-model agreement, with 0 wrong "yes" answers: docs/phase3-summary.md, "Evaluation".
- SAF-0475's assessment: ledger seq 3027.
- 78 signals and 9 priority: docs/signal-scout.md, 1.0.1 results.

## Scene 6: Preclinical, from the rat to the first dose (about 80 s)

**Pages:** `/preclinical` as leela.nair (toxicologist), one panel at a time. The medical
monitor's role does not see preclinical studies, so the whole scene is the toxicologist's
view:
1. Findings.
2. NOAEL, with "Current decisions" and "Earlier decisions".
3. Dose translation, "The working, step by step".
4. Translational, "Safety margins".

**Narration:**
> Now the main thread: a thirteen-week rat study, in SEND, the standard format for animal
> study data. The toxicology monitor compared each dose group with control using Dunnett's
> test, looked for trends across doses with the Jonckheere-Terpstra and Cochran-Armitage
> tests, and treated deaths and tumours as rules. It raised ninety-eight findings, but not
> the key number. The NOAEL, the highest dose with no adverse effect, is the toxicologist's
> call: two milligrams per kilogram per day, with twenty as the lowest dose with an
> adverse effect. In this demonstration the toxicologist is played by the developer, the
> assessment was drafted by an assistant from the agent's findings, and the record says
> so. The fourteen earlier entries were mistaken entries made through an early version of
> the form. They were superseded by the proper decision and are still visible, because
> the ledger never deletes. From the NOAEL, the dose agent follows the FDA's 2005
> method step by step. It converts the rat dose to a human equivalent, divides by a safety
> factor of ten, and scales to a sixty-kilogram adult: one point nine four milligrams. The
> medical monitor accepted it. The translational agent then turns the target organs, liver
> and heart, into a monitoring plan for the first human study, with margins of ten and one
> hundred times.

**Actions:**
1. Findings: scroll the per-dose-group table.
2. NOAEL: show "Current decisions" (M, F, combined, all 2 mg/kg/day) and the rationale's
   last sentence, then open "Earlier decisions (14)" and scroll it.
3. Dose translation: step through the working (NOAEL 2, HED 0.323 mg/kg,
   MRSD 0.0323 mg/kg, 1.94 mg).
4. Translational: show the organs and "Safety margins".

**Highlights:**

| When | Element | Chip |
|---|---|---|
| "ninety-eight findings" | the findings count | 98 findings (1.1.0) |
| "two milligrams" | Current decisions | NOAEL 2 mg/kg/day |
| "the record says so" | the rationale's last sentence | "Assessment drafted by Claude Code from the agent's findings; recorded as Dr. Leela Nair, played by Himanshu, in this demonstration." |
| "fourteen earlier entries" | `Earlier decisions` | 14 superseded, still visible |
| "safety factor of ten" | working step 4 | / 10 |
| "one point nine four milligrams" | working step 5 | 1.94 mg |
| "margins of ten and one hundred" | `Safety margins` | 10 x, 100 x |

**Sources:**
- 98 findings: docs/phase5-summary.md, "What was built" (1.1.0, seq 19828 to 19925).
- The tests used: docs/preclinical-eval.md, "Methods (pre-registered)" (Dunnett, Jonckheere-Terpstra), and src/meridian/preclinical/analysis.py (exact Cochran-Armitage for incidences).
- NOAEL 2 and LOAEL 20, and 17 decisions with 14 superseded: docs/phase5-summary.md,
  "Human decisions".
- The attribution sentence: the rationale of seq 19941 to 19943, quoted verbatim.
- The dose working, 6.2 as the rat divisor and 60 kg: config/default.toml
  [agents.dose_translation] and docs/fih-dose-notes.md.
- Acceptance: seq 19966.
- Organs and margins: docs/translational-notes.md, "Results" (PRE-0237, accepted at seq
  20067).

**Note on the brief.** The brief puts "the attribution note" in this scene, but the
attribution correction belongs to the signal scan (seq 19985), not to the NOAEL. The NOAEL
panel has no such note. I moved it to scene 8, where the correction is shown on the
signals page and in the ledger.

## Scene 7: Portfolio and the IND gate (about 55 s)

**Pages:** `/portfolio` as rahul.iyer (portfolio lead): "Pipeline outlook", then "Stage
gates" with the MRD-101 IND gate request.

**Narration:**
> At the portfolio level, the outlook summarises every asset for the portfolio lead, with
> its top risks and the ledger seq behind each one, built only from what that role may see.
> Then comes the gate: may MRD-101 go to the FDA as an investigational new drug, an IND?
> The gate pack checks six criteria. Four come from regulation, each with its citation,
> and two from company policy. Four were met, one was not met because four IND readiness
> gaps were still open, and one did not apply. The pack gives no verdict. The portfolio
> lead decided: a conditional go, with five conditions. The conditions are the person's,
> written into the decision itself.

**Actions:**
1. Show the outlook cards: MRD-101 at risk, the top risks with seqs, and the open findings
   count with its link (not followed).
2. Expand the IND gate request and scroll its criteria table, then the open items.
3. Show the recorded decision: conditional go, the conditions, and the rationale. It was
   recorded on 20 Nov 2026 at seq 20158; the demo shows it and records nothing.

**Highlights:**

| When | Element | Chip |
|---|---|---|
| "top risks" | the risks list | Top 5, by severity then recency |
| "six criteria" | the `Criteria` table | Regulation or policy |
| "one was not met" | the ind_gaps_resolved row | 4 gaps open |
| "no verdict" | the pack summary | No verdict |
| "a conditional go" | the decision | seq 20158 |
| "the person's" | the conditions list | Written into GateDecision seq 20158 |

**Sources:**
- Pack 20156 (4 met, 1 not met, 1 not applicable) and decision 20158 (five conditions):
  docs/phase5-summary.md, "The IND gate".
- The criteria and their citations: config/gates.toml and docs/stage-gate-notes.md.

## Scene 8: Audit and attribution (about 45 s)

**Pages:**
1. `/quality` as asha.rao: "Audit findings", then QA-0178.
2. `/safety/signals` as karan.mehta, scan 19985 in "Earlier scans", with its correction
   note.
3. `/ledger`, filtered to the event types `signal_scan_requested` and
   `attribution_correction`.

**Narration:**
> An oversight system must itself be auditable. The audit trail reviewer is an agent that
> reads the ledger. It found something real here. An early signal scan was recorded
> as requested by the medical monitor, but it had been started from the command line by
> the developer's assistant, under the medical monitor's name. The ledger cannot be edited,
> so the record stays, and a correction event, seq twenty thousand and sixty-eight, states
> what is true. It is shown next to the original wherever that appears. The command line
> now records only operator identities, never a person's.

**Actions:**
1. Open QA-0178 and highlight its evidence naming seq 19985.
2. Open the earlier scan on the signals page and highlight the "Attribution corrected"
   note.
3. On the ledger page, show events 19985 and 20068 side by side.

**Highlights:**

| When | Element | Chip |
|---|---|---|
| "found something real" | QA-0178 | QA-0178: seq 19985 |
| "a correction event" | the note | AttributionCorrection seq 20068 |
| "operator identities" | event 20068's actor | operator:himanshu |

**Sources:**
- docs/phase5-summary.md, "The attribution correction".
- docs/audit-notes.md (reviewer 1.3.0).
- Stats: attribution corrections, seq 20068.

## Scene 9: Scale (about 45 s)

**Pages:**
1. `/fleet` as asha.rao, a Substrate run's traceability (actor, template, golden snapshot,
   worker pod).
2. A card generated from `uv run meridian stats` on the copy, showing runs by executor and
   cut scope.
3. A card with the phase 4b table (kind against GKE).

**Narration:**
> The agents without network access run in Agent Substrate, with each run in a fresh
> gVisor sandbox cloned from a clean snapshot and deleted afterwards. The same code ran on
> a laptop cluster and on Google's cloud, behind one dispatcher that alone may start
> sandboxes. On the cloud, a full pass of nine hundred and ten runs took two hundred and
> seventy seconds, and every result matched the laptop's byte for byte. In all, the ledger
> holds more than eight thousand five hundred runs, and more than seven thousand of them in
> Substrate.

**Actions:**
1. On `/fleet`, open one Substrate run and highlight its actor and its golden snapshot.
2. Show the stats card.
3. Show the kind and GKE table card.

**Highlights:**

| When | Element | Chip |
|---|---|---|
| "a fresh gVisor sandbox" | the run's actor and snapshot | One actor per run |
| "nine hundred and ten runs" | the GKE row | 910 runs, 270 s |
| "byte for byte" | the "Same drafts as kind" row | 910 of 910 equal |
| "eight thousand five hundred" | the stats card | 8,543 runs, seq 20158 |

**Sources:**
- 910 runs in 270 s, and 910 of 910 equal: docs/phase4b-summary.md, "Measurements" and
  "The proofs".
- 8,543 completed runs, of which 7,297 were in Substrate (6,387 on kind and 910 on GKE):
  stats, "Runs".

## Scene 10: Honest limits and lessons (about 50 s)

**Page:** a closing card (local page) with three limits and three lessons, then the README.

**Narration:**
> Some honest limits. All the data is synthetic or public. This is not a validated system
> for regulated use. Identity is for development only: anyone at the keyboard can claim to
> be anyone. Some evaluations missed their targets, and the project says so: under-reporting of adverse
> events could not be detected on this data at all. What did
> this teach? Measure the measurement first. A positive control is what makes a search that
> finds nothing mean something. And identity is data: one command-line flag put a person's
> name on an event they never touched. The code, every number in this video, and the
> misses are in the repository.

**Actions:**
1. The closing card, with each line highlighted as it is said.
2. The README's "Honest limits" section.

**Highlights:** each limit and each lesson in turn.

**Sources:**
- The limits: README, "Honest limits".
- AE under-reporting: docs/phase1-summary.md, "Known limitations".
- The lessons: docs/lessons.md (Evaluation 1, Evaluation 7, People and attribution 1).

---

## Timing

Measured from the audio (Piper en_US-libritts-high, length scale 1.35; docs/demo/README.md).

| Scene | Target | Words | Measured |
|---|---:|---:|---:|
| 1 What this is | 45 s | 120 | 46.5 s |
| 2 Command Center | 45 s | 108 | 38.4 s |
| 3 RBQM | 50 s | 116 | 41.5 s |
| 4 Containment | 55 s | 116 | 44.2 s |
| 5 Safety | 55 s | 122 | 44.4 s |
| 6 Preclinical | 80 s | 216 | 82.1 s |
| 7 Portfolio | 55 s | 113 | 43.0 s |
| 8 Audit | 45 s | 96 | 37.0 s |
| 9 Scale | 45 s | 93 | 32.3 s |
| 10 Limits | 50 s | 108 | 40.3 s |
| **Total** | **7 to 8 min** | **1,208** | **449.5 s (7 min 30 s)** |

Within 8 minutes, so nothing was cut.

## Pronunciation list

These are given to the TTS engine as phonetic respellings, applied only to the spoken text.
The captions keep the written form.

| Term | Say | Note |
|---|---|---|
| NOAEL | NO-ell | Rhymes with "Noel"; the usual spoken form |
| LOAEL | LO-ell | |
| SEND | send | One word, not spelled out |
| RBQM | R-B-Q-M | Spelled out |
| gVisor | gee-VY-zer | |
| openFDA | open F-D-A | |
| Cochran-Armitage | KOCK-ran AR-mi-tij | |
| Jonckheere-Terpstra | YONK-heer-uh TERP-struh | |
| Dunnett | DUN-it | |
| IND | I-N-D | Spelled out |
| SAE | S-A-E | Spelled out |
| MRD-101 | M-R-D one-oh-one | |
| MRD-201 | M-R-D two-oh-one | |
| kind | kind | The tool's name, like the word |
| xanomeline | zan-OM-eh-leen | Only if a scene names the drug; the current text does not |
| mg/kg | milligrams per kilogram | Written out in the narration already |
| seq | sequence | Said "seq" only where the text does |

## What this script needs from the pipeline (Part 2), for the record

- **Read-only mode.** An environment flag for `meridian api serve` makes every non-GET
  route return 403, and the recorder makes one deliberate POST to prove it.
- **A verified copy.** The copy is checked with `ledger verify`, and its last hash is
  compared with the real ledger's at the same seq.
- **Card pages for scenes 4, 9 and 10.** They are generated from files, not typed in.
- **Ids and cut names** are looked up by the recorder from the copy, not hard-coded, except
  those the narration states (19985, 20068, 20158).
