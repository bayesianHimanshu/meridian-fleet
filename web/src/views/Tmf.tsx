import { useState } from "react";
import { api, unwrap, type Schemas } from "../api/client";
import {
  ErrorNote,
  Loading,
  SeverityChip,
  StatusChip,
  isStatus,
} from "../components/common";
import { FindingLink } from "../components/text";
import { useApi } from "../queries";

const STUDY = "MRD-201";
const MONTHS = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

type View = Schemas["TmfCompletenessView"];
type Cell = Schemas["TmfCell"];
type Artifact = Schemas["TmfArtifact"];
export type CellStatus = Cell["status"];

/** "20 Jan 2013" from an ISO date; the input unchanged if it is not one. */
export function humanDate(iso: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (!m) return iso;
  return `${String(Number(m[3]))} ${MONTHS[Number(m[2]) - 1] ?? ""} ${m[1] ?? ""}`;
}

/** Each check in plain words. */
export const TMF_CHECK_TEXT: Record<string, string> = {
  tmf_missing_artifact: "Expected record missing",
  tmf_late_filing: "Filed late",
  tmf_approval_after_first_subject: "Approval dated after the first subject",
  tmf_misfiled: "Filed under the wrong site",
  tmf_monitoring_report_missing: "Monitoring visit without a report",
  tmf_qc_overdue: "Quality check overdue",
  tmf_data_quality: "Index record has a missing or unusable field",
};

/** Short column headings for the site-level artifacts; the full name is on hover. */
const SHORT: Record<string, string> = {
  irb_iec_decision: "IRB or IEC approval",
  form_fda_1572: "Form FDA 1572",
  pi_cv: "PI CV",
  site_signature_sheet: "Delegation log",
  protocol_signature_page: "Protocol signature",
  financial_disclosure_form: "Financial disclosure",
  initiation_report: "Initiation report",
  monitoring_visit_report: "Visit reports",
  close_out_report: "Close-out report",
};

export const STATUS_TEXT: Record<CellStatus, string> = {
  filed: "Filed",
  filed_late: "Filed late",
  missing: "Missing",
  not_expected: "Not yet expected",
};

const STATUS_MARK: Record<CellStatus, string> = {
  filed: "Filed",
  filed_late: "Late",
  missing: "Missing",
  not_expected: "-",
};

function pct(v: number | null | undefined): string {
  return v === null || v === undefined
    ? "n/a"
    : `${v.toFixed(v === 100 ? 0 : 1)}%`;
}

/** What a cell shows: the status, and for visit reports how many of the due visits have one. */
export function cellText(cell: Cell, perVisit: boolean): string {
  if (!perVisit || cell.status === "not_expected")
    return STATUS_MARK[cell.status];
  const late = cell.late > 0 ? `, ${String(cell.late)} late` : "";
  return `${String(cell.filed)} of ${String(cell.expected)}${late}`;
}

function MatrixCell({ cell, artifact }: { cell: Cell; artifact: Artifact }) {
  const perVisit = artifact.milestone.endsWith("_visit");
  const text = cellText(cell, perVisit);
  const said = `${artifact.name}: ${STATUS_TEXT[cell.status]}${perVisit && cell.expected ? ` (${text})` : ""}`;
  return (
    <td className={`cell tmf-${cell.status}`} title={said}>
      {cell.finding_ids.length > 0 ? (
        <>
          <span aria-hidden="true">{text}</span>
          <span className="visually-hidden">{said}</span>{" "}
          {cell.finding_ids.map((id) => (
            <FindingLink key={id} id={id} className="mono cell-link">
              <span className="visually-hidden">finding </span>
              {id}
            </FindingLink>
          ))}
        </>
      ) : (
        <>
          <span aria-hidden="true">{text}</span>
          <span className="visually-hidden">{said}</span>
        </>
      )}
    </td>
  );
}

function Matrix({ data }: { data: View }) {
  const columns = data.artifacts.filter((a) => a.level === "site");
  return (
    <section className="panel" aria-labelledby="tmf-matrix-title">
      <div className="panel-head">
        <h2 id="tmf-matrix-title">Site records</h2>
        <span className="src">{data.sites.length} sites</span>
      </div>
      <div
        className="table-wrap heat-wrap"
        tabIndex={0}
        aria-label="Site records table, scrolls sideways"
      >
        <table className="heat tmf-matrix">
          <caption className="visually-hidden">
            Expected site-level records against what is filed, per site, for{" "}
            {data.cut_id}
          </caption>
          <thead>
            <tr>
              <th scope="col">Site</th>
              <th
                scope="col"
                className="num"
                title="Expected records that are filed"
              >
                Complete
              </th>
              <th
                scope="col"
                className="num"
                title="Filed records that were filed in time"
              >
                On time
              </th>
              {columns.map((a) => (
                <th
                  scope="col"
                  key={a.key}
                  title={`${a.name} (${a.zone}); filed within ${String(a.file_within_days)} days`}
                >
                  {SHORT[a.key] ?? a.name}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.sites.map((s) => (
              <tr key={s.site_id}>
                <th scope="row" className="mono">
                  {s.site_id}
                </th>
                <td className="num">{pct(s.completeness_percent)}</td>
                <td className="num">{pct(s.timely_percent)}</td>
                {columns.map((a) => {
                  const cell = s.cells.find((c) => c.artifact === a.key);
                  return cell ? (
                    <MatrixCell key={a.key} cell={cell} artifact={a} />
                  ) : (
                    <td key={a.key} />
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Legend />
    </section>
  );
}

function Legend() {
  return (
    <dl className="legend-list" aria-label="How to read the site records">
      <dt>
        <i className="swatch tmf-filed" aria-hidden="true" />
        Filed
      </dt>
      <dd>a version that counts is in the TMF and was filed in time.</dd>
      <dt>
        <i className="swatch tmf-filed_late" aria-hidden="true" />
        Late
      </dt>
      <dd>
        the latest version was filed more days after its date than its record
        allows.
      </dd>
      <dt>
        <i className="swatch tmf-missing" aria-hidden="true" />
        Missing
      </dt>
      <dd>
        expected and not filed. A version rejected at quality check, or indexed
        under another site, does not count.
      </dd>
      <dt>-</dt>
      <dd>
        not yet expected: the site has not reached the milestone that makes it
        due.
      </dd>
      <dt>Visit reports</dt>
      <dd>
        how many of the site's due monitoring visits have a report, for example
        5 of 6.
      </dd>
      <dt>Links</dt>
      <dd>
        a cell with a finding links to it; findings you may not see are not
        shown.
      </dd>
    </dl>
  );
}

function TrialList({ data }: { data: View }) {
  const names = new Map(data.artifacts.map((a) => [a.key, a]));
  return (
    <section className="panel" aria-labelledby="tmf-trial-title">
      <div className="panel-head">
        <h2 id="tmf-trial-title">Trial records</h2>
        <span className="src">
          {pct(data.trial_completeness_percent)} complete,{" "}
          {pct(data.trial_timely_percent)} on time
        </span>
      </div>
      <ul className="tmf-trial">
        {data.trial.map((c) => {
          const a = names.get(c.artifact);
          return (
            <li key={c.artifact}>
              <span className={`tmf-status tmf-${c.status}`}>
                {STATUS_TEXT[c.status]}
              </span>{" "}
              <span>{a?.name ?? c.artifact}</span>
              {a?.milestone === "on_issue" ? (
                <span className="muted"> (filed when issued)</span>
              ) : null}
              {c.finding_ids.map((id) => (
                <span key={id}>
                  {" "}
                  <FindingLink id={id} className="mono" />
                </span>
              ))}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Findings({ items }: { items: Schemas["TmfFindingItem"][] }) {
  return (
    <section className="panel" aria-labelledby="tmf-findings-title">
      <div className="panel-head">
        <h2 id="tmf-findings-title">Findings</h2>
        <span className="src">{items.length} you may see</span>
      </div>
      {items.length === 0 ? (
        <p className="muted">No finding on this cut.</p>
      ) : (
        <ul className="discrepancies">
          {items.map((f) => (
            <li key={f.finding_id} className="discrepancy">
              <div className="discrepancy-head">
                <SeverityChip severity={f.severity} />
                <StatusChip status={f.status} />
                <span>{TMF_CHECK_TEXT[f.check] ?? f.check}</span>
              </div>
              <p>
                <FindingLink id={f.finding_id} className="mono" /> {f.title}
              </p>
              <p className="muted">
                {f.site_id ? (
                  <>
                    Site <span className="mono">{f.site_id}</span>
                  </>
                ) : (
                  "Trial level"
                )}
              </p>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function TmfView() {
  const cuts = useApi(["cuts", STUDY], () =>
    unwrap(
      api.GET("/api/v1/studies/{study}/cuts", {
        params: { path: { study: STUDY } },
      }),
    ),
  );
  const [chosen, setChosen] = useState<string | null>(null);
  const operational = cuts.data?.find((c) => c.scope === "operational")?.cut_id;
  const cut = chosen ?? operational ?? "DC-BASE";
  const tmf = useApi(["tmf", cut], () =>
    unwrap(api.GET("/api/v1/quality/{cut}/tmf", { params: { path: { cut } } })),
  );
  return (
    <section aria-labelledby="tmf-title" className="stack">
      <div className="view-head">
        <div>
          <h1 id="tmf-title">Trial Master File</h1>
          <p>
            Whether the trial's essential records are in the Trial Master File
            and were filed on time: each site's approvals, investigator records
            and monitoring reports, and the trial's own documents. An inspector
            expects the file to be complete and kept up to date as the trial
            runs. This page only shows what the file holds; nothing can be
            changed here.
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
      {tmf.isPending ? <Loading what="the Trial Master File" /> : null}
      {tmf.isError ? (
        isStatus(tmf.error, 403) ? (
          <p className="message">
            Your role does not review the Trial Master File.
          </p>
        ) : isStatus(tmf.error, 404) ? (
          <p className="message">
            There is no Trial Master File index for this cut.
          </p>
        ) : (
          <ErrorNote error={tmf.error} />
        )
      ) : null}
      {tmf.isSuccess ? (
        <>
          {tmf.data.scope === "evaluation" ? (
            <p className="message" data-testid="evaluation-cut">
              This cut is evaluation data, not the study's current data.
            </p>
          ) : null}
          {tmf.data.run_completed ? null : (
            <p className="message">
              The TMF completeness agent has not run on this cut yet.
            </p>
          )}
          <div className="summary-grid tmf-summary">
            <section className="panel" aria-labelledby="tmf-totals-title">
              <div className="panel-head">
                <h2 id="tmf-totals-title">Whole trial</h2>
              </div>
              <dl className="kv">
                <dt>Complete</dt>
                <dd data-testid="tmf-complete">
                  {pct(tmf.data.completeness_percent)}
                </dd>
                <dt>Filed on time</dt>
                <dd data-testid="tmf-timely">{pct(tmf.data.timely_percent)}</dd>
                <dt>Index exported</dt>
                <dd>
                  {tmf.data.export_date
                    ? humanDate(tmf.data.export_date)
                    : "n/a"}
                </dd>
              </dl>
            </section>
            <TrialList data={tmf.data} />
          </div>
          <Matrix data={tmf.data} />
          <Findings items={tmf.data.findings} />
          <p className="muted">
            Agent <span className="mono">{tmf.data.agent_id}</span>
            {tmf.data.agent_version ? (
              <>
                {" "}
                <span className="mono">{tmf.data.agent_version}</span>
              </>
            ) : null}
            . Deterministic: no language model reads the TMF. Record names
            follow the TMF Reference Model.
          </p>
        </>
      ) : null}
    </section>
  );
}
