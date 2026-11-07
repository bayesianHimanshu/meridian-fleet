import { useState } from "react";
import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading, isStatus } from "../components/common";
import { useApi } from "../queries";

type Site = Schemas["SiteMetrics"];
type Flag = Schemas["SiteFlag"];
export type Band = "normal" | "warning" | "critical" | "na";

const STUDY = "MRD-201";

/** z columns: absolute z from 2 to 3 is warning, above 3 critical, below 2 normal. */
export function zBand(z: number | null | undefined): Band {
  if (z === null || z === undefined) return "na";
  const size = Math.abs(z);
  if (size > 3) return "critical";
  return size >= 2 ? "warning" : "normal";
}

/** Medians against the agents' own thresholds: above the threshold the agent flags it
 * (warning), above the high-severity threshold it is critical. */
export function thresholdBand(
  value: number | null | undefined,
  warnAbove: number | undefined,
  criticalAbove: number | undefined,
): Band {
  if (value === null || value === undefined || warnAbove === undefined) return "na";
  if (criticalAbove !== undefined && value > criticalAbove) return "critical";
  return value > warnAbove ? "warning" : "normal";
}

const BAND_TEXT: Record<Band, string> = {
  normal: "normal",
  warning: "warning",
  critical: "critical",
  na: "not computed",
};

function flagText(flags: Flag[]): string {
  return flags.map((f) => `${f.finding_id}, ${f.agent_id} ${f.agent_version} (${f.status})`).join("; ");
}

function Cell({ text, band, flags }: { text: string; band: Band; flags: Flag[] }) {
  const flagged = flags.length > 0;
  return (
    <td
      className={`cell ${band}${flagged ? " flagged" : ""}`}
      title={flagged ? `Flagged by ${flagText(flags)}` : undefined}
      data-flagged={flagged ? flags.map((f) => f.finding_id).join(" ") : undefined}
    >
      {text}
      <span className="visually-hidden">
        , {BAND_TEXT[band]}
        {flagged ? `, flagged by ${flagText(flags)}` : ""}
      </span>
    </td>
  );
}

function fmt(v: number | null | undefined, digits: number): string {
  return v === null || v === undefined ? "n/a" : v.toFixed(digits);
}

const PARAMETER_NAMES: Record<string, string> = {
  SYSBP: "systolic blood pressure",
  DIABP: "diastolic blood pressure",
  PULSE: "pulse",
};

export const COLUMN_HELP = {
  treated: "Subjects at this site who started treatment",
  ae: "How unusual this site's adverse event rate is compared with the rest of the study (negative: fewer than expected)",
  deviation: "How unusual this site's protocol deviation rate is compared with the rest of the study",
  lag: "Median days from a visit to its data being entered",
  query: "Median age in days of the site's open data queries",
  spread: (p: string) =>
    `How unusual the spread of ${PARAMETER_NAMES[p] ?? p} readings at this site is compared with the other sites (negative: less spread)`,
};

/** The current findings at a site that a column shows. */
export function flagsFor(site: Site, check: string, parameter?: string): Flag[] {
  return site.flagged_by.filter(
    (f) =>
      f.check === check &&
      (parameter === undefined || f.parameters.length === 0 || f.parameters.includes(parameter)),
  );
}

function QtlBar({ qtl }: { qtl: Schemas["QtlView"] }) {
  const max = Math.max(qtl.limit * 1.2, qtl.rate ?? 0) || 1;
  const pct = (v: number) => `${((100 * v) / max).toFixed(2)}%`;
  const rate = qtl.rate ?? 0;
  return (
    <section className="panel" aria-labelledby="qtl-title">
      <div className="panel-head">
        <h2 id="qtl-title">Quality tolerance limit: premature discontinuation</h2>
        <span className="src">
          {qtl.discontinued ?? 0} of {qtl.treated} treated subjects
        </span>
      </div>
      <p>
        <span className="num">{qtl.rate === null ? "n/a" : `${(100 * rate).toFixed(1)}%`}</span> discontinued
        against a limit of <span className="num">{(100 * qtl.limit).toFixed(1)}%</span>; approaching from{" "}
        <span className="num">{(100 * qtl.approach_from).toFixed(1)}%</span>. Status:{" "}
        <strong>{qtl.status ?? "not computed"}</strong>.
      </p>
      <div
        className="qtl-bar"
        role="img"
        aria-label={`Rate ${(100 * rate).toFixed(1)} percent, limit ${(100 * qtl.limit).toFixed(1)} percent`}
      >
        <span className="band" style={{ left: pct(qtl.approach_from), width: pct(qtl.limit - qtl.approach_from) }} />
        <span className={`fill ${qtl.status ?? ""}`} style={{ width: pct(rate) }} />
        <span className="limit" style={{ left: pct(qtl.limit) }} />
      </div>
      <div className="qtl-scale" aria-hidden="true">
        <span>0%</span>
        <span>{(100 * max).toFixed(0)}%</span>
      </div>
    </section>
  );
}

export function RbqmView() {
  const cuts = useApi(["cuts", STUDY], () =>
    unwrap(api.GET("/api/v1/studies/{study}/cuts", { params: { path: { study: STUDY } } })),
  );
  const [chosen, setChosen] = useState<string | null>(null);
  const operational = cuts.data?.find((c) => c.scope === "operational")?.cut_id;
  const cut = chosen ?? operational ?? "DC-BASE";
  const sites = useApi(["rbqm", cut], () =>
    unwrap(api.GET("/api/v1/rbqm/{cut}/sites", { params: { path: { cut } } })),
  );

  return (
    <section aria-labelledby="rbqm-title" className="stack">
      <div className="view-head">
        <div>
          <h1 id="rbqm-title">Risk-based quality management</h1>
          <p>
            Site metrics computed with the same statistics the agents use. Colour shows how far a site is
            from the others or from its threshold; an outlined cell is a check an agent flagged.
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
      {sites.isPending ? <Loading what="site metrics" /> : null}
      {sites.isError ? (
        isStatus(sites.error, 403) ? (
          <p className="message">Your role does not see RBQM site metrics.</p>
        ) : (
          <ErrorNote error={sites.error} />
        )
      ) : null}
      {sites.isSuccess ? <Heatmap data={sites.data} /> : null}
      {sites.isSuccess ? <QtlBar qtl={sites.data.qtl} /> : null}
    </section>
  );
}

function Heatmap({ data }: { data: Schemas["RbqmSites"] }) {
  const t = data.thresholds;
  const flags = data.sites.flatMap((s) => s.flagged_by.map((f) => ({ site: s.site_id, ...f })));
  return (
    <section className="panel" aria-labelledby="heat-title">
      <div className="panel-head">
        <h2 id="heat-title">Site heatmap</h2>
        <span className="src mono">{data.cut_id}</span>
      </div>
      <div className="table-wrap">
        <table className="heat">
          <caption className="visually-hidden">Per-site RBQM metrics for {data.cut_id}</caption>
          <thead>
            <tr>
              <th scope="col">Site</th>
              <th scope="col" className="num" title={COLUMN_HELP.treated}>
                Treated
              </th>
              <th scope="col" className="num" title={COLUMN_HELP.ae}>
                AE rate z
              </th>
              <th scope="col" className="num" title={COLUMN_HELP.deviation}>
                Deviation z
              </th>
              <th scope="col" className="num" title={COLUMN_HELP.lag}>
                Entry lag, days
              </th>
              <th scope="col" className="num" title={COLUMN_HELP.query}>
                Open query age, days
              </th>
              {data.parameters.map((p) => (
                <th scope="col" className="num" key={p} title={COLUMN_HELP.spread(p)}>
                  {p} spread z
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.sites.map((s) => (
              <tr key={s.site_id}>
                <th scope="row">
                  <span className="mono">{s.site_id}</span>
                  {s.tested ? null : <span className="sub">too small to test</span>}
                </th>
                <td className="num">{s.treated}</td>
                <Cell text={fmt(s.ae_rate_z, 2)} band={zBand(s.ae_rate_z)} flags={flagsFor(s, "ae_rate")} />
                <Cell text={fmt(s.deviation_z, 2)} band={zBand(s.deviation_z)} flags={flagsFor(s, "deviations")} />
                <Cell
                  text={fmt(s.median_entry_lag_days, 1)}
                  band={thresholdBand(s.median_entry_lag_days, t.data_entry_lag_days, t.data_entry_lag_high_days)}
                  flags={flagsFor(s, "data_entry_lag")}
                />
                <Cell
                  text={fmt(s.median_open_query_age_days, 1)}
                  band={thresholdBand(s.median_open_query_age_days, t.open_query_age_days, t.open_query_age_high_days)}
                  flags={flagsFor(s, "query_aging")}
                />
                {data.parameters.map((p) => (
                  <Cell
                    key={p}
                    text={fmt(s.csm_log_sd_z[p], 2)}
                    band={zBand(s.csm_log_sd_z[p])}
                    flags={flagsFor(s, "variance", p)}
                  />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <dl className="legend-list" aria-label="How to read the heatmap">
        <dt>z columns (AE rate, deviations, spread)</dt>
        <dd>
          <i className="swatch warning" aria-hidden="true" />
          warning when the absolute z is from 2 to 3; <i className="swatch critical" aria-hidden="true" />
          critical above 3; no fill below 2.
        </dd>
        <dt>Entry lag</dt>
        <dd>
          warning above {t.data_entry_lag_days} days (the agent flags it), critical above{" "}
          {t.data_entry_lag_high_days} days (high severity).
        </dd>
        <dt>Open query age</dt>
        <dd>
          warning above {t.open_query_age_days} days (the agent flags it), critical above{" "}
          {t.open_query_age_high_days} days (high severity).
        </dd>
        <dt>
          <i className="swatch flagged" aria-hidden="true" />
          Outline
        </dt>
        <dd>
          a current finding (newest agent version, not superseded or retired, in any status) covers
          this cell:{" "}
          {flags.length === 0
            ? "none on this cut."
            : flags.map((f) => `${f.finding_id} at site ${f.site} by ${f.agent_id} ${f.agent_version}`).join("; ") +
              "."}
        </dd>
        <dt>n/a</dt>
        <dd>not computed: the site has too few treated subjects, queries or readings for the test.</dd>
      </dl>
    </section>
  );
}
