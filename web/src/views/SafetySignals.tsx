import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading, StatusChip, isStatus } from "../components/common";
import { FindingLink } from "../components/text";
import { useApi } from "../queries";
import { CorrectionNote } from "./Ledger";

type Row = Schemas["SignalRow"];
type Scan = Schemas["ScanSummary"];

const TIER_LABELS: Record<string, string> = {
  priority: "priority",
  listed: "in the label",
  indication: "indication",
  use: "use or supply",
  weak: "weak",
  not_signal: "not a signal",
};

function ScanLine({ scan }: { scan: Scan }) {
  return (
    <li data-testid={`scan-${String(scan.scan_seq)}`}>
      {scan.scan_id} (seq {scan.scan_seq}, {scan.agent_version ?? "?"}): requested by{" "}
      {scan.requested_by}
      {scan.reuses_responses_of ? `, on the responses of seq ${String(scan.reuses_responses_of)}` : ""};{" "}
      {scan.findings} findings, {scan.open} in the queue.
      <CorrectionNote corrections={scan.corrections} />
    </li>
  );
}

function ratio(v: number | null | undefined, low: number | null | undefined, high: number | null | undefined) {
  if (v === null || v === undefined) return "undefined";
  return `${v.toFixed(2)} (${(low ?? 0).toFixed(2)} to ${(high ?? 0).toFixed(2)})`;
}

function SignalTable({ rows, caption }: { rows: Row[]; caption: string }) {
  return (
    <div className="table-wrap" tabIndex={0} aria-label={caption}>
      <table>
        <caption className="visually-hidden">{caption}</caption>
        <thead>
          <tr>
            <th scope="col">Reaction (MedDRA PT)</th>
            <th scope="col" className="num">
              Reports with the drug
            </th>
            <th scope="col" className="num">
              All reports of the term
            </th>
            <th scope="col">PRR (95% CI)</th>
            <th scope="col">ROR (95% CI)</th>
            <th scope="col" className="num">
              Chi-squared
            </th>
            <th scope="col">US label</th>
            <th scope="col">Tier</th>
            <th scope="col">Finding</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.term} data-testid={`signal-${r.term}`}>
              <th scope="row">{r.term.toLowerCase()}</th>
              <td className="num">
                {r.reports} of {r.drug_reports}
              </td>
              <td className="num">{r.term_reports}</td>
              <td>{ratio(r.prr, r.prr_low, r.prr_high)}</td>
              <td>{ratio(r.ror, r.ror_low, r.ror_high)}</td>
              <td className="num">{r.chi2.toFixed(1)}</td>
              <td>{r.label_listed === null ? "no label" : r.label_listed ? "listed" : "not found"}</td>
              <td data-testid={`tier-${r.term}`}>{TIER_LABELS[r.tier] ?? r.tier}</td>
              <td>
                {r.finding_id ? (
                  <>
                    <FindingLink id={r.finding_id} className="mono" />{" "}
                    {r.status ? <StatusChip status={r.status as Schemas["FindingSummary"]["status"]} /> : null}
                  </>
                ) : (
                  <span className="muted">none</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function SafetySignalsView() {
  const s = useApi(["safety", "signals"], () => unwrap(api.GET("/api/v1/safety/signals")));
  return (
    <section aria-labelledby="signals-title" className="stack">
      <div className="view-head">
        <div>
          <h1 id="signals-title">Safety signals</h1>
          <p>
            Public spontaneous reports (FDA's FAERS, through openFDA) for MRD-201's drug. A signal of
            disproportionate reporting says a reaction is reported with the drug more often, relative to its
            reports, than with all other drugs. It is never an incidence and never evidence that the drug causes
            the reaction; the medical monitor decides each one.
          </p>
          <p className="muted" data-testid="openfda-notice">
            Adverse event reports and labeling are from openFDA (CC0). No endorsement by the FDA or the
            manufacturer is implied.
          </p>
        </div>
      </div>
      {s.isPending ? <Loading what="the safety signals" /> : null}
      {s.isError ? (
        isStatus(s.error, 403) ? (
          <p className="message">Safety signals are shown to the medical monitor and the portfolio lead.</p>
        ) : (
          <ErrorNote error={s.error} />
        )
      ) : null}
      {s.isSuccess && s.data.status !== "scanned" ? (
        <p className="message" data-testid="signals-none">
          {s.data.status === "not_scanned"
            ? "No scan of openFDA has run yet."
            : `The responses of scan ${s.data.scan_id ?? ""} are not on this host.`}
        </p>
      ) : null}
      {s.isSuccess && s.data.status === "scanned" ? (
        <>
          <section className="panel" aria-labelledby="scan-title">
            <div className="panel-head">
              <h2 id="scan-title">Scan {s.data.scan_id}</h2>
              <span className="src">openFDA data date {s.data.data_date}</span>
            </div>
            <p data-testid="scan-summary">
              {s.data.drug}: {String(s.data.drug_reports)} reports of {String(s.data.all_reports)} in FAERS;{" "}
              {String(s.data.terms_with_drug)} reaction terms, {String(s.data.tested)} with at least 3 reports tested,{" "}
              <strong>{String(s.data.signals.length)} signals</strong>. {s.data.rule}.
            </p>
            {s.data.not_answered?.length ? (
              <p className="message" data-testid="not-answered">
                Not tested ({String(s.data.not_answered.length)}): openFDA did not answer their total query:{" "}
                {s.data.not_answered.join(", ").toLowerCase()}.
              </p>
            ) : null}
            <p className="muted mono">{s.data.product_query}</p>
            {s.data.scan ? (
              <ul aria-label="This scan" className="plain">
                <ScanLine scan={s.data.scan} />
              </ul>
            ) : null}
          </section>
          <section className="panel" aria-labelledby="triage-title">
            <div className="panel-head">
              <h2 id="triage-title">Triage</h2>
            </div>
            <p data-testid="triage">{s.data.triage}</p>
            <ul aria-label="Signals by tier">
              {Object.entries(s.data.tiers ?? {}).map(([tier, n]) => (
                <li key={tier}>
                  {TIER_LABELS[tier] ?? tier}: {n}
                </li>
              ))}
            </ul>
            <ul aria-label="Context of these reports">
              {(s.data.context ?? []).map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </section>
          <section className="panel" aria-labelledby="sig-title">
            <div className="panel-head">
              <h2 id="sig-title">Signals of disproportionate reporting</h2>
            </div>
            <SignalTable rows={s.data.signals} caption="Signals, by PRR" />
          </section>
          <section className="panel" aria-labelledby="label-title">
            <div className="panel-head">
              <h2 id="label-title">Against the US label</h2>
              <span className="src">
                {s.data.label_set_id ? `set ${s.data.label_set_id}, version ${s.data.label_version ?? ""}` : "no label"}
              </span>
            </div>
            <p data-testid="label-summary">
              {String(s.data.signals_listed)} of {String(s.data.signals.length)} signals are named in the label's
              adverse reactions, warnings and boxed warning text (the exact term; no synonyms). The label was written
              by others, not by this project.
            </p>
            <ul aria-label="The label's most common adverse reactions">
              {s.data.label_most_common.map((r) => (
                <li key={r.reaction}>
                  {r.reaction}: {r.signal_terms.length ? `a signal (${r.signal_terms.join(", ").toLowerCase()})` : "not a signal"}
                </li>
              ))}
            </ul>
          </section>
          {s.data.earlier_scans?.length ? (
            <section className="panel" aria-labelledby="earlier-title">
              <div className="panel-head">
                <h2 id="earlier-title">Earlier scans</h2>
              </div>
              <ul className="plain">
                {s.data.earlier_scans.map((e) => (
                  <ScanLine key={e.scan_seq} scan={e} />
                ))}
              </ul>
            </section>
          ) : null}
          <details>
            <summary>Terms tested that are not signals ({String(s.data.not_signals.length)})</summary>
            <SignalTable rows={s.data.not_signals} caption="Terms tested, not signals" />
          </details>
          <section className="panel" aria-labelledby="caveats-title">
            <div className="panel-head">
              <h2 id="caveats-title">FAERS caveats</h2>
              <span className="src">{s.data.caveats_source}</span>
            </div>
            <ul>
              {s.data.caveats.map((c) => (
                <li key={c}>
                  <q>{c}</q>
                </li>
              ))}
            </ul>
          </section>
        </>
      ) : null}
    </section>
  );
}
