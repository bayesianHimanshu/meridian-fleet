import { api, unwrap } from "../api/client";
import { ErrorNote, Loading } from "../components/common";
import { useIdentity } from "../identity";
import { useApi } from "../queries";

export function UnblindedView() {
  const { person } = useIdentity();
  const allowed = person.role === "unblinded_statistician";
  const findings = useApi(["findings", "restricted"], () =>
    unwrap(api.GET("/api/v1/findings", { params: { query: { scope: "all", include_superseded: true, limit: 500 } } })),
  );
  const cases = useApi(["safety", "unblinded-cases"], () => unwrap(api.GET("/api/v1/safety/unblinded-cases")), {
    enabled: allowed,
  });
  if (!allowed) {
    return <p className="message">The unblinded zone is only for the unblinded statistician.</p>;
  }
  const restricted = findings.data?.items.filter((f) => f.visibility === "restricted") ?? [];
  return (
    <section aria-labelledby="unblinded-title" className="stack">
      <div className="view-head">
        <div>
          <h1 id="unblinded-title">Unblinded zone</h1>
          <p>Restricted findings: treatment-arm information that only you may see.</p>
        </div>
      </div>
      <div className="restricted-panel">
        <h2>No restricted findings exist yet</h2>
        <p>
          The agents that would raise them (efficacy and safety by treatment arm) arrive in a later step.
          When they do, their findings appear here and stay redacted for every other role, in the review
          queue, the ledger and every count.
        </p>
        {findings.isPending ? <Loading what="restricted findings" /> : null}
        {findings.isError ? <ErrorNote error={findings.error} /> : null}
        {findings.isSuccess ? (
          <p>
            Restricted findings in the ledger now: <span className="fig">{restricted.length}</span>.
          </p>
        ) : null}
      </div>
      <div className="restricted-panel">
        <h2>Safety database</h2>
        <p>
          Some expedited safety cases (suspected unexpected serious adverse reactions) carry the subject's
          treatment, as an unblinded process would hold it. That field is split into the unblinded zone when the
          safety database is ingested; no agent and no other role ever reads it, and it is not shown here.
        </p>
        {cases.isPending ? <Loading what="the case count" /> : null}
        {cases.isError ? <ErrorNote error={cases.error} /> : null}
        {cases.isSuccess ? (
          <p data-testid="unblinded-cases">
            On cut <span className="mono">{cases.data.cut_id}</span>,{" "}
            <span className="fig">{cases.data.cases_with_treatment}</span> of{" "}
            <span className="fig">{cases.data.cases}</span> safety cases carry an unblinded treatment field.
          </p>
        ) : null}
      </div>
    </section>
  );
}
