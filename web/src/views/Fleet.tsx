import { api, unwrap } from "../api/client";
import { Hash } from "../components/Hash";
import { ErrorNote, Loading, when } from "../components/common";
import { useApi } from "../queries";

const OUTCOME_CLASS: Record<string, string> = {
  completed: "good",
  accepted: "good",
  not_completed: "critical",
  error: "critical",
  rejected: "warning",
  refused: "warning",
};

export function FleetView() {
  const fleet = useApi(["fleet"], () => unwrap(api.GET("/api/v1/fleet")));
  return (
    <section aria-labelledby="fleet-title" className="stack">
      <div className="view-head">
        <div>
          <h1 id="fleet-title">Agent fleet</h1>
          <p>
            The registered agents, the policy each runs under, and the latest containment check for each
            sandbox policy.
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
                    <th scope="col">Policy</th>
                    <th scope="col">Last run</th>
                    <th scope="col">Outcome</th>
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
                        <span className="mono">{a.policy_file}</span>
                        <br />
                        <Hash value={a.policy_sha256} label={`policy sha256 of ${a.agent_id}`} />
                      </td>
                      <td className="mono">{a.last_run_at ? when(a.last_run_at) : "never"}</td>
                      <td>
                        {a.last_outcome ? (
                          <span className={`chip ${OUTCOME_CLASS[a.last_outcome] ?? ""}`}>
                            {a.last_outcome.replaceAll("_", " ")}
                          </span>
                        ) : (
                          <span className="muted">no run yet</span>
                        )}
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
              <span className="src">Latest per policy, run in a real sandbox</span>
            </div>
            {fleet.data.containment.length === 0 ? <p className="muted">No containment check recorded.</p> : null}
            <div className="stack">
              {fleet.data.containment.map((c) => (
                <div key={c.policy}>
                  <h3>
                    {c.policy}{" "}
                    <span className={`chip ${c.passed ? "good" : "critical"}`}>{c.passed ? "passed" : "failed"}</span>
                  </h3>
                  <p className="muted">
                    {when(c.recorded_at)}, ledger seq <span className="mono">{c.seq}</span>, image{" "}
                    <Hash value={c.image_digest} label={`image digest for ${c.policy}`} />
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
                              <span className={`chip ${k.passed ? "good" : "critical"}`}>{k.outcome}</span>
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
