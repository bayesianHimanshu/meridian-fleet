import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading } from "../components/common";
import { useApi } from "../queries";

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
            Meridian's four assets. Fields marked static are written in configuration and never computed;
            only MRD-201 has live data, its open findings from the ledger.
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
                      <span className="num">{a.static.progress_percent}%</span> to milestone
                      {a.static.probability_of_success ? (
                        <>
                          , probability of success <span className="num">{a.static.probability_of_success}</span>
                        </>
                      ) : null}
                    </p>
                    <p>
                      <span className={`chip ${STATUS_CLASS[a.static.status.level]}`}>{a.static.status.text}</span>
                    </p>
                    {a.live ? (
                      <p>
                        <span className="chip outline">live</span>{" "}
                        <span className="num">{a.live.open_findings}</span> open findings to review
                      </p>
                    ) : null}
                  </article>
                ))}
            </section>
          ))}
        </div>
      ) : null}
    </section>
  );
}
