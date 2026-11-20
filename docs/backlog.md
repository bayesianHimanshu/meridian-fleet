# Backlog

These are the open items at the wrap-up. Each one says why it matters and gives a first
step. The source is the doc where the item was found. Items marked (wrap-up) were found
while closing the project.

## Evaluation

- **Narrator 1.2.**
  - *What:* plain and technical display strings, false rejection measured on its own
    outputs, a narrower misattribution rule, and mutations written independently.
  - *Why:* validator 1.1.0 caught all 5 bad narratives and all 240 mutations, but it
    rejected 28 of 47 good 1.0.0 narratives. The mutations share an author with the rules,
    and accepted narratives copy statistical jargon from the displays.
  - *First step:* add a `plain` display beside `display` for each evidence kind, and label
    a fresh set of 1.1.1 outputs to measure false rejection on its own outputs
    (narrator-eval.md, phase2-summary.md).
- **Detection probability over many seeds per fault magnitude.**
  - *Why:* each cut is one draw, so one draw cannot separate a real effect from a lucky
    one. The same 70% AE drop put its site at z = -1.4 to -2.6 across holdouts.
  - *First step:* for each sweep magnitude, generate 50 seeds and report detection
    probability with a binomial interval. This is a script over `faults inject` and
    `eval run` (phase2-summary.md, phase1-summary.md).
- **A longitudinal within-site test for AE under-reporting.**
  - *Why:* between-site dispersion (phi about 8) hides a 70% drop, and the fault inflates
    the dispersion that hides it. A site compared with its own earlier rate does not move
    with the other sites.
  - *First step:* a pre-registered variant comparing each site's AE rate before and after
    a cut date, on the dev cuts only (ae-experiment.md).
- **Toxicologist observations the agent did not raise.**
  - *What:* for example male liver necrosis at 0/0/4/1, and no rule for non-monotonic
    incidences.
  - *Why:* a pattern that peaks at the middle dose is biologically meaningful and is
    missed by trend tests.
  - *First step:* list every observation in noael-assessment-MRD-101-TOX-01.md that no
    finding covers, then pre-register an incidence rule (any dose group at least k above
    control, monotonic or not) with its null rate (preclinical-eval.md).
- **Tox monitor: the male red cell decrease, and noisy recovery-only findings.**
  - *Why:* a true haematology effect is missed (male HGB/RBC), and recovery-only
    differences fill the queue.
  - *First step:* measure the haematology magnitude in the holdout copies, then
    pre-register a recovery-phase rule that needs the main-phase effect too
    (preclinical-eval.md).
- **Tox monitor 1.1.0's other misses.**
  - *What:*
    - 5.95 statistical findings per null cut (target 5);
    - 3 null cuts above 10;
    - holdout power: male body weight gain 60%, female liver weight 60%, female HGB 70%;
    - 98 findings on the real study, against 50 to 90 expected.
  - *Why:* the agent over-reports on noise and under-detects some planted effects.
  - *First step:* measure what the bootstrap's repeated animals add to the null rate, and
    run it before any rule change (preclinical-eval.md).
- **SAE reconciler: the sealed holdout cases still open.**
  - *What:* case 2 (a subject typo plus a recoded term), case 8 (an excluded record leaves
    its counterpart an orphan) and case 10 (a lone same-term pair 9 days apart).
  - *Why:* 8 of 12 against a target of 10.
  - *First step:* case 8 needs no model; match orphans against excluded records before
    reporting them (safety-eval.md, local-model-eval.md).
- **Two serious AEs with the same subject and term in the window can pair the wrong way
  round.**
  - *Why:* this is not measured, because no evaluation creates the case.
  - *First step:* add it to the generator, then to the stress set (safety-notes.md).
- **Term adjudicator.**
  - *What:* no model ever says "unsure"; ADMINISTRATION SITE ERYTHEMA gets a wrong "no"
    from both models; and answers are not repeatable at temperature 0 with a fixed seed.
  - *Why:* abstention comes only from disagreement, and a rerun can move a pair.
  - *First step:* run each pair three times and record the spread before changing any
    prompt (local-model-eval.md).
- **TMF.**
  - *What:*
    - seeded consent dates, including screen failures;
    - misfiling judged from document content;
    - per-site amendment approvals;
    - a site never activated is silent;
    - a left-out record is counted twice;
    - a wrong VERSION hides a late document.
  - *Why:* the pilot has no consent dates (RFICDTC is empty). The DOC_SITE field that the
    misfiling check reads does not exist in real indexes. The TMF evaluations are
    regression checks.
  - *First step:* seed consent dates for every screened subject in the eTMF generator,
    and write a blind stress set with someone other than the rule author (tmf-notes.md,
    tmf-eval.md).
- **Signal scout.**
  - *What:*
    - the triage lists were chosen after seeing 1.0.0's results;
    - the retry is tested only on a fake;
    - one product term misses reports that name the drug only in free text;
    - the label match is exact-phrase only.
  - *Why:* the 9 of 9 match tests the rule, not a blind result. A real 429 has never been
    retried live. Sedation and somnolence are not equated.
  - *First step:* freeze the lists, then run the next scan's new terms through them before
    looking at their tiers; add a MedDRA-synonym table for the label match
    (signal-scout.md).
- **IND readiness: what the inventory leaves out.**
  - *What:* reproductive and other case-by-case toxicology, margins, study adequacy, CMC
    and impurities, and the 120-day rule.
  - *Why:* a "met" today means met for what the inventory knows.
  - *First step:* list each ICH M3(R2) item the inventory has no field for, as a
    "not assessed" row in the pack (fih-dose-notes.md).
- **The first dose rests on defaults.**
  - *What:* the FDA 2005 safety factor of 10, no PAD or MABEL, and an assumed first-in-human
    design.
  - *Why:* the gate's condition 5 (the dog NOAEL) may lower the dose.
  - *First step:* record the FIH design as an event the dose and bridge agents read, and
    re-run dose_translation on the dog NOAEL when it is decided (phase5-summary.md).
- **Independent stress sets.**
  - *Why:* every stress set was written by the same author as its agent, and only the
    user's sealed sets were independent (both missed).
  - *First step:* for the next agent, have someone else write the stress set and its
    ideals before the code (phase3-summary.md).
- **Small evaluation gaps.**
  - *What:*
    - sites with fewer than 5 treated subjects are never tested;
    - site 1008 is a standing deviation false positive on DC-BASE;
    - the AE reference that does not move with one site (trimmed or prior-anchored) was
      never tried;
    - the phase 1 limits are limits on one study.
  - *First step:* pre-register the trimmed reference with the longitudinal test above
    (phase1-summary.md).

## Containment and infrastructure

- **Kernel-level Landlock audit records** as an independent source of filesystem refusals.
  - *Why:* OpenShell 0.1.7 writes no log line for a Landlock refusal, so file refusals are
    known only from the harness's own observation.
  - *First step:* check whether the host kernel's audit subsystem records Landlock denials
    (`audit` rules for the landlock type), then read them in conformance as a second
    source. This may need host configuration, which is the user's call (phase2-summary.md,
    threat-model.md).
- **Substrate: no way to deny suspend to an editor (v1.0.0), and DNS refusals not
  logged.**
  - *Why:* only the dispatcher's code refuses suspend, so a compromised dispatcher could
    put a run's memory in a snapshot. A refused lookup leaves no record.
  - *First step:* follow upstream; propose a role without suspend, revert and tag, or a
    per-verb grant. Until then the per-run snapshot check and the leak check stay
    (phase4b-summary.md).
- **GKE throughput: the single port-forward caps runs at 2 to 4 per second.**
  - *Why:* in-cluster phases fell with a second node, but the tunnel phase stayed at about
    1 s p50 and 19 s p95.
  - *First step:* bench two and four port-forwards in parallel against the same
    dispatcher, then the runner as a pod in the cluster (phase4b-summary.md).
- **Leak check cost on GKE.**
  - *Why:* each check downloads the bucket and the dispatcher pod (about 1.3 GB, USD 0.16),
    most of session 2's egress.
  - *First step:* run the scanner as a pod in the cluster that returns only verdicts and
    the positive control's result (phase4b-summary.md).
- **atenet-router admits exactly one dispatcher (wrap-up).**
  - *Why:* a second copy on the same cluster (the fresh-clone check) had to widen the
    allow-list by hand, then restore it.
  - *First step:* make `dispatcher deploy` refuse, not replace, when the list names
    another dispatcher, with an explicit `--replace-router-ids` (README quickstart).
- **The fresh-clone check did not cover a new Substrate install or cold caches (wrap-up).**
  - *Why:* it ran on the existing cluster with warm uv, Docker and npm caches. The node
    label and the deploy race were found there; others may remain.
  - *First step:* run the quickstart on a second machine from Substrate's own quickstart
    onwards (README).
- **OpenShell read paths.**
  - *What:* any network rule widens the readable paths on 0.1.7, and every zone shares the
    same read-only paths.
  - *Why:* the narrator and local-model sandboxes can read `/usr`, `/lib`, `/etc`,
    `/var/log` and `/proc` (the image, not the trial).
  - *First step:* re-test on the next OpenShell release, which `expected_effective` pins
    (threat-model.md).
- **The local model is reached by address, not identity.**
  - *Why:* whatever listens on 172.17.0.1:11434 answers.
  - *First step:* check the model digest in each answer's metadata against
    `[local_model.digests]` (threat-model.md).
- **Substrate's compensating controls are procedural.**
  - *What:* zones have no read allow-list; refusal evidence names the actor, not the
    policy version; snapshots are unencrypted, and rustfs on kind uses its default
    credentials.
  - *First step:* record the template's spec hash in each PolicyDenied, as ActorSuspended
    does (threat-model.md, substrate-design.md).
- **Dispatcher memory.**
  - *Why:* freed heap copies of the inputs stay in the process until it exits; the restart
    after a pass is the control, not zeroing.
  - *First step:* hold inputs in one preallocated buffer that is zeroed on drop
    (phase4b-summary.md).
- **The bootstrap owner (ate-client) is still a global owner, used by deploy.**
  - *First step:* a deploy identity scoped to the meridian atespaces (threat-model.md).
- **Small operational items.**
  - *What:*
    - the kind node's disk was 89% full in phase 4a (84% at the wrap-up), and atelet
      evicts the image after idle minutes;
    - `image_source_commit` names the image's sources, not host changes made after it;
    - the local model and the cluster do not fit in laptop memory together;
    - Cloud Logging receives whatever a container prints;
    - park.sh's drain is blocked by PodDisruptionBudgets.
  - *First step:* free disk on the node (the user's call), and print counts only from
    every container (phase4a-summary.md, gke-plan.md).

## People, attribution and the API

- **The audit reviewer: a check for an implausibly fast decision.**
  - *Why:* seq 787 and 788 were 47 ms apart and pass. The reviewer sees timings, not
    reading.
  - *First step:* pre-register a minimum time between a finding's display and its
    decision, and measure it on the 28 recorded decisions first (audit-notes.md,
    phase3-summary.md).
- **Opaque cut ids from a canonical scenario form.**
  - *Why:* sweep ids still name their fault (DC-SWEEP-ENTRY-LAG-SPIKE-14). Opaque ids hash
    the scenario file's bytes, so a reformatted file gets a new id.
  - *First step:* hash a canonical form of the parsed scenario, and map the old ids in a
    table (phase1-summary.md).
- **Development identity only.**
  - *Why:* anyone who reaches 127.0.0.1:8765 can claim any listed person.
  - *First step:* OIDC sign-in in front of the API, mapping the token's subject to
    config/people.toml (api-notes.md).
- **"Came through the API" is inferred.**
  - *Why:* the ledger has no channel field, and redacted rows are not judged.
  - *First step:* an optional `channel` on person events written by the API, checked by
    the audit reviewer (phase5-summary.md).
- **The portfolio lead cannot see RBQM or QTL data.**
  - *Why:* the outlook shows MRD-201's QTL as "not shown". Widening the role is a
    governance decision, not an engineering one.
  - *First step:* the decision itself, by the study's owner (stage-gate-notes.md).
- **The outlook narrator (the optional plain-language summary) was not built.**
  - *First step:* build it like rbqm.narrator, with the host writing the opening
    (stage-gate-notes.md).
- **Recorded agent text keeps its precision (wrap-up).**
  - *Why:* the API shows doses at 3 significant figures, but the ledger's text and the
    ledger page show the recorded "1.93548 mg" of outlook 20139 and pack 20156.
  - *First step:* apply the display rule on the ledger page's payload view too, marked as
    a display.

- **The demo's question box: decline rule (demo).**
  - *Status:* run 2 fixed the flaw found in run 1. A word in no passage now counts towards
    a decline, and 10 of 10 held-out uncovered questions were declined. The price is false
    declines: 6 of the original 25 answerable questions and 1 of the 10 held-out ones
    (docs/demo/README.md).
  - *First step:* fold common words (calculate, compare, recorded) to a shared stem, or
    weigh unseen words by a general English frequency list. Measure on a third set,
    written before the change.

## Data

- **The synthetic eTMF index leaves out a lot.**
  - *What:* no document content or signatures; one country, one IRB and one PI per site;
    no staff changes, translations, IRB renewals, safety letters or QC reviewer.
  - *First step:* add PI changes and IRB renewals to the generator, which are the
    commonest real gaps (tmf-notes.md).

## Operations

- **The monthly portfolio review needs a cron line that the operator installs.**
  - *First step:* a systemd user timer file in deploy/, documented, which the operator
    enables (phase5-summary.md).
- **The real deployment runs the image of its last deploy.** After an image build, run
  `substrate deploy` and conformance before the next `agents run` (architecture.md,
  "Operating notes").
