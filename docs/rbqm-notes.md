# RBQM notes: what the step 3 agents check and why

## Risk-based quality management in one paragraph

ICH E6 is the international guideline for Good Clinical Practice. Its third revision,
E6(R3), asks sponsors to build quality in from the start instead of inspecting everything
afterwards. In practice that means: decide which data and processes are critical to the
trial's conclusions and to participant safety, identify what could go wrong with them,
agree in advance how much deviation is acceptable, then watch the incoming data
centrally and act when something drifts. That is risk-based quality management (RBQM).
Central monitoring, looking at all sites' data together from one place, is one of its
main tools, and it is what our agents do.

## KRIs and QTLs

* A key risk indicator (KRI) is a metric computed per site that signals operational or
  data quality risk: adverse events per subject-day, data entry lag, query ageing,
  protocol deviations. A KRI flag says "look at this site".
* A quality tolerance limit (QTL) is a study-level limit, fixed before the data come in,
  on a parameter that could undermine the trial's conclusions if it drifts too far, such
  as the share of subjects who stop early. Crossing a QTL is a study-wide issue that has
  to be explained and reported, not just a site follow-up.

## The checks in this step

### rbqm.kri_monitor (per site)

* ae_rate: counts a site's adverse events and compares them with what the rest of the
  study implies for the same exposure (subject-days). The comparison rate leaves the site
  out, so a site cannot hide its own anomaly by pulling the average towards itself. The
  Poisson lower tail asks how likely so few events would be by chance.
  Catches: ae_under_reporting, where 70% of a site's AEs were removed.
* deviations: the same leave-one-out design, upper tail: more protocol deviations than
  the rest of the study implies. Catches: deviation_cluster.
* data_entry_lag: the median number of days between a visit and its data being entered.
  Late data means late safety signals. Catches: entry_lag_spike (median 14 days).
* query_aging: the median age of open data queries. A backlog means questions about the
  data go unanswered. Catches: query_backlog (median 40 days).

Both rate tests correct for testing many sites at once (Bonferroni): with 13 sites, a
p value must be 13 times smaller to count.

### rbqm.qtl_detector (study level)

Premature discontinuation: the share of treated subjects whose disposition is anything
other than COMPLETED. The limit was fixed once from DC-BASE (0.567 plus a 0.05 margin,
0.617). From 90% of the limit the status is approaching (medium), from 100% breached
(high). Catches: discontinuation_drift, which pushes the rate to 0.667.

### rbqm.csm (central statistical monitoring of vital signs)

For systolic and diastolic blood pressure and pulse, each site is compared with all other
sites pooled:

* Variance: is the site's spread much smaller than everyone else's? Brown-Forsythe tests
  equality of spread using distances from the median, which is robust to outliers. We only
  care about too little spread, so the test is one-sided.
* Terminal digits: do the site's last digits follow the same mix as the other sites'?
  The comparison is against the other sites, not a uniform 10% each, because honest
  measurement already has habits (see below).

Catches: fabricated_vitals, which shrinks values towards the site mean and rounds 40% of
them to the nearest 10.

## Why a very low AE rate is a risk

It is tempting to read few adverse events as good news. In a trial it is usually the
opposite: the drug and the population are the same everywhere, so a site with far fewer
AEs than its exposure predicts is more likely not to be asking, recording or entering them.
Missed AEs hide safety signals from the people who must protect participants and
understate the drug's risks in the final analysis. That is why ae_rate looks at the lower
tail.

## Why fabricated data shows up as low variance and odd terminal digits

People who invent numbers tend to write values close to what they expect to be typical,
so the invented values cluster around the mean: the spread is too small. People are also
poor at producing random digits. They favour round numbers ending in 0 or 5 and avoid
others, so the last digits of invented readings have a different mix from real ones.
Neither proves fraud on its own, which is why a CSM finding means "check this site's
source documents", not "this site cheated".

## What the real pilot data taught us already

The CDISC pilot sites have strong measurement habits of their own. Several sites almost
never record a blood pressure ending in 5, and at one site 65% of readings end in 0. So a
terminal digit test comparing one site with all others finds a difference at nearly every
site on clean data: digit habits genuinely differ between sites. Pooling also cuts both
ways: when two sites are fabricated, the pooled reference shifts, which can hide the
variance signal (masking) and make clean sites look odd (swamping). The step 3 calibration
report shows the untuned result; tuning is step 4.
