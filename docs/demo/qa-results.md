# Demo Q&A: run 2, the original 30 questions, decline rule fixed

Questions: [qa-questions.json](qa-questions.json), committed before the code
they evaluate, run once with the page's own search (web/demo/search.mjs: BM25; it declines
when the best passage covers less than 0.5 of the question's idf weight, a word in
no passage weighing as BM25's idf for df = 0). Nothing was tuned on them. Written by
`node web/demo/eval-qa.mjs`.

Hits. One-source: top-1 = the first passage is from an expected source; top-3 = any of the
top 3 is. Two-source: top-1 = the first passage is from either expected source; top-3 =
both expected sources are in the top 3 (stricter). A declined question is no hit.

| Set | Questions | Top-1 hits | Top-3 hits | False declines | Correct declines |
|---|---:|---:|---:|---:|---:|
| Answerable from one source | 20 | 10 | 13 | 4 | |
| Needing two sources | 5 | 2 (first from either) | 1 (both in top 3) | 2 | |
| Not covered | 5 | | | | 4 |

False declines (answerable, but declined): a03, a04, a08, a10, t02, t03.

The index holds 148 passages (transcript segments and document sections).

## Every question

| Id | Question | Declined | Top-1 | Top-3 | Best coverage | First retrieved source |
|---|---|---|---|---|---:|---|
| a01 | Who decides what a finding means, the agent or a person? | no | yes | yes | 0.65 | transcript:scene-1 (0:15) |
| a02 | What is the NOAEL for the rat study? | no | yes | yes | 0.68 | transcript:scene-6 (3:35) |
| a03 | What is the first human starting dose and how was it calculated? | yes | no | no | 0.38 | transcript:scene-6 (4:46) |
| a04 | How long did 910 runs take on GKE? | yes | no | no | 0.41 | docs/phase4b-summary.md#Measurements, kind and GKE |
| a05 | What did the portfolio lead decide at the IND gate? | no | yes | yes | 0.80 | transcript:scene-7 (4:57) |
| a06 | What is the attribution correction at seq 20068? | no | yes | yes | 1.00 | docs/phase5-summary.md#Human decisions recorded in phase 5, and by whom |
| a07 | Why was site 1008 flagged for protocol deviations? | no | yes | yes | 0.75 | transcript:scene-3 (1:25) |
| a08 | How does a leak check know its search works? | yes | no | no | 0.45 | transcript:scene-4 (2:35) |
| a09 | How many safety signals did the scout find and how many need review? | no | no | no | 0.54 | docs/phase5-summary.md#Gaps |
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
| t02 | Compare full pass throughput on kind and on GKE, and what limits GKE | yes | no | no | 0.38 | docs/phase4b-summary.md#Measurements, kind and GKE |
| t03 | What went wrong with attribution and how does the audit reviewer catch it now? | yes | no | no | 0.47 | docs/phase5-summary.md#Human decisions recorded in phase 5, and by whom |
| t04 | Which zones run on OpenShell and which on Substrate, and why? | no | no | no | 1.00 | docs/phase4a-summary.md#Measurements against OpenShell (rbqm.kri_monitor on DC-BASE) |
| t05 | What did the narrator validator miss, and what was the lesson about measuring the guard? | no | yes | no | 0.76 | docs/lessons.md#Evaluation |
| u01 | What is the price of the drug in Europe? | yes |  | correctly declined | 0.25 | transcript:scene-2 (1:10) |
| u02 | How do I deploy this on Microsoft Azure? | yes |  | correctly declined | 0.19 | README.md#Quickstart: a laptop with kind |
| u03 | What were the Phase 3 efficacy results for MRD-201? | yes |  | correctly declined | 0.36 | docs/phase1-summary.md#(intro) |
| u04 | Which patients received the placebo? | no |  | NOT declined | 0.62 | transcript:scene-2 (1:10) |
| u05 | Does the system support single sign-on with Okta? | yes |  | correctly declined | 0.28 | README.md#Honest limits |
