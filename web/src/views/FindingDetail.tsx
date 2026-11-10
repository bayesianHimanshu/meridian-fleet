import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type KeyboardEvent, type SyntheticEvent } from "react";
import { Link, useLocation, useParams } from "react-router";
import { api, unwrap, type Schemas } from "../api/client";
import { Hash } from "../components/Hash";
import { ErrorNote, Loading, SeverityChip, StatusChip, Time, isStatus } from "../components/common";
import { ROLE_LABELS, useIdentity } from "../identity";
import { FALLBACK_FROM, FindingLink, WithTimes, isFrom } from "../components/text";
import { useApi } from "../queries";
import { PolicyPair } from "./Fleet";

type Detail = Schemas["FindingDetail"];
type Action = Schemas["DecisionAction"];

const ACTIONS: { value: Action; label: string }[] = [
  { value: "accepted", label: "Accept" },
  { value: "escalated", label: "Escalate" },
  { value: "dismissed", label: "Dismiss" },
];

const CLOSED = new Set(["accepted", "dismissed"]);

/** One line for a closed finding: who closed it, when, why, and where in the ledger. */
export function ClosedLine({ decision }: { decision: Schemas["DecisionView"] }) {
  const { people } = useIdentity();
  const who = people.find((p) => p.actor === decision.actor)?.name ?? decision.actor;
  const verb = decision.action === "accepted" ? "Accepted" : "Dismissed";
  return (
    <p className="message" data-testid="closed-line">
      {verb} by {who} ({ROLE_LABELS[decision.role]}) on <Time iso={decision.recorded_at} />
      {decision.reason ? `: ${decision.reason}` : ", no reason given"}. Ledger seq{" "}
      <span className="mono">{decision.seq}</span>.
    </p>
  );
}

export function DecisionForm({
  findingId,
  status,
  closing,
}: {
  findingId: string;
  status: string;
  closing: Schemas["DecisionView"] | undefined;
}) {
  const { person } = useIdentity();
  const queryClient = useQueryClient();
  const reasonRef = useRef<HTMLInputElement>(null);
  const [action, setAction] = useState<Action | null>(null); // nothing preselected
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const decide = useMutation({
    mutationFn: (chosen: Action) =>
      unwrap(
        api.POST("/api/v1/findings/{finding_id}/decisions", {
          params: { path: { finding_id: findingId } },
          body: { action: chosen, reason: reason.trim() === "" ? null : reason },
        }),
      ),
    onSuccess: () => {
      setReason("");
      void queryClient.invalidateQueries({ queryKey: [person.actor] });
    },
  });

  if (CLOSED.has(status) && closing) {
    return <ClosedLine decision={closing} />;
  }
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
    if (action === null) {
      setLocalError("Choose accept, escalate or dismiss first.");
      return;
    }
    if (action === "dismissed" && reason.trim() === "") {
      setLocalError("Give a reason to dismiss a finding.");
      reasonRef.current?.focus();
      return;
    }
    decide.mutate(action);
  };
  // Enter submits only from the reason field, never from a radio button.
  const guardEnter = (e: KeyboardEvent<HTMLFormElement>) => {
    if (e.key === "Enter" && e.target !== reasonRef.current) {
      e.preventDefault();
    }
  };

  return (
    <form className="decision" onSubmit={submit} onKeyDown={guardEnter} aria-label="Record a decision">
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
      <button type="submit" className="btn primary" disabled={action === null || decide.isPending}>
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

const LOCAL_NARRATOR = "safety.local_narrator";

const VERDICT_TEXT: Record<string, string> = {
  yes: "probably the same event",
  no: "probably different events",
  unsure: "cannot tell",
};
const VERDICT_CLASS: Record<string, string> = { yes: "warning", no: "good", unsure: "" };

/** The local model's same-event judgements about this finding. A judgement only: it
 * never changes, closes or supersedes a finding. Rejected or failed answers show their
 * status and reason codes, never the model's text. */
export function Assessments({ detail }: { detail: Detail }) {
  const items = detail.assessments;
  if (items.length === 0) {
    return null;
  }
  const me = detail.finding.finding_id;
  const accepted = items.filter((a) => a.status === "accepted");
  const other = items.filter((a) => a.status !== "accepted");
  return (
    <section className="panel" aria-labelledby="assess-title">
      <div className="panel-head">
        <h2 id="assess-title">Same-event assessment</h2>
        <span className="src">local model, for a person to read; the finding is unchanged</span>
      </div>
      <div className="stack">
        {accepted.map((a) => (
          <div key={a.seq} data-testid="assessment">
            <p>
              <span className={`chip ${VERDICT_CLASS[a.verdict ?? ""] ?? ""}`}>
                {a.verdict}: {VERDICT_TEXT[a.verdict ?? ""] ?? a.verdict}
              </span>{" "}
              {a.reason}
            </p>
            <p className="muted">
              {a.candidate === "unpaired_pair" ? (
                <>
                  Together with{" "}
                  {a.finding_ids
                    .filter((id) => id !== me)
                    .map((id) => (
                      <FindingLink key={id} id={id} className="mono" />
                    ))}
                  .{" "}
                </>
              ) : null}
              {a.agent_id} {a.agent_version}, local model <span className="mono">{a.model}</span>
              {a.model_digest ? (
                <>
                  {" "}
                  (<Hash value={a.model_digest} label={`model digest of assessment ${a.seq}`} />)
                </>
              ) : null}
              , ledger seq <span className="mono">{a.seq}</span>, <Time iso={a.recorded_at} />
            </p>
            {a.answers.length > 0 ? (
              <ul className="stack" aria-label={`answers of assessment ${a.seq}`}>
                {a.answers.map((x) => (
                  <li key={x.model} data-testid="assessment-answer">
                    <p className="muted">
                      <span className="mono">{x.model}</span>
                      {x.model_digest ? (
                        <>
                          {" "}
                          (<Hash value={x.model_digest} label={`digest of ${x.model}`} />)
                        </>
                      ) : null}
                      {": "}
                      {x.status === "accepted"
                        ? x.verdict
                        : `${x.status} (${x.rejection_reasons.join(", ") || (x.error ?? "").split(":")[0]})`}
                    </p>
                    {x.text !== null ? (
                      <pre className="mono" data-testid="raw-answer">
                        {x.text}
                      </pre>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ))}
        {other.length > 0 ? (
          <p className="muted" data-testid="assessment-not-shown">
            {other.length} answer{other.length === 1 ? "" : "s"} not shown:{" "}
            {other
              .map((a) => `${a.status} (${a.rejection_reasons.join(", ") || (a.error ?? "").split(":")[0]})`)
              .join("; ")}
            .
          </p>
        ) : null}
      </div>
    </section>
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
        <dt>Policy</dt>
        <dd>
          <PolicyPair
            file={suspended?.policy_file}
            fileSha={suspended?.policy_file_sha256}
            appliedSha={suspended?.applied_policy_sha256}
            of="this run"
          />
          {suspended?.applied_policy_sha256 && !suspended.policy_file_sha256 ? (
            <span className="muted" data-testid="policy-file-note">
              Recorded before runs named their policy file: match the applied policy sha256 on the Agent fleet
              page.
            </span>
          ) : null}
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
  const location = useLocation();
  const state: unknown = location.state;
  const carried = typeof state === "object" && state !== null ? (state as { from?: unknown }).from : undefined;
  const from = isFrom(carried) ? carried : FALLBACK_FROM;
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
        <Link to={from.path}>{from.label}</Link>
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
          <h1 id="finding-title">
            <WithTimes text={f.title} />
          </h1>
          <p>
            <WithTimes text={f.summary} />
          </p>
          {f.retired ? (
            <p className="message">
              Retired: version <span className="mono">{f.retired_by_version}</span> of this agent ran on
              this cut and did not raise it again. It stays in the record.
            </p>
          ) : null}
          {f.superseded ? (
            <p className="message">
              A newer version of this agent replaced this finding:{" "}
              <FindingLink id={f.superseded_by ?? ""} />.
            </p>
          ) : null}
        </div>
      </div>

      <div className="grid-2">
        <section className="panel" aria-labelledby="narrative-title">
          <div className="panel-head">
            <h2 id="narrative-title">Plain-language summary</h2>
            {accepted ? (
              <span className="src" data-testid="narrator-source">
                {accepted.narrator_id === LOCAL_NARRATOR ? (
                  <>
                    local model <span className="mono">{accepted.model}</span>, narrator {accepted.narrator_version}
                  </>
                ) : (
                  <>narrator {accepted.narrator_version}</>
                )}
              </span>
            ) : null}
          </div>
          {accepted ? (
            <>
              <h3>
                <WithTimes text={accepted.headline ?? ""} />
              </h3>
              <p>
                <WithTimes text={accepted.explanation ?? ""} />
              </p>
            </>
          ) : f.agent_id.startsWith("safety.") ? (
            <p className="muted" data-testid="safety-summary-note">
              Safety findings are never sent to the cloud language model. They are summarised by a local model on
              this machine, and no local summary of this finding has been accepted yet.
            </p>
          ) : (
            <p className="muted">No accepted narrative for this finding.</p>
          )}
        </section>
        <Traceability detail={d} />
      </div>

      <Assessments detail={d} />

      <section className="panel" aria-labelledby="evidence-title">
        <div className="panel-head">
          <h2 id="evidence-title">Evidence</h2>
          <span className="src">Primary evidence is what the finding rests on</span>
        </div>
        <table className="evidence">
          <thead>
            <tr>
              <th scope="col">Role</th>
              <th scope="col">Value</th>
              <th scope="col">Source</th>
            </tr>
          </thead>
          <tbody>
            {d.evidence.map((e, i) => (
              <tr key={`${e.statistic}-${i}`}>
                <td className="ev-role">
                  <span className={`chip ${e.role === "primary" ? "outline" : ""}`}>{e.role ?? "not stated"}</span>
                </td>
                <td className="ev-value">
                  {e.display ? <WithTimes text={e.display} /> : <span className="mono">{e.value}</span>}
                  <span className="ev-key mono">{e.statistic}</span>
                </td>
                <td className="ev-source mono">
                  {e.source}
                  <span className="ev-key">{e.selector}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      <div className="grid-2">
        <section className="panel" aria-labelledby="decide-title">
          <div className="panel-head">
            <h2 id="decide-title">Decide</h2>
          </div>
          {f.scope === "evaluation" && !CLOSED.has(f.status) ? (
            <p className="message" data-testid="evaluation-note">
              This finding comes from evaluation data, not the study's current data. A decision here is
              recorded in the ledger but does not change anything for the study.
            </p>
          ) : null}
          <DecisionForm findingId={f.finding_id} status={f.status} closing={d.decisions.at(-1)} />
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
                      <td className="mono">
                        <Time iso={h.recorded_at} />
                      </td>
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
