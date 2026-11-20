import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type SyntheticEvent } from "react";
import { Link } from "react-router";
import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading, Time, isStatus } from "../components/common";
import { useIdentity } from "../identity";
import { useApi } from "../queries";

type AssetOutlook = Schemas["AssetOutlook"];
type GateRequestView = Schemas["GateRequestView"];

const OUTLOOK_STATUS: Record<
  AssetOutlook["status"],
  { text: string; chip: string }
> = {
  on_track: { text: "On track", chip: "good" },
  attention: { text: "Needs attention", chip: "warning" },
  at_risk: { text: "At risk", chip: "critical" },
  static: { text: "Static", chip: "static" },
};

const CRITERION_STATUS: Record<Schemas["CriterionResult"]["status"], string> = {
  met: "met",
  not_met: "not met",
  not_applicable: "not applicable",
};

const DECISIONS: {
  id: Schemas["GateDecisionBody"]["decision"];
  label: string;
}[] = [
  { id: "go", label: "Go" },
  { id: "conditional_go", label: "Conditional go" },
  { id: "no_go", label: "No-go" },
];

const RATIONALE_MIN = 20;

function Seqs({ seqs }: { seqs: number[] }) {
  if (seqs.length === 0) return <span className="muted">no ledger seq</span>;
  return <span className="mono">seq {seqs.join(", ")}</span>;
}

function AssetCard({ asset }: { asset: AssetOutlook }) {
  const s = OUTLOOK_STATUS[asset.status];
  return (
    <article
      className="panel"
      aria-label={`Outlook ${asset.asset_id}`}
      data-testid={`outlook-${asset.asset_id}`}
    >
      <div className="panel-head">
        <h3 className="mono">{asset.asset_id}</h3>
        <span className={`chip ${s.chip}`}>
          {asset.static ? `static: ${asset.status_text}` : s.text}
        </span>
      </div>
      {asset.risks.length ? (
        <>
          <h4>Top risks</h4>
          <ul aria-label={`Top risks of ${asset.asset_id}`}>
            {asset.risks.map((r) => (
              <li key={r.text}>
                <span
                  className={`chip ${r.severity === "high" ? "critical" : "warning"}`}
                >
                  {r.severity}
                </span>{" "}
                {r.text} (<Seqs seqs={r.seqs} />)
              </li>
            ))}
          </ul>
          {asset.more_risks ? (
            <p className="muted" data-testid={`more-risks-${asset.asset_id}`}>
              and {String(asset.more_risks)} more{" "}
              {asset.more_risks === 1 ? "risk" : "risks"}:{" "}
              <Link to={`/review?study=${encodeURIComponent(asset.asset_id)}`}>
                see the review queue for {asset.asset_id}
              </Link>
            </p>
          ) : null}
        </>
      ) : (
        <p className="muted">
          {asset.static
            ? "No live data: static fields only."
            : "No risk found by the outlook's rules."}
        </p>
      )}
      {asset.open_findings ? (
        <p data-testid={`open-findings-${asset.asset_id}`}>
          <span className="fig">{String(asset.open_findings)}</span> open{" "}
          {asset.open_findings === 1 ? "finding" : "findings"}, counted in the facts:{" "}
          <Link to={`/review?study=${encodeURIComponent(asset.asset_id)}`}>
            open them in the review queue
          </Link>
        </p>
      ) : null}
      <h4>Changes since the previous outlook</h4>
      <ul aria-label={`Changes of ${asset.asset_id}`}>
        {asset.changes.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <details>
        <summary>Facts and sources ({String(asset.facts.length)})</summary>
        <ul>
          {asset.facts.map((f) => (
            <li key={f.key}>
              {f.label}: {f.value || f.status}{" "}
              {f.static ? (
                <span className="chip static">static</span>
              ) : f.seq ? (
                <span className="mono">seq {f.seq}</span>
              ) : null}
            </li>
          ))}
        </ul>
      </details>
    </article>
  );
}

function RequestReview({ onDone }: { onDone: () => void }) {
  const [note, setNote] = useState("");
  const [reviewing, setReviewing] = useState(false);
  const ids = useId();
  const ask = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/portfolio/reviews", {
          body: { note: note.trim() || null },
        }),
      ),
    onSuccess: () => {
      setReviewing(false);
      setNote("");
      onDone();
    },
  });
  return (
    <form
      className="stack"
      aria-label="Request a portfolio review"
      onSubmit={(e: SyntheticEvent) => {
        e.preventDefault();
        setReviewing(true);
      }}
    >
      <label className="field" htmlFor={`${ids}-note`}>
        Note (optional)
        <input
          id={`${ids}-note`}
          value={note}
          maxLength={500}
          onChange={(e) => {
            setNote(e.target.value);
          }}
        />
      </label>
      {!reviewing ? (
        <div>
          <button type="submit" className="btn">
            Review the request
          </button>
        </div>
      ) : (
        <section
          className="panel"
          aria-label="Summary before requesting"
          data-testid="review-summary"
        >
          <p>
            Ask portfolio.pipeline_intelligence for an outlook of all four
            assets, from what the portfolio lead may see.
            {note.trim() ? <> Note: {note.trim()}</> : null}
          </p>
          <div className="toolbar">
            <button
              type="button"
              className="btn"
              disabled={ask.isPending}
              onClick={() => {
                ask.mutate();
              }}
            >
              Confirm and request
            </button>
            <button
              type="button"
              className="btn outline"
              onClick={() => {
                setReviewing(false);
              }}
            >
              Change
            </button>
          </div>
        </section>
      )}
      {ask.isError ? <ErrorNote error={ask.error} /> : null}
      {ask.isSuccess ? (
        <p className="message ok">
          Requested at ledger seq {ask.data.seq}. The agents answer it on their
          next run.
        </p>
      ) : null}
    </form>
  );
}

function RequestGate({
  gates,
  onDone,
}: {
  gates: Schemas["GateDef"][];
  onDone: () => void;
}) {
  const first = gates[0];
  const [choice, setChoice] = useState(
    first ? `${first.asset_id}|${first.gate}` : "",
  );
  const [reviewing, setReviewing] = useState(false);
  const ids = useId();
  const chosen = gates.find((g) => `${g.asset_id}|${g.gate}` === choice);
  const ask = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/portfolio/gates", {
          body: { asset_id: chosen?.asset_id ?? "", gate: chosen?.gate ?? "" },
        }),
      ),
    onSuccess: () => {
      setReviewing(false);
      onDone();
    },
  });
  if (!first) return null;
  return (
    <form
      className="stack"
      aria-label="Request a gate's evidence pack"
      onSubmit={(e: SyntheticEvent) => {
        e.preventDefault();
        setReviewing(true);
      }}
    >
      <label className="field" htmlFor={`${ids}-gate`}>
        Gate
        <select
          id={`${ids}-gate`}
          value={choice}
          onChange={(e) => {
            setChoice(e.target.value);
            setReviewing(false);
          }}
        >
          {gates.map((g) => (
            <option
              key={`${g.asset_id}|${g.gate}`}
              value={`${g.asset_id}|${g.gate}`}
            >
              {g.asset_id}: {g.title}
            </option>
          ))}
        </select>
      </label>
      {!reviewing ? (
        <div>
          <button type="submit" className="btn">
            Review the request
          </button>
        </div>
      ) : chosen ? (
        <section
          className="panel"
          aria-label="Summary before requesting the gate"
          data-testid="gate-summary"
        >
          <p>
            Ask for the evidence pack of {chosen.asset_id}, {chosen.title}:{" "}
            {String(chosen.criteria.length)} criteria (
            {String(
              chosen.criteria.filter((c) => c.basis === "regulation").length,
            )}{" "}
            from regulation,{" "}
            {String(chosen.criteria.filter((c) => c.basis === "policy").length)}{" "}
            company policy). The pack states evidence; you decide.
          </p>
          <div className="toolbar">
            <button
              type="button"
              className="btn"
              disabled={ask.isPending}
              onClick={() => {
                ask.mutate();
              }}
            >
              Confirm and request
            </button>
            <button
              type="button"
              className="btn outline"
              onClick={() => {
                setReviewing(false);
              }}
            >
              Change
            </button>
          </div>
        </section>
      ) : null}
      {ask.isError ? <ErrorNote error={ask.error} /> : null}
      {ask.isSuccess ? (
        <p className="message ok">Requested at ledger seq {ask.data.seq}.</p>
      ) : null}
    </form>
  );
}

function decisionProblem(
  decision: string,
  conditions: string[],
  rationale: string,
): string | null {
  if (decision === "conditional_go" && conditions.length === 0)
    return "A conditional go needs at least one condition.";
  if (decision !== "conditional_go" && conditions.length > 0)
    return "Only a conditional go has conditions.";
  if (rationale.trim().length < RATIONALE_MIN)
    return `The rationale needs at least ${String(RATIONALE_MIN)} characters.`;
  return null;
}

function DecideGate({
  request,
  onDone,
}: {
  request: GateRequestView;
  onDone: () => void;
}) {
  const ids = useId();
  const [decision, setDecision] =
    useState<Schemas["GateDecisionBody"]["decision"]>("no_go");
  const [conditionsText, setConditionsText] = useState("");
  const [rationale, setRationale] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const conditions = conditionsText
    .split("\n")
    .map((c) => c.trim())
    .filter((c) => c.length > 0);
  const record = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/portfolio/gates/{request_seq}/decision", {
          params: { path: { request_seq: request.seq } },
          body: {
            pack_seq: request.pack_seq ?? 0,
            decision,
            conditions,
            rationale: rationale.trim(),
          },
        }),
      ),
    onSuccess: () => {
      setReviewing(false);
      onDone();
    },
  });
  const label = DECISIONS.find((d) => d.id === decision)?.label ?? decision;
  return (
    <form
      className="stack"
      aria-label={`Record the gate decision for seq ${String(request.seq)}`}
      onSubmit={(e: SyntheticEvent) => {
        e.preventDefault();
        const p = decisionProblem(decision, conditions, rationale);
        setProblem(p);
        if (p === null) setReviewing(true);
      }}
    >
      <fieldset>
        <legend>Your decision</legend>
        {DECISIONS.map((d) => (
          <label key={d.id} className="radio">
            <input
              type="radio"
              name={`${ids}-decision`}
              value={d.id}
              checked={decision === d.id}
              onChange={() => {
                setDecision(d.id);
                setReviewing(false);
              }}
            />{" "}
            {d.label}
          </label>
        ))}
      </fieldset>
      {decision === "conditional_go" ? (
        <label className="field" htmlFor={`${ids}-conditions`}>
          Conditions, one per line
          <textarea
            id={`${ids}-conditions`}
            value={conditionsText}
            onChange={(e) => {
              setConditionsText(e.target.value);
              setReviewing(false);
            }}
          />
        </label>
      ) : null}
      <label className="field" htmlFor={`${ids}-rationale`}>
        Rationale (at least {RATIONALE_MIN} characters)
        <textarea
          id={`${ids}-rationale`}
          value={rationale}
          onChange={(e) => {
            setRationale(e.target.value);
            setReviewing(false);
          }}
        />
      </label>
      {problem ? (
        <p className="message error" role="alert">
          {problem}
        </p>
      ) : null}
      {!reviewing ? (
        <div>
          <button type="submit" className="btn">
            Review the decision
          </button>
        </div>
      ) : (
        <section
          className="panel"
          aria-label="Summary before recording the gate decision"
          data-testid="gate-decision-summary"
        >
          <h4>Before you record</h4>
          <dl className="kv">
            <div>
              <dt>Gate</dt>
              <dd>
                {request.request.asset_id},{" "}
                {request.pack?.gate_title ?? request.request.gate} (evidence
                pack seq {String(request.pack_seq)})
              </dd>
            </div>
            <div>
              <dt>Decision</dt>
              <dd>{label}</dd>
            </div>
            {conditions.length ? (
              <div>
                <dt>Conditions</dt>
                <dd>
                  <ul>
                    {conditions.map((c) => (
                      <li key={c}>{c}</li>
                    ))}
                  </ul>
                </dd>
              </div>
            ) : null}
            <div>
              <dt>Rationale</dt>
              <dd>{rationale.trim()}</dd>
            </div>
          </dl>
          <p className="muted">
            Recorded once for this request, under your name; it cannot be
            changed afterwards.
          </p>
          <div className="toolbar">
            <button
              type="button"
              className="btn"
              disabled={record.isPending}
              onClick={() => {
                record.mutate();
              }}
            >
              Confirm and record
            </button>
            <button
              type="button"
              className="btn outline"
              onClick={() => {
                setReviewing(false);
              }}
            >
              Change
            </button>
          </div>
        </section>
      )}
      {record.isError ? <ErrorNote error={record.error} /> : null}
    </form>
  );
}

function GateRequestCard({
  r,
  canDecide,
  onDone,
}: {
  r: GateRequestView;
  canDecide: boolean;
  onDone: () => void;
}) {
  return (
    <article
      className="panel"
      aria-label={`Gate request ${String(r.seq)}`}
      data-testid={`gate-${String(r.seq)}`}
    >
      <div className="panel-head">
        <h3>
          {r.request.asset_id}: {r.pack?.gate_title ?? r.request.gate}
        </h3>
        <span className="src">
          requested by {r.request.requested_by}, seq {r.seq},{" "}
          <Time iso={r.recorded_at} />
        </span>
      </div>
      {r.pack ? (
        <>
          <p>{r.pack.summary}</p>
          <div className="table-wrap" tabIndex={0} aria-label="Criteria">
            <table>
              <thead>
                <tr>
                  <th scope="col">Criterion</th>
                  <th scope="col">Basis</th>
                  <th scope="col">Status</th>
                  <th scope="col">Evidence</th>
                </tr>
              </thead>
              <tbody>
                {r.pack.criteria.map((c) => (
                  <tr key={c.id} data-testid={`criterion-${c.id}`}>
                    <th scope="row">{c.title}</th>
                    <td>
                      {c.basis === "regulation"
                        ? `${c.source ?? ""} ${c.section ?? ""}`
                        : "company policy (our choice)"}
                    </td>
                    <td>
                      <span
                        className={`chip ${c.status === "met" ? "good" : c.status === "not_met" ? "warning" : "outline"}`}
                      >
                        {CRITERION_STATUS[c.status]}
                      </span>
                    </td>
                    <td>
                      {c.explanation} <Seqs seqs={c.seqs} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {r.pack.open_items.length ? (
            <>
              <h4>Open items ({String(r.pack.open_items.length)})</h4>
              <ul aria-label="Open items">
                {r.pack.open_items.map((i) => (
                  <li key={`${i.criterion_id}-${i.text}`}>
                    {i.text}
                    {i.seq ? (
                      <span className="mono"> (seq {i.seq})</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </>
      ) : (
        <p className="muted">
          No evidence pack yet: the agents answer the request on their next run.
        </p>
      )}
      {r.decision ? (
        <p className="message" data-testid="gate-decision">
          <strong>
            {
              DECISIONS.find((d) => d.id === r.decision?.decision.decision)
                ?.label
            }
          </strong>{" "}
          by {r.decision.decision.decided_by} (seq {r.decision.seq}):{" "}
          {r.decision.decision.rationale}
          {r.decision.decision.conditions.length ? (
            <> Conditions: {r.decision.decision.conditions.join("; ")}.</>
          ) : null}
        </p>
      ) : r.pack && canDecide ? (
        <DecideGate request={r} onDone={onDone} />
      ) : r.pack ? (
        <p className="muted">Awaiting the portfolio lead's decision.</p>
      ) : null}
    </article>
  );
}

export function PortfolioReviews() {
  const { person } = useIdentity();
  const queryClient = useQueryClient();
  const outlook = useApi(["portfolio", "outlook"], () =>
    unwrap(api.GET("/api/v1/portfolio/outlook")),
  );
  const gates = useApi(["portfolio", "gates"], () =>
    unwrap(api.GET("/api/v1/portfolio/gates")),
  );
  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: [person.actor] });
  };
  if (outlook.isError && isStatus(outlook.error, 403)) {
    return (
      <p className="message" data-testid="portfolio-reviews-hidden">
        Portfolio outlooks and stage gates are shown to the portfolio lead and
        the QA auditor.
      </p>
    );
  }
  const lead = person.role === "portfolio_lead";
  return (
    <>
      <section className="stack" aria-labelledby="outlook-title">
        <div className="panel-head">
          <h2 id="outlook-title">Pipeline outlook</h2>
          {outlook.data?.outlook ? (
            <span className="src">
              {outlook.data.outlook.review_id}, seq {outlook.data.seq}, by{" "}
              {outlook.data.outlook.agent_id}{" "}
              {outlook.data.outlook.agent_version}
            </span>
          ) : null}
        </div>
        <p className="muted">
          Information, not a decision: built from what the portfolio lead may
          see, each fact with its ledger seq. Not narrated.
        </p>
        {outlook.isPending ? <Loading what="the outlook" /> : null}
        {outlook.isError ? <ErrorNote error={outlook.error} /> : null}
        {outlook.data?.pending_reviews?.length ? (
          <p className="message" data-testid="pending-reviews">
            Requested and not yet answered: seq{" "}
            {(outlook.data.pending_reviews ?? []).join(", ")}.
          </p>
        ) : null}
        {outlook.data?.status === "none" ? (
          <p data-testid="no-outlook">No outlook yet.</p>
        ) : null}
        {outlook.data?.outlook ? (
          <div className="grid">
            {outlook.data.outlook.assets.map((a) => (
              <AssetCard key={a.asset_id} asset={a} />
            ))}
          </div>
        ) : null}
        {lead ? <RequestReview onDone={refresh} /> : null}
      </section>
      <section className="stack" aria-labelledby="gates-title">
        <h2 id="gates-title">Stage gates</h2>
        {gates.isPending ? <Loading what="the gates" /> : null}
        {gates.isError ? <ErrorNote error={gates.error} /> : null}
        {gates.data && lead ? (
          <RequestGate gates={gates.data.gates} onDone={refresh} />
        ) : null}
        {gates.data?.requests.length === 0 ? (
          <p className="muted">No gate requested yet.</p>
        ) : null}
        {gates.data?.requests.map((r) => (
          <GateRequestCard
            key={r.seq}
            r={r}
            canDecide={lead}
            onDone={refresh}
          />
        ))}
      </section>
    </>
  );
}
