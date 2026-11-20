# MRD-101's nonclinical data: which public SEND study, and why

Phase 5 step 1, Part 0. Searched 15 Nov 2026.

## What exists

The public SEND example studies are in the PHUSE repository
https://github.com/phuse-org/phuse-scripts, folder `data/send/`, read at commit
`398a6d33ced9359ffb58c46650a6d488811176b1` (licence: MIT, `LICENSE.md` of the repository).
The study and design facts below are read from each study's TS, TX and DM domains.

| Folder | Species, strain | Study type | Duration, route | Groups (dose) | Animals | Domains of interest present |
|---|---|---|---|---|---|---|
| **PointCross** (PC201708, synthetic) | rat, Sprague-Dawley | repeat dose, GLP (FDA) | 13 weeks, oral gavage; 2-week recovery | vehicle, 2, 20, 200 mg/kg/day | 10 per sex per group (main), 5 per sex per group (recovery, all groups), TK satellites at 2, 20, 200; 150 in all | DM TS TX EX DS BW BG CL LB MA MI OM FW, plus DD TF (deaths, tumours) |
| instem (XYZ-12345) | rat, Sprague-Dawley | repeat dose, GLP (FDA) | 1 month, oral gavage; 2-week recovery | vehicle, negative control, 60, 200, 600 mg/kg/day | 15 per sex (main), recovery sets; 241 | all of the above |
| PDS (PDS-FAKEDRUG-111) | rat, Sprague-Dawley | repeat dose, GLP | 1 month, oral gavage; 1-month recovery | vehicle, 20, 200, 400 mg/kg | 10 per sex (main), 5 (recovery, control and high), TK; 124 | all of the above |
| Nimble | rat, Fischer 344 | repeat dose, GLP | 3 weeks, oral | 0, 10, 20 mg/kg/day (two sets share one group code) | uneven: 18 to 32 per sex per group; 100 | all of the above |
| FFU-Contribution-to-FDA | monkey, cynomolgus | repeat dose, non-GLP | 30 days, IV | 0, 4, 6, 8, 12 mg/kg | 2 females per group; 10 | BW LB MA MI OM CL, no FW |
| TOXSCI-24-0062 (BioCelerate) | rat and dog, 2 compounds | repeat dose | 1 month | not inspected (nested folders) | not inspected | XPT sets for cross-study analysis |
| CJ16050, CJUGSEND00, CDISC-Safety-Pharmacology-POC | rat, dog | safety pharmacology (respiratory, cardiovascular) | single dose | | | no LB, OM, MI |
| CBER-POC-Pilot-Study1 to 5 | various | vaccine and gene therapy pilots | | | | not repeat-dose small-molecule studies |

## The choice: PointCross PC201708

It stands in for MRD-101's GLP rat study (relabelled MRD-101-TOX-01, treatment MRD-101):

- **The design a first-in-human package relies on:** a GLP repeat-dose study in the rat with
  a concurrent vehicle control, three log-spaced dose levels, both sexes, 10 per sex per
  group, and recovery animals in every group. Of the candidates, only this one has
  recovery in all groups, which the agent must keep apart from the main study.
- **Every domain asked for** (DM, TS, TX, EX, DS, BW, CL, LB, MA, MI with severity grades,
  OM, FW), plus deaths (three moribund sacrifices) and TK satellites (which the agent must
  exclude).
- **A stated answer to compare against.** It is synthetic: its reviewer's guide
  (`PC201708_draft_nsdrg.docx`, sha256 `ed708c9c6649ef9b3d44c5b74ff316f476e3054bcb2fe735338115b428f9b793`,
  section 6.2) lists the signals built into the data: lower body weight and gain in
  groups 3 and 4; higher AST, ALT and ALP and lower RBC, HGB and HCT in group 4; heavier
  livers in group 4; liver findings in groups 3 and 4; liver tumours and premature deaths
  from hepatocellular carcinoma in group 4. There is no published study report; this list
  is the nearest thing, and is used as such when the agent's findings are compared.
- **Free to use:** the repository is MIT-licensed and PointCross's ReadMe says the study
  "may be freely used for development of tools, applications scripts etc."

Caveats, said plainly: the data are synthetic ("created based on published ranges for rat
parameters, randomized data from public sources, and artificially adjusted to show
signals"), 13 weeks is longer than the 2 to 4 weeks ICH M3(R2) asks before a Phase 1
trial, and MRD-101 is an ALK5 inhibitor whose real toxicology would differ. instem's
1-month study was the runner-up (two control groups, no recovery in every group).

## How it is ingested

- `meridian preclinical fetch`: downloads `bg bw cl dd dm ds ex fw lb ma mi om ts tx` (.xpt)
  from `https://raw.githubusercontent.com/phuse-org/phuse-scripts/<commit>/data/send/PointCross/`
  into `data/raw/send_pointcross/`, append-only, with `fetch_manifest.json`; each file must
  match the sha256 pinned in `config/default.toml` `[preclinical.files]`.
- `meridian preclinical ingest`: checks the hashes again, relabels (source study id
  PC201708 to MRD-101-TOX-01 and treatment PCDRUG to MRD-101 in every text column; the
  source USUBJID kept as SRC_USUBJID), sorts every table, and writes
  `data/send/MRD-101-TOX-01/TOX-FINAL/send/<domain>.parquet` with a manifest. A second
  ingest is a no-op; different content under the same cut id is refused. It records one
  `SendStudyLoaded` event, the preclinical agent's trigger.
- Values, units and SEND codes are as published: the curated tables are the study, not a
  cleaned version of it. Unusable records are the agent's to report.
