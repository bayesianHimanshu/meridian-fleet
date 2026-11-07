import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type KeyboardEvent, type SyntheticEvent } from "react";
import { Link, useParams } from "react-router";
import { api, unwrap, type Schemas } from "../api/client";
import { Hash } from "../components/Hash";
import { ErrorNote, Loading, SeverityChip, StatusChip, isStatus, when } from "../components/common";
import { ROLE_LABELS, useIdentity } from "../identity";
import { useApi } from "../queries";

type Detail = Schemas["FindingDetail"];
type Action = Schemas["DecisionAction"];

const ACTIONS: { value: Action; label: string }[] = [
  { value: "accepted", label: "Accept" },
  { value: "escalated", label: "Escalate" },
  { value: "dismissed", label: "Dismiss" },
];

export function DecisionForm({ findingId, status }: { findingId: string; status: string }) {
  const { person } = useIdentity();
  const queryClient = useQueryClient();
  const reasonRef = useRef<HTMLInputElement>(null);
  const [action, setAction] = useState<Action>("accepted");
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const decide = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/findings/{finding_id}/decisions", {
          params: { path: { finding_id: findingId } },
          body: { action, reason: reason.trim() === "" ? null : reason },
        }),
      ),
    onSuccess: () => {
      setReason("");
      void queryClient.invalidateQueries({ queryKey: [person.actor] });
    },
  });

  if (person.role === "qa_auditor") {
    return (
      <p className="message" data-testid="qa-note">
        QA auditors review findings and the record, but cannot make decisions.
      </p>
    );
  }

  const submit = (e: SyntheticEvent) => {
    e.preventDefault();
    setLocalError(null);
    if (action === "dismissed" && reason.trim() === "") {
      setLocalError("Give a reason to dismiss a finding.");
      reasonRef.current?.focus();
      return;
    }
    decide.mutate();
  };
  // Enter submits only from the reason field, never from a radio button.
  const guardEnter = (e: KeyboardEvent<HTMLFormElement>) => {
    if (e.key === "Enter" && e.target !== reasonRef.current) {
      e.preventDefault();
    }
  };

  return (
    <form className="decision" onSubmit={submit} onKeyDown={guardEnter} aria-label="Record a decision">
      {status === "accepted" || status === "dismissed" ? (
        <p className="message">This finding is already {status}. A further decision will be refused.</p>
      ) : null}
      <fieldset>
        <legend className="label">Decision</legend>
        {ACTIONS.map((a) => (
          <label key={a.value} className="check">
            <input
              type="radio"
              name="action"
              value={a.value}
              checked={action === a.value}
              onChange={() => {
                setAction(a.value);
              }}
            />
            {a.label}
          </label>
        ))}
      </fieldset>
      <label className="reason">
        Reason {action === "dismissed" ? "(required to dismiss)" : "(optional)"}
        <input
          ref={reasonRef}
          type="text"
          name="reason"
          value={reason}
          maxLength={2000}
          onChange={(e) => {
            setReason(e.target.value);
          }}
        />
      </label>
      <button type="submit" className="btn primary" disabled={decide.isPending}>
        Record decision as {person.name}
      </button>
      <div aria-live="polite">
        {localError ? (
          <p className="message error" role="alert">
            {localError}
          </p>
        ) : null}
        {decide.isError ? <ErrorNote error={decide.error} /> : null}
        {decide.isSuccess ? (
          <p className="message ok">
            Recorded: {decide.data.decision.action} at ledger seq{" "}
            <span className="mono">{decide.data.decision.seq}</span>.
          </p>
        ) : null}
      </div>
    </form>
  );
}

function Traceability({ detail }: { detail: Detail }) {
  const run = detail.run;
  const suspended = run?.suspended;
  return (
    <section className="panel" aria-labelledby="trace-title">
      <div className="panel-head">
        <h2 id="trace-title">Traceability</h2>
        <span className="src">The run that raised this finding</span>
      </div>
      <dl className="facts">
        <dt>Agent</dt>
        <dd>
          <span className="mono">{detail.finding.agent_id}</span> version{" "}
          <span className="mono">{detail.finding.agent_version}</span>
        </dd>
        <dt>Executor</dt>
        <dd>{suspended?.executor ?? run?.woke?.executor ?? "not recorded"}</dd>
        <dt>Image digest</dt>
        <dd>
          <Hash value={suspended?.image_digest} label="image digest" />
        </dd>
        <dt>Policy sha256</dt>
        <dd>
          <Hash value={suspended?.policy_sha256} label="policy sha256" />
        </dd>
        <dt>Inputs manifest sha256</dt>
        <dd>
          <Hash value={suspended?.inputs_manifest_sha256} label="inputs manifest sha256" />
        </dd>
        <dt>Finding event</dt>
        <dd>
          ledger seq <span className="mono">{detail.finding.seq}</span>, hash{" "}
          <Hash value={detail.hash} label="finding event hash" />
        </dd>
        <dt>Run events</dt>
        <dd className="mono">
          {run?.woke ? `woke at seq ${run.woke.seq}` : "no woke event"}
          {suspended ? `, finished at seq ${suspended.seq}` : ", no finished event"}
        </dd>
      </dl>
    </section>
  );
}

export function FindingDetail() {
  const { findingId = "" } = useParams();
  const detail = useApi(["finding", findingId], () =>
    unwrap(api.GET("/api/v1/findings/{finding_id}", { params: { path: { finding_id: findingId } } })),
  );
  if (detail.isPending) {
    return <Loading what="the finding" />;
  }
  if (detail.isError) {
    return isStatus(detail.error, 404) ? (
      <p className="message">This finding does not exist, or your role cannot see it.</p>
    ) : (
      <ErrorNote error={detail.error} />
    );
  }
  const d = detail.data;
  const f = d.finding;
  const accepted = [...d.narratives].reverse().find((n) => n.status === "accepted");
  return (
    <article className="stack" aria-labelledby="finding-title">
      <p>
        <Link to="/review">Back to the review queue</Link>
      </p>
      <div className="view-head">
        <div>
          <div className="finding-meta">
            <span className="mono">{f.finding_id}</span>
            <SeverityChip severity={f.severity} />
            <StatusChip status={f.status} />
            <span>{f.site_id ? `Site ${f.site_id}` : "Study-wide"}</span>
            <span className="mono">{f.cut_id}</span>
            <span className="chip">{f.scope} cut</span>
            {f.visibility === "restricted" ? <span className="chip restricted">restricted</span> : null}
          </div>
          <h1 id="finding-title">{f.title}</h1>
          <p>{f.summary}</p>
          {f.superseded ? (
            <p className="message">
              A newer version of this agent replaced this finding:{" "}
              <Link to={`/findings/${f.superseded_by ?? ""}`}>{f.superseded_by}</Link>.
            </p>
          ) : null}
        </div>
      </div>

      <div className="grid-2">
        <section className="panel" aria-labelledby="narrative-title">
          <div className="panel-head">
            <h2 id="narrative-title">Plain-language summary</h2>
            {accepted ? <span className="src">narrator {accepted.narrator_version}</span> : null}
          </div>
          {accepted ? (
            <>
              <h3>{accepted.headline}</h3>
              <p>{accepted.explanation}</p>
            </>
          ) : (
            <p className="muted">No accepted narrative for this finding.</p>
          )}
        </section>
        <Traceability detail={d} />
      </div>

      <section className="panel" aria-labelledby="evidence-title">
        <div className="panel-head">
          <h2 id="evidence-title">Evidence</h2>
          <span className="src">Primary evidence is what the finding rests on</span>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th scope="col">Role</th>
                <th scope="col">Value</th>
                <th scope="col">Statistic</th>
                <th scope="col">Source</th>
              </tr>
            </thead>
            <tbody>
              {d.evidence.map((e, i) => (
                <tr key={`${e.statistic}-${i}`}>
                  <td>
                    <span className={`chip ${e.role === "primary" ? "accent" : ""}`}>{e.role ?? "not stated"}</span>
                  </td>
                  <td>{e.display ?? <span className="mono">{e.value}</span>}</td>
                  <td className="mono">{e.statistic}</td>
                  <td className="mono muted">{e.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <div className="grid-2">
        <section className="panel" aria-labelledby="decide-title">
          <div className="panel-head">
            <h2 id="decide-title">Decide</h2>
          </div>
          <DecisionForm findingId={f.finding_id} status={f.status} />
        </section>
        <section className="panel" aria-labelledby="history-title">
          <div className="panel-head">
            <h2 id="history-title">Decision history</h2>
            <span className="src">{d.decisions.length} recorded</span>
          </div>
          {d.decisions.length === 0 ? (
            <p className="muted">No decisions yet.</p>
          ) : (
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">When</th>
                    <th scope="col">Decision</th>
                    <th scope="col">By</th>
                    <th scope="col">Reason</th>
                    <th scope="col">Ledger</th>
                  </tr>
                </thead>
                <tbody>
                  {d.decisions.map((h) => (
                    <tr key={h.seq}>
                      <td className="mono">{when(h.recorded_at)}</td>
                      <td>
                        <StatusChip status={h.action} />
                      </td>
                      <td>
                        {h.actor}
                        <br />
                        <span className="muted">{ROLE_LABELS[h.role]}</span>
                      </td>
                      <td>{h.reason ?? <span className="muted">none given</span>}</td>
                      <td>
                        <span className="mono">seq {h.seq}</span>
                        <br />
                        <Hash value={h.hash} label="decision event hash" />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </article>
  );
}
