import { useRef, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router";
import { api, unwrap, type Schemas } from "../api/client";
import {
  ErrorNote,
  Loading,
  SeverityChip,
  StatusChip,
  Time,
  isStatus,
} from "../components/common";
import { FindingLink } from "../components/text";
import { useApi } from "../queries";
import { TmfView } from "./Tmf";

/** Each audit check in plain words. */
export const AUDIT_CHECK_TEXT: Record<string, string> = {
  audit_chain_linkage: "Ledger chain does not link",
  audit_unsandboxed_operational_run:
    "Operational run without the sandbox record",
  audit_failed_run_unresolved: "Failed run never completed",
  audit_rapid_decisions: "Many decisions in a few minutes",
  audit_reason_quality: "Dismissal reason too thin or copied",
  audit_decision_on_retired: "Decision on a finding no longer current",
  audit_data_quality: "Audit extract row unusable",
};

const CHAIN_TEXT: Record<Schemas["AuditView"]["chain_linkage"], string> = {
  intact: "Intact: every event links to the one before it, with no gap.",
  broken:
    "Broken: at least one event does not link to the one before it, or seq has a gap.",
  not_reviewed: "Not reviewed yet: no audit review has completed.",
};

const TABS = [
  { id: "tmf", label: "Trial Master File" },
  { id: "audit", label: "Audit trail" },
  { id: "observations", label: "Observations" },
] as const;
type Tab = (typeof TABS)[number]["id"];

function isTab(v: string | null): v is Tab {
  return TABS.some((t) => t.id === v);
}

function AuditTab() {
  const audit = useApi(["quality", "audit"], () =>
    unwrap(api.GET("/api/v1/quality/audit")),
  );
  if (audit.isPending) return <Loading what="the audit trail review" />;
  if (audit.isError) {
    return isStatus(audit.error, 403) ? (
      <p className="message">Your role does not review the audit trail.</p>
    ) : (
      <ErrorNote error={audit.error} />
    );
  }
  const a = audit.data;
  return (
    <div className="stack">
      <div className="summary-grid">
        <section className="panel" aria-labelledby="review-title">
          <div className="panel-head">
            <h2 id="review-title">Last review</h2>
          </div>
          {a.review_to_seq === null ? (
            <p className="muted">No audit review has run yet.</p>
          ) : (
            <dl className="kv" data-testid="review-range">
              <dt>Ledger reviewed</dt>
              <dd>
                seq <span className="mono">{a.review_from_seq}</span> to{" "}
                <span className="mono">{a.review_to_seq}</span>
              </dd>
              <dt>Started</dt>
              <dd>
                {a.last_review_at ? <Time iso={a.last_review_at} /> : "n/a"}
              </dd>
              <dt>Run</dt>
              <dd>
                {a.agent_id} <span className="mono">{a.agent_version}</span>,{" "}
                {a.review_completed ? "completed" : "not completed"}
              </dd>
            </dl>
          )}
        </section>
        <section className="panel" aria-labelledby="chain-title">
          <div className="panel-head">
            <h2 id="chain-title">Chain linkage</h2>
          </div>
          <p
            className={`message${a.chain_linkage === "intact" ? " ok" : ""}`}
            data-testid="chain-result"
          >
            {CHAIN_TEXT[a.chain_linkage]}
          </p>
          <p className="muted">
            The reviewer checks links only; it sees no payload of a restricted
            event. Full hash verification runs on the host (Event ledger page).
          </p>
        </section>
      </div>
      <section className="panel" aria-labelledby="audit-checks-title">
        <div className="panel-head">
          <h2 id="audit-checks-title">By check</h2>
        </div>
        <table>
          <thead>
            <tr>
              <th scope="col">Check</th>
              <th scope="col" className="num">
                Findings
              </th>
            </tr>
          </thead>
          <tbody>
            {a.by_check.map((c) => (
              <tr key={c.check}>
                <th scope="row">
                  {AUDIT_CHECK_TEXT[c.check] ?? c.check}
                  <span className="sub mono">{c.check}</span>
                </th>
                <td className={`num${c.count === 0 ? " muted" : ""}`}>
                  {c.count}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      <section className="panel" aria-labelledby="audit-findings-title">
        <div className="panel-head">
          <h2 id="audit-findings-title">Audit findings</h2>
          <span className="src">{a.findings.length} current</span>
        </div>
        {a.findings.length === 0 ? (
          <p className="muted">No current audit finding.</p>
        ) : (
          <ul className="discrepancies">
            {a.findings.map((f) => (
              <li key={f.finding_id} className="discrepancy">
                <div className="discrepancy-head">
                  <SeverityChip severity={f.severity} />
                  <StatusChip status={f.status} />
                  <span>{AUDIT_CHECK_TEXT[f.check] ?? f.check}</span>
                </div>
                <p>
                  <FindingLink id={f.finding_id} className="mono" /> {f.title}
                </p>
                {f.actor_under_review ? (
                  <p className="muted">
                    About the decisions of{" "}
                    <span className="mono">{f.actor_under_review}</span>, who
                    may not decide it.
                  </p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function ObservationsTab() {
  const obs = useApi(["quality", "observations"], () =>
    unwrap(api.GET("/api/v1/quality/observations")),
  );
  if (obs.isPending) return <Loading what="the observations" />;
  if (obs.isError) {
    return isStatus(obs.error, 403) ? (
      <p className="message">Your role does not review quality observations.</p>
    ) : (
      <ErrorNote error={obs.error} />
    );
  }
  return (
    <section className="panel" aria-labelledby="observations-title">
      <div className="panel-head">
        <h2 id="observations-title">QA observations</h2>
        <span className="src">{obs.data.length} you may see</span>
      </div>
      <p className="muted">
        Raised by the QA auditor from a finding page or a ledger row; decided by
        clinical operations or the medical monitor.
      </p>
      {obs.data.length === 0 ? (
        <p className="muted">No observation yet.</p>
      ) : (
        <ul className="discrepancies">
          {obs.data.map((o) => (
            <li key={o.finding_id} className="discrepancy">
              <div className="discrepancy-head">
                <SeverityChip severity={o.severity} />
                <StatusChip status={o.status} />
                <span>
                  Raised by <span className="mono">{o.raised_by}</span> on{" "}
                  <Time iso={o.recorded_at} />
                </span>
              </div>
              <p>
                <FindingLink id={o.finding_id} className="mono" /> {o.title}
              </p>
              {o.refers_to_finding_id ? (
                <p className="muted">
                  About{" "}
                  <FindingLink id={o.refers_to_finding_id} className="mono" />
                </p>
              ) : null}
              {o.refers_to_seq ? (
                <p className="muted">
                  About ledger seq{" "}
                  <span className="mono">{o.refers_to_seq}</span>
                </p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function QualityView() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("tab");
  const tab: Tab = isTab(raw) ? raw : "tmf";
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const choose = (t: Tab) => {
    setParams(t === "tmf" ? {} : { tab: t });
  };
  // Arrow keys move between tabs (the tablist pattern); Tab moves into the panel.
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const i = TABS.findIndex((t) => t.id === tab);
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (step === 0) return;
    e.preventDefault();
    const next = (i + step + TABS.length) % TABS.length;
    const nextTab = TABS[next];
    if (!nextTab) return;
    choose(nextTab.id);
    refs.current[next]?.focus();
  };
  return (
    <div className="stack">
      <div
        className="tabs"
        role="tablist"
        aria-label="Quality and audit"
        onKeyDown={onKey}
      >
        {TABS.map((t, i) => (
          <button
            key={t.id}
            ref={(el) => {
              refs.current[i] = el;
            }}
            id={`tab-${t.id}`}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            aria-controls={`panel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            className={`tab${tab === t.id ? " active" : ""}`}
            onClick={() => {
              choose(t.id);
            }}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === "tmf" ? (
          <TmfView />
        ) : tab === "audit" ? (
          <AuditTab />
        ) : (
          <ObservationsTab />
        )}
      </div>
    </div>
  );
}
