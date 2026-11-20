import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading } from "../components/common";
import { useApi } from "../queries";
import { PortfolioReviews } from "./PortfolioReviews";

const STAGES: { id: Schemas["AssetStatic"]["stage"]; label: string }[] = [
  { id: "preclinical", label: "Preclinical" },
  { id: "phase_1", label: "Phase 1" },
  { id: "phase_2", label: "Phase 2" },
  { id: "phase_3", label: "Phase 3" },
];

const STATUS_CLASS: Record<Schemas["AssetStatus"]["level"], string> = {
  good: "good",
  warning: "warning",
  critical: "critical",
  info: "outline",
};

export function PortfolioView() {
  const portfolio = useApi(["portfolio"], () => unwrap(api.GET("/api/v1/portfolio")));
  return (
    <section aria-labelledby="portfolio-title" className="stack">
      <div className="view-head">
        <div>
          <h1 id="portfolio-title">Portfolio</h1>
          <p>
            Meridian's four assets. Fields marked static are written in configuration and never computed.
            Live fields come from the ledger: MRD-201's open findings; MRD-101's current NOAEL decision, its IND
            readiness, and its planned first-in-human starting dose with the chain behind it.
          </p>
        </div>
      </div>
      {portfolio.isPending ? <Loading what="the portfolio" /> : null}
      {portfolio.isError ? <ErrorNote error={portfolio.error} /> : null}
      {portfolio.isSuccess ? (
        <div className="pipeline">
          {STAGES.map((stage) => (
            <section key={stage.id} className="stage" aria-label={stage.label}>
              <div className="label">{stage.label}</div>
              {portfolio.data.assets
                .filter((a) => a.static.stage === stage.id)
                .map((a) => (
                  <article key={a.asset_id} className="asset" aria-label={a.asset_id}>
                    <div className="finding-meta">
                      <strong className="mono">{a.asset_id}</strong>
                      <span className="chip static">static</span>
                    </div>
                    <h3>{a.static.title}</h3>
                    <p>{a.static.indication}</p>
                    {a.static.intervention ? <p className="muted">{a.static.intervention}</p> : null}
                    <p className="muted">
                      Next: {a.static.milestone}, {a.static.milestone_due}
                    </p>
                    <div className="bar" role="img" aria-label={`${a.static.progress_percent} percent to milestone`}>
                      <span style={{ width: `${a.static.progress_percent}%` }} />
                    </div>
                    <p className="muted">
                      <span className="fig">{a.static.progress_percent}%</span> to milestone
                      {a.static.probability_of_success ? (
                        <>
                          , probability of success <span className="fig">{a.static.probability_of_success}</span>
                        </>
                      ) : null}
                    </p>
                    <p>
                      <span className={`chip ${STATUS_CLASS[a.static.status.level]}`}>{a.static.status.text}</span>
                    </p>
                    {a.live?.noael_status === null && !a.live.starting_dose ? (
                      <p>
                        <span className="chip outline">live</span>{" "}
                        <span className="fig">{a.live.open_findings}</span> open findings to review
                      </p>
                    ) : null}
                    {a.live?.noael_status ? <NoaelField live={a.live} /> : null}
                    {a.live?.ind_status ? <IndField live={a.live} /> : null}
                    {a.live?.starting_dose ? <StartingDoseField dose={a.live.starting_dose} /> : null}
                    {a.live?.monitoring ? <MonitoringField brief={a.live.monitoring} /> : null}
                  </article>
                ))}
            </section>
          ))}
        </div>
      ) : null}
      <PortfolioReviews />
    </section>
  );
}

function NoaelField({ live }: { live: Schemas["AssetLive"] }) {
  const sexes: Record<string, string> = { M: "males", F: "females", combined: "both sexes" };
  return (
    <p data-testid="noael-field">
      <span className="chip outline">live</span>{" "}
      {live.noael_status === "not_shown" ? (
        <span className="muted">NOAEL: not shown to your role</span>
      ) : live.noael_status === "not_yet_decided" ? (
        <>NOAEL: not yet decided</>
      ) : (
        <>
          NOAEL:{" "}
          {(live.noael ?? []).map((n, i) => (
            <span key={n.decision_id}>
              {i ? "; " : ""}
              {n.established ? (
                <span className="fig">
                  {String(n.noael_dose)} {n.dose_unit}
                </span>
              ) : (
                "not established"
              )}{" "}
              ({sexes[n.sex]})
            </span>
          ))}
        </>
      )}
    </p>
  );
}

function IndField({ live }: { live: Schemas["AssetLive"] }) {
  return (
    <p data-testid="ind-field">
      <span className="chip outline">live</span>{" "}
      {live.ind_status === "not_shown" ? (
        <span className="muted">IND readiness: not shown to your role</span>
      ) : live.ind_status === "not_loaded" ? (
        <>IND readiness: no inventory loaded</>
      ) : (
        <>
          IND readiness: <span className="fig">{String(live.ind_readiness_percent ?? 0)}%</span> of first-in-human
          requirements met
        </>
      )}
    </p>
  );
}

const DOSE_STATUS: Record<Schemas["StartingDose"]["status"], string> = {
  planned: "Planned first-in-human starting dose",
  proposed: "Proposed first-in-human starting dose, not yet decided",
  escalated: "Proposed first-in-human starting dose, escalated",
  dismissed: "Proposed first-in-human starting dose, dismissed",
  no_starting_dose: "No first-in-human starting dose",
  no_proposal: "No first-in-human starting dose yet",
  no_decision: "No first-in-human starting dose yet",
  not_shown: "First-in-human starting dose",
};

const STEP_TEXT: Record<Schemas["ChainLink"]["step"], string> = {
  study: "Study",
  noael: "NOAEL",
  proposal: "Proposal",
  decision: "Decision",
};

function mg(v: number | null | undefined): string {
  return v === null || v === undefined ? "" : v.toPrecision(3);
}

export function StartingDoseField({ dose }: { dose: Schemas["StartingDose"] }) {
  if (dose.status === "not_shown") {
    return (
      <p data-testid="dose-field">
        <span className="chip outline">live</span> <span className="muted">First-in-human starting dose: not shown to your role</span>
      </p>
    );
  }
  const withDose = dose.dose_mg !== null && dose.dose_mg !== undefined;
  const chain = dose.chain ?? [];
  return (
    <div data-testid="dose-field" className="stack">
      <p>
        <span className="chip outline">live</span> {DOSE_STATUS[dose.status]}
        {withDose ? (
          <>
            : <span className="fig">{mg(dose.dose_mg)} mg</span> for a {String(dose.human_weight_kg ?? 60)} kg adult
            (MRSD {mg(dose.mrsd_mg_per_kg)} mg/kg)
          </>
        ) : null}
      </p>
      <p className="muted">{dose.why}</p>
      {chain.length ? (
        <ol className="chain" aria-label="From the study to the starting dose">
          {chain.map((link) => (
            <li key={`${link.step}-${String(link.seq)}`}>
              <strong>{STEP_TEXT[link.step]}:</strong> {link.label}
              {link.who ? <> ({link.who})</> : null}, ledger seq <span className="mono">{link.seq}</span>
            </li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

const MONITORING_TEXT: Record<Schemas["MonitoringBrief"]["status"], string> = {
  none: "no recommendation yet",
  open: "recommended, awaiting the medical monitor",
  accepted: "accepted",
  escalated: "escalated",
  dismissed: "dismissed",
  not_shown: "not shown to your role",
};

export function MonitoringField({ brief }: { brief: Schemas["MonitoringBrief"] }) {
  return (
    <p data-testid="monitoring-field">
      <span className="chip outline">live</span> First-in-human monitoring: {MONITORING_TEXT[brief.status]}
      {brief.organs?.length ? <> ({brief.organs.join(", ")})</> : null}
      {brief.decided_by ? (
        <>
          {" "}
          by {brief.decided_by}, ledger seq <span className="mono">{brief.decided_seq}</span>
        </>
      ) : null}
    </p>
  );
}
