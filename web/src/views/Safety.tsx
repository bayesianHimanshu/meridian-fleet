import { useState } from "react";
import { Link } from "react-router";
import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading, SeverityChip, StatusChip } from "../components/common";
import { useApi } from "../queries";

const STUDY = "MRD-201";

/** Each check in plain words. */
export const CHECK_TEXT: Record<string, string> = {
  sae_missing_in_safety_db: "Serious AE with no safety case",
  case_without_clinical_sae: "Safety case with no serious AE",
  onset_mismatch: "Onset dates differ",
  outcome_mismatch: "Outcome differs",
  causality_mismatch: "Causality differs",
  late_sae_report: "Case reported late",
  sae_late_reporting_site: "Site reports late",
  sae_data_quality: "Record has a missing or unusable field",
  sae_probable_duplicate: "Two cases are probably one event",
  sae_partial_date: "Onset date is incomplete",
  seriousness_criteria_mismatch: "Seriousness criteria differ",
  sae_subject_mismatch: "Case names the wrong subject",
  sae_term_mismatch: "Preferred terms differ",
};

type Item = Schemas["ReconciliationItem"];

function Compare({ item }: { item: Item }) {
  return (
    <dl className="compare">
      <div>
        <dt>{item.left_label}</dt>
        <dd className="mono">{item.left}</dd>
      </div>
      <div>
        <dt>{item.right_label}</dt>
        <dd className="mono">{item.right}</dd>
      </div>
    </dl>
  );
}

function Summary({ data }: { data: Schemas["Reconciliation"] }) {
  return (
    <div className="summary-grid">
      <section className="panel" aria-labelledby="by-check-title">
        <div className="panel-head">
          <h2 id="by-check-title">By check</h2>
        </div>
        <table>
          <thead>
            <tr>
              <th scope="col">Check</th>
              <th scope="col" className="num">
                Discrepancies
              </th>
            </tr>
          </thead>
          <tbody>
            {data.by_check.map((c) => (
              <tr key={c.check}>
                <th scope="row">
                  {CHECK_TEXT[c.check] ?? c.check}
                  <span className="sub mono">{c.check}</span>
                </th>
                <td className={`num${c.count === 0 ? " muted" : ""}`}>{c.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="panel" aria-labelledby="by-site-title">
        <div className="panel-head">
          <h2 id="by-site-title">By site</h2>
        </div>
        {data.by_site.length === 0 ? (
          <p className="muted">No site has a discrepancy you may see.</p>
        ) : (
          <table>
            <thead>
              <tr>
                <th scope="col">Site</th>
                <th scope="col" className="num">
                  Discrepancies
                </th>
              </tr>
            </thead>
            <tbody>
              {data.by_site.map((s) => (
                <tr key={s.site_id}>
                  <th scope="row" className="mono">
                    {s.site_id}
                  </th>
                  <td className="num">{s.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function Discrepancies({ items }: { items: Item[] }) {
  return (
    <section className="panel" aria-labelledby="disc-title">
      <div className="panel-head">
        <h2 id="disc-title">Discrepancies</h2>
        <span className="src">{items.length} you may see</span>
      </div>
      <ul className="discrepancies">
        {items.map((i) => (
          <li key={i.finding_id} className="discrepancy">
            <div className="discrepancy-head">
              <SeverityChip severity={i.severity} />
              <StatusChip status={i.status} />
              <span>{CHECK_TEXT[i.check] ?? i.check}</span>
            </div>
            <p>
              <Link to={`/findings/${i.finding_id}`} className="mono">
                {i.finding_id}
              </Link>{" "}
              {i.title}
            </p>
            <p className="muted">
              Site <span className="mono">{i.site_id ?? "n/a"}</span>
              {i.subject_id ? (
                <>
                  , subject <span className="mono">{i.subject_id}</span>
                </>
              ) : null}
            </p>
            <Compare item={i} />
          </li>
        ))}
      </ul>
    </section>
  );
}

export function SafetyView() {
  const cuts = useApi(["cuts", STUDY], () =>
    unwrap(api.GET("/api/v1/studies/{study}/cuts", { params: { path: { study: STUDY } } })),
  );
  const [chosen, setChosen] = useState<string | null>(null);
  const operational = cuts.data?.find((c) => c.scope === "operational")?.cut_id;
  const cut = chosen ?? operational ?? "DC-BASE";
  const rec = useApi(["safety", cut], () =>
    unwrap(api.GET("/api/v1/safety/{cut}/reconciliation", { params: { path: { cut } } })),
  );
  return (
    <section aria-labelledby="safety-title" className="stack">
      <div className="view-head">
        <div>
          <h1 id="safety-title">SAE reconciliation</h1>
          <p>
            Serious adverse events in the clinical database compared with the cases in the safety database:
            every serious AE should have one case with the same onset, outcome and causality, received within
            24 hours of the site becoming aware. Subject-level discrepancies are shown only to the roles that
            review them.
          </p>
        </div>
        <label className="field toolbar">
          <span className="muted">Data cut</span>
          <select
            value={cut}
            onChange={(e) => {
              setChosen(e.target.value);
            }}
          >
            {(cuts.data ?? []).map((c) => (
              <option key={c.cut_id} value={c.cut_id}>
                {c.cut_id} ({c.scope}
                {c.label ? `, ${c.label}` : ""})
              </option>
            ))}
          </select>
        </label>
      </div>
      {rec.isPending ? <Loading what="the reconciliation" /> : null}
      {rec.isError ? <ErrorNote error={rec.error} /> : null}
      {rec.isSuccess ? (
        <>
          {rec.data.scope === "evaluation" ? (
            <p className="message" data-testid="evaluation-cut">
              This cut is evaluation data, not the study's current data.
            </p>
          ) : null}
          {rec.data.run_completed ? null : (
            <p className="message">The reconciliation agent has not run on this cut yet.</p>
          )}
          <Summary data={rec.data} />
          {rec.data.items.length === 0 ? (
            rec.data.run_completed ? (
              <p className="message ok">
                No discrepancies you may see on this cut.
              </p>
            ) : null
          ) : (
            <Discrepancies items={rec.data.items} />
          )}
          <p className="muted">
            Agent <span className="mono">{rec.data.agent_id}</span>
            {rec.data.agent_version ? (
              <>
                {" "}
                <span className="mono">{rec.data.agent_version}</span>
              </>
            ) : null}
            . Deterministic: no language model reads safety data.
          </p>
        </>
      ) : null}
    </section>
  );
}
