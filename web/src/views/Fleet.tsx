import { Link } from "react-router";
import { api, unwrap, type Schemas } from "../api/client";
import { Hash } from "../components/Hash";
import { ErrorNote, Loading, Time } from "../components/common";
import { useApi } from "../queries";

const OUTCOME_CLASS: Record<string, string> = {
  completed: "good",
  accepted: "good",
  running: "", // started, still within its timeout: neither done nor failed
  not_completed: "critical",
  error: "critical",
  rejected: "warning",
  refused: "warning",
};

/** The ledger, newest first, showing only this agent's run events. Failed runs stay there
 * as they were recorded: an ActorWoke with no ActorSuspended after it. */
export function runsLink(a: Schemas["FleetAgent"]): string {
  const q = new URLSearchParams({ agent: a.agent_id });
  for (const t of a.run_event_types) {
    q.append("type", t);
  }
  return `/ledger?${q.toString()}`;
}

/** The two policy hashes, labelled the same way here and in a finding's traceability. */
export function PolicyPair({
  file,
  fileSha,
  appliedSha,
  of,
}: {
  file: string | null | undefined;
  fileSha: string | null | undefined;
  appliedSha: string | null | undefined;
  of: string;
}) {
  return (
    <dl className="policy-pair">
      <dt>policy file sha256</dt>
      <dd>
        {file ? <span className="mono">{file}</span> : null}{" "}
        {fileSha ? (
          <Hash value={fileSha} label={`policy file sha256 of ${of}`} />
        ) : (
          <span className="muted">not recorded</span>
        )}
      </dd>
      <dt>applied policy sha256</dt>
      <dd>
        {appliedSha ? (
          <Hash value={appliedSha} label={`applied policy sha256 of ${of}`} />
        ) : (
          <span className="muted">not recorded</span>
        )}
      </dd>
    </dl>
  );
}

/** Where an Agent Substrate run happened (phase 4a), labelled the same way here and in a
 * finding's traceability: release, template and its spec hash, pool, sandbox class, the
 * golden snapshot the actor was cloned from, the commit the image was built from, and
 * the actor. */
export function SubstratePlacement({
  run,
  of,
}: {
  run: Schemas["SubstrateRun"];
  of: string;
}) {
  return (
    <dl className="policy-pair" data-testid="substrate-placement">
      <dt>Agent Substrate</dt>
      <dd>
        <span className="mono">{run.version}</span> commit{" "}
        <Hash value={run.commit} label={`substrate commit of ${of}`} />
      </dd>
      <dt>template</dt>
      <dd>
        <span className="mono">{run.template}</span> spec sha256{" "}
        <Hash
          value={run.template_sha256}
          label={`template spec sha256 of ${of}`}
        />
      </dd>
      <dt>template file sha256</dt>
      <dd>
        <span className="mono">{run.template_file}</span>{" "}
        <Hash
          value={run.template_file_sha256}
          label={`template file sha256 of ${of}`}
        />
      </dd>
      <dt>image source commit</dt>
      <dd>
        <Hash
          value={run.image_source_commit}
          label={`image source commit of ${of}`}
        />
      </dd>
      <dt>worker pool, sandbox</dt>
      <dd>
        <span className="mono">{run.worker_pool}</span>,{" "}
        <span className="mono">{run.sandbox_class}</span>
      </dd>
      <dt>golden snapshot</dt>
      <dd className="mono">{run.golden_snapshot}</dd>
      <dt>actor</dt>
      <dd className="mono">
        {run.actor}
        {run.actor_uid ? ` (${run.actor_uid})` : ""}
      </dd>
    </dl>
  );
}

export function FleetView() {
  const fleet = useApi(["fleet"], () => unwrap(api.GET("/api/v1/fleet")));
  return (
    <section aria-labelledby="fleet-title" className="stack">
      <div className="view-head">
        <div>
          <h1 id="fleet-title">Agent fleet</h1>
          <p>
            The registered agents, the policy each runs under, and the latest
            containment check for each sandbox policy. The policy file sha256 is
            the committed file, comments included. The applied policy sha256 is
            what the sandbox reports as effective, without comments, and is what
            each run records. Zone policies that differ only in their header
            comment apply the same policy.
          </p>
        </div>
      </div>
      {fleet.isPending ? <Loading what="the fleet" /> : null}
      {fleet.isError ? <ErrorNote error={fleet.error} /> : null}
      {fleet.isSuccess ? (
        <>
          <section className="panel" aria-labelledby="agents-title">
            <div className="panel-head">
              <h2 id="agents-title">Agents</h2>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Agent</th>
                    <th scope="col">Version</th>
                    <th scope="col">Zone</th>
                    <th scope="col">Executor and policy</th>
                    <th scope="col">Last run</th>
                    <th scope="col">Outcome</th>
                    <th scope="col" className="num">
                      Runs, 7 days
                    </th>
                    <th scope="col" className="num">
                      Failed, 7 days
                    </th>
                    <th scope="col">Last failure</th>
                  </tr>
                </thead>
                <tbody>
                  {fleet.data.agents.map((a) => (
                    <tr key={a.agent_id}>
                      <td>
                        <span className="mono">{a.agent_id}</span>
                        <br />
                        <span className="muted">{a.role}</span>
                      </td>
                      <td className="mono">{a.version}</td>
                      <td>{a.zone}</td>
                      <td>
                        <span className="chip">{a.executor}</span>{" "}
                        <span className="mono">{a.policy}</span>
                        <PolicyPair
                          file={a.policy_file}
                          fileSha={a.policy_file_sha256}
                          appliedSha={a.applied_policy_sha256}
                          of={a.agent_id}
                        />
                        {a.substrate ? (
                          <SubstratePlacement
                            run={a.substrate}
                            of={`the last run of ${a.agent_id}`}
                          />
                        ) : null}
                      </td>
                      <td className="mono">
                        {a.last_run_at ? <Time iso={a.last_run_at} /> : "never"}
                      </td>
                      <td>
                        {a.last_outcome ? (
                          <span
                            className={`chip ${OUTCOME_CLASS[a.last_outcome] ?? ""}`}
                          >
                            {a.last_outcome.replaceAll("_", " ")}
                          </span>
                        ) : (
                          <span className="muted">no run yet</span>
                        )}
                      </td>
                      <td className="num">{a.runs_7d}</td>
                      <td className="num">
                        {a.failed_7d > 0 ? (
                          <span className="chip critical">{a.failed_7d}</span>
                        ) : (
                          a.failed_7d
                        )}
                      </td>
                      <td className="mono">
                        {a.last_failure_at ? (
                          <Time iso={a.last_failure_at} />
                        ) : (
                          <span className="muted">none</span>
                        )}
                        <br />
                        <Link to={runsLink(a)}>Run events in the ledger</Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section className="panel" aria-labelledby="contain-title">
            <div className="panel-head">
              <h2 id="contain-title">Containment checks</h2>
              <span className="src">
                Latest per policy, run in a real sandbox
              </span>
            </div>
            {fleet.data.containment.length === 0 ? (
              <p className="muted">No containment check recorded.</p>
            ) : null}
            <div className="stack">
              {fleet.data.containment.map((c) => (
                <div key={`${c.policy} ${c.executor}`}>
                  <h3>
                    {c.policy} <span className="chip">{c.executor}</span>{" "}
                    <span className={`chip ${c.passed ? "good" : "critical"}`}>
                      {c.passed ? "passed" : "failed"}
                    </span>
                  </h3>
                  <p className="muted">
                    <Time iso={c.recorded_at} />, ledger seq{" "}
                    <span className="mono">{c.seq}</span>, image{" "}
                    <Hash
                      value={c.image_digest}
                      label={`image digest for ${c.policy}`}
                    />
                    , applied policy sha256{" "}
                    {c.applied_policy_sha256 ? (
                      <Hash
                        value={c.applied_policy_sha256}
                        label={`applied policy sha256 for ${c.policy}`}
                      />
                    ) : (
                      "not recorded"
                    )}
                    {c.credential_value_visible === null
                      ? null
                      : c.credential_value_visible
                        ? ". A real API key was visible inside the sandbox."
                        : ". Only a placeholder key was visible inside the sandbox."}
                  </p>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th scope="col">Check</th>
                          <th scope="col">Expected</th>
                          <th scope="col">Result</th>
                          <th scope="col">Message</th>
                        </tr>
                      </thead>
                      <tbody>
                        {c.checks.map((k) => (
                          <tr key={k.name}>
                            <td className="mono">{k.name}</td>
                            <td>{k.expect}</td>
                            <td>
                              <span
                                className={`chip ${k.passed ? "good" : "critical"}`}
                              >
                                {k.outcome}
                              </span>
                            </td>
                            <td className="mono muted">{k.message}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              ))}
            </div>
          </section>
        </>
      ) : null}
    </section>
  );
}
