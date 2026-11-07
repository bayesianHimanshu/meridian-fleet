import { useState } from "react";
import { Link } from "react-router";
import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading, SeverityChip, StatusChip, agentName, when } from "../components/common";
import { useApi } from "../queries";

type Finding = Schemas["FindingSummary"];

function uniq(values: string[]): string[] {
  return [...new Set(values)].sort();
}

export function ReviewQueue() {
  const [includeEvaluation, setIncludeEvaluation] = useState(false);
  const [severity, setSeverity] = useState("");
  const [agent, setAgent] = useState("");
  const [site, setSite] = useState("");
  const scope = includeEvaluation ? "all" : "operational";
  const queue = useApi(["review-queue", scope], () =>
    unwrap(api.GET("/api/v1/review-queue", { params: { query: { scope } } })),
  );

  const items: Finding[] = queue.data?.items ?? [];
  const shown = items.filter(
    (f) =>
      (!severity || f.severity === severity) &&
      (!agent || f.agent_id === agent) &&
      (!site || (f.site_id ?? "Study-wide") === site),
  );

  return (
    <section aria-labelledby="queue-title">
      <div className="view-head">
        <div>
          <h1 id="queue-title">Review queue</h1>
          <p>
            Open and escalated findings you can see, newest first. Only the study's current data is
            shown unless you include evaluation cuts; findings replaced by a newer agent version
            are left out.
          </p>
        </div>
      </div>
      <div className="toolbar" role="group" aria-label="Filters">
        <label className="field">
          Severity
          <select
            value={severity}
            onChange={(e) => {
              setSeverity(e.target.value);
            }}
          >
            <option value="">All</option>
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
          </select>
        </label>
        <label className="field">
          Agent
          <select
            value={agent}
            onChange={(e) => {
              setAgent(e.target.value);
            }}
          >
            <option value="">All</option>
            {uniq(items.map((f) => f.agent_id)).map((a) => (
              <option key={a} value={a}>
                {agentName(a)}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Site
          <select
            value={site}
            onChange={(e) => {
              setSite(e.target.value);
            }}
          >
            <option value="">All</option>
            {uniq(items.map((f) => f.site_id ?? "Study-wide")).map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={includeEvaluation}
            onChange={(e) => {
              setIncludeEvaluation(e.target.checked);
            }}
          />
          Include evaluation cuts
        </label>
      </div>
      {queue.isPending ? <Loading what="the review queue" /> : null}
      {queue.isError ? <ErrorNote error={queue.error} /> : null}
      {queue.isSuccess ? (
        <>
          <p className="muted" aria-live="polite">
            Showing {shown.length} of {items.length} findings.
          </p>
          {shown.length === 0 ? (
            <p className="message">Nothing waits for review with these filters.</p>
          ) : (
            <ul className="findings" aria-label="Findings to review">
              {shown.map((f) => (
                <li key={f.finding_id} className="finding">
                  <span className={`stripe ${f.visibility === "restricted" ? "restricted" : f.severity}`} aria-hidden="true" />
                  <div>
                    <div className="finding-meta">
                      <span className="mono">{f.finding_id}</span>
                      <SeverityChip severity={f.severity} />
                      <StatusChip status={f.status} />
                      <span>{f.site_id ? `Site ${f.site_id}` : "Study-wide"}</span>
                      <span>
                        {agentName(f.agent_id)} {f.agent_version}
                      </span>
                      <span className="mono">{f.cut_id}</span>
                      {f.scope === "evaluation" ? <span className="chip">evaluation cut</span> : null}
                      <span>{when(f.raised_at)}</span>
                    </div>
                    <h3>
                      <Link to={`/findings/${f.finding_id}`}>{f.title}</Link>
                    </h3>
                    {f.latest_narrative ? (
                      <p className="headline">{f.latest_narrative.headline}</p>
                    ) : (
                      <p className="headline muted">No accepted narrative yet.</p>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      ) : null}
    </section>
  );
}
