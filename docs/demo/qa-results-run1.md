# Demo Q&A: evaluation results, run 1 (the original decline rule)

The 30 questions in [qa-questions.json](qa-questions.json) were committed before the index
was built. They were run once, on the index the recording built, with the page's own search
(web/demo/search.mjs, BM25, declining when the best passage covers less than
0.5 of the question's idf weight). Nothing was tuned on them. This file is
written by `node web/demo/eval-qa.mjs`.

Run 1, kept as it came out; only the labels of the two-source row were made exact
afterwards (the numbers are unchanged). For a two-source question, a top-1 hit means the
first passage comes from either of the two expected sources; a top-3 hit means both
expected sources appear among the top 3, so it is the stricter of the two and can be lower.
One-source questions: a top-1 hit is the first passage from an expected source, a top-3 hit
any of the top 3. A declined question counts as no hit. Run 2, with the decline rule fixed,
is in [qa-results.md](qa-results.md).

| Set | Questions | Top-1 hit | Top-3 hit | Declined |
|---|---:|---:|---:|---:|
| Answerable from one source | 20 | 11 | 14 | 3 |
| Needing two sources | 5 | 3 (first passage from either expected source) | 1 (both expected sources in the top 3) | 1 |
| Not covered (should be declined) | 5 | | | 1 correctly declined |

The index holds 148 passages (transcript segments and document sections). Run 1 was first
made on the index of the recording (147 passages); the README then gained its demo section,
and this table comes from the same rule on the 148-passage index. Every question's outcome
was the same on both.

## Every question

| Id | Question | Declined | Top-1 | Top-3 | Best coverage | First retrieved source |
|---|---|---|---|---|---:|---|
| a01 | Who decides what a finding means, the agent or a person? | no | yes | yes | 0.65 | transcript:scene-1 (0:15) |
| a02 | What is the NOAEL for the rat study? | no | yes | yes | 0.68 | transcript:scene-6 (3:35) |
| a03 | What is the first human starting dose and how was it calculated? | no | yes | yes | 0.63 | transcript:scene-6 (4:46) |
| a04 | How long did 910 runs take on GKE? | yes | no | no | 0.41 | docs/phase4b-summary.md#Measurements, kind and GKE |
| a05 | What did the portfolio lead decide at the IND gate? | no | yes | yes | 0.80 | transcript:scene-7 (4:57) |
| a06 | What is the attribution correction at seq 20068? | no | yes | yes | 1.00 | docs/phase5-summary.md#Human decisions recorded in phase 5, and by whom |
| a07 | Why was site 1008 flagged for protocol deviations? | no | yes | yes | 0.75 | transcript:scene-3 (1:25) |
| a08 | How does a leak check know its search works? | yes | no | no | 0.45 | transcript:scene-4 (2:35) |
| a09 | How many safety signals did the scout find and how many need review? | no | no | no | 0.77 | docs/phase5-summary.md#Gaps |
| a10 | How do two models adjudicate whether two event terms are the same event? | yes | no | no | 0.47 | transcript:scene-5 (3:06) |
| a11 | Which statistical tests does the toxicology monitor use? | no | yes | yes | 0.51 | transcript:scene-6 (3:42) |
| a12 | Why does the ledger stay on one machine? | no | no | no | 0.70 | README.md#Honest limits |
| a13 | Why did the translational bridge become rules instead of a model? | no | no | no | 0.50 | docs/project-summary.md#What was built |
| a14 | Was AE under-reporting detected in phase 1? | no | yes | yes | 0.71 | transcript:scene-10 (7:02) |
| a15 | How much did the GKE sessions cost? | no | yes | yes | 1.00 | docs/phase4b-summary.md#Costs per session (USD, estimates) |
| a16 | How does the narrator keep the Anthropic API key out of its sandbox? | no | no | yes | 0.71 | README.md#Quickstart: a laptop with kind |
| a17 | What are the honest limits of the project? | no | no | yes | 1.00 | README.md#Contents |
| a18 | Why are the earlier NOAEL entries superseded but still visible? | no | yes | yes | 0.63 | transcript:scene-6 (4:22) |
| a19 | What does conformance check in a sandbox? | no | yes | yes | 1.00 | docs/phase2-summary.md#What containment proves, and how |
| a20 | How fast is a Substrate run compared with OpenShell? | no | no | yes | 0.67 | README.md#Running the checks |
| t01 | How did the NOAEL lead to the IND gate decision? | no | yes | yes | 1.00 | docs/phase5-summary.md#Human decisions recorded in phase 5, and by whom |
| t02 | Compare full pass throughput on kind and on GKE, and what limits GKE | no | yes | no | 0.54 | docs/phase4b-summary.md#Measurements, kind and GKE |
| t03 | What went wrong with attribution and how does the audit reviewer catch it now? | yes | no | no | 0.47 | docs/phase5-summary.md#Human decisions recorded in phase 5, and by whom |
| t04 | Which zones run on OpenShell and which on Substrate, and why? | no | no | no | 1.00 | docs/phase4a-summary.md#Measurements against OpenShell (rbqm.kri_monitor on DC-BASE) |
| t05 | What did the narrator validator miss, and what was the lesson about measuring the guard? | no | yes | no | 0.76 | docs/lessons.md#Evaluation |
| u01 | What is the price of the drug in Europe? | yes |  | correctly declined | 0.43 | transcript:scene-2 (1:10) |
| u02 | How do I deploy this on Microsoft Azure? | no |  | NOT declined | 1.00 | README.md#Quickstart: a laptop with kind |
| u03 | What were the Phase 3 efficacy results for MRD-201? | no |  | NOT declined | 0.60 | docs/phase1-summary.md#(intro) |
| u04 | Which patients received the placebo? | no |  | NOT declined | 1.00 | transcript:scene-2 (1:10) |
| u05 | Does the system support single sign-on with Okta? | no |  | NOT declined | 0.63 | README.md#Honest limits |
