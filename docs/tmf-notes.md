# TMF notes (phase 3 step 4a)

## What the Trial Master File is

The Trial Master File (TMF) is the set of records that lets someone who was not there
reconstruct how a trial was run, and judge whether it was run well: approvals, the
protocol and its amendments, who was qualified and delegated to do what, what monitors
saw, what was decided and when. The sponsor keeps a TMF; each investigator site keeps its
own part (the investigator site file). Today it is usually electronic (an eTMF): a
document store with an index of metadata per document.

ICH E6(R3), the GCP guideline, calls these *essential records* (E6(R2) said "essential
documents") and lists them in its appendix on essential records. The emphasis is on
records that are necessary to evaluate the trial's conduct and the reliability of its
results, kept so they are complete, legible, attributable and available when asked for,
in whatever format, rather than on a fixed checklist of paper documents. Which records
are essential depends on the trial; the sponsor decides and documents it.

## The TMF Reference Model

The TMF Reference Model is the industry's shared taxonomy of TMF content, first an
industry working group's spreadsheet, now published by CDISC as controlled terminology.
It sorts artifacts into zones:

Trial Management, Central Trial Documents, Regulatory, IRB or IEC and Other Approvals,
Site Management, IP and Trial Supplies, Safety Reporting, Central and Local Testing,
Third Parties, Data Management, Statistics.

Each zone has sections and artifacts (for example Site Management holds Form FDA 1572,
Principal Investigator Curriculum Vitae, Site Signature Sheet, Monitoring Visit Report).
The model's own spreadsheet numbers them zone.section.artifact. config/tmf_expected.toml
uses the names and zones of "CDISC TMF Controlled Terminology, 2024-09-27" (NCI EVS),
which has no numbers; the v3.3.1 spreadsheet could not be downloaded (HTTP 404), so no
number is used anywhere rather than one that could not be checked.

16 artifacts were chosen, 7 trial level (Protocol, Protocol Amendment, Investigator's
Brochure, Sample Case Report Form, Informed Consent Form, Monitoring Plan, Regulatory
Authority Decision) and 9 site level (IRB or IEC Decision, Form FDA 1572, Principal
Investigator Curriculum Vitae, Site Signature Sheet as the delegation log, Protocol
Signature Page, Financial Disclosure Form, Trial Initiation Monitoring Report,
Monitoring Visit Report, Final Trial Close Out Monitoring Report). Each has a level, the
milestone that makes it expected, a file_within_days and, for two, a dated_before rule.

## Why contemporaneous filing matters

Inspectors read the TMF as the trial's story. A record filed months after the event is a
record nobody could check against at the time; a gap may mean the event did not happen,
or happened late, or was never overseen. A TMF that is complete only after a clean-up
before an inspection is itself a finding: regulators expect it to be inspection-ready
throughout, which is why filing lag (document date to filed date) and QC backlog are
tracked as metrics, not only completeness.

## Why approval after the first subject is serious

A subject may enter a trial only after the ethics committee (IRB or IEC) has approved it
for that site and, in the US, the investigator has signed the Form FDA 1572 commitment.
An approval dated after the site's first subject means a person may have been treated in
an unapproved trial: a protection failure for that subject, not a paperwork delay. It is
the one check this agent marks high regardless of the record. Two caveats: a later
approval of a protocol amendment is normal (only the initial approval must come first),
and the date compared is the first subject's first treatment, not consent.

## Site milestones from DM

| DM field | Pilot | Used for |
|---|---|---|
| RFICDTC (informed consent) | empty for all 306 subjects | nothing |
| RFSTDTC (first study treatment) | set for the 254 treated subjects | first subject at site (earliest) |
| RFENDTC (end of participation) | set for the same 254 | last subject at site (latest) |

Consent would be the right start: approval must precede consent, and consent comes 2 to
37 days before RFSTDTC in this pilot (DMDTC against RFSTDTC). Using RFSTDTC, an approval
dated between a site's first consent and its first treatment is not flagged. Screen
failures have no RFSTDTC and do not count. Both fields are config
([agents.tmf_completeness]).

## What this synthetic index leaves out

- Documents: only metadata. Nothing checks a signature, a version on the page, or that a
  document is what its index row says.
- A real index rarely carries "the site the document names" (DOC_SITE); here it does,
  and that is the only signal for misfiling. A real check reads the document.
- One country, one IRB per site, one PI per site, no site staff changes (so one CV, one
  1572 per site, apart from harmless versions), no translations, no country or
  regulatory variation, no IRB renewals or progress reports, no safety letters.
- Monitoring visits are generated (SDTM has none) on a regular 6 to 10 week rhythm, and
  each report is dated the visit day or 1 to 3 days later.
- Amendment approvals per site are generated but not checked: the agent does not ask
  whether every site approved every amendment.
- QC is a status per document; there is no QC finding, reviewer or reason.

## The agent's rules and their known failure modes

quality.tmf_completeness 1.0.0 (deterministic; docstring has the full rule):

- A record with an unusable identity field or document date is left out with a
  tmf_data_quality finding; a bad filed date or QC status also gets one, but the
  document still counts as filed. Failure mode: a left-out record then also shows up as
  a missing artifact (two findings for one problem).
- Names match after upper-casing and collapsing whitespace. A truly different spelling
  ("Delegation Log" for Site Signature Sheet) is a data-quality finding, not a match.
- The latest counting version (by VERSION) decides timeliness; superseded versions are
  history. Failure mode: a wrong VERSION number hides a late current document.
- Visit reports match visits by date (within 3 days), nearest first. A report indexed
  under the wrong date leaves its visit "missing" (stress case 07): the index gives no
  better link.
- Expected only once due: a visit report once its window has passed at export, an
  activation record once the site is activated (initiation visit, else first subject).
  A site never activated in the data expects nothing, so a whole missing site is silent.
- Approval check: some counting version on or before the first subject. An approval
  that was replaced by a later one dated after the first subject still passes if the
  first was on time; a missing approval is reported as missing, not as late approval.
- Misfiled documents count for neither site, so the right site also gets a missing
  finding when it had no other copy (by design: its own file is incomplete).

What the evaluation shows and does not show is in docs/tmf-eval.md.
