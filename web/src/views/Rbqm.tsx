import { useLayoutEffect, useRef, useState } from "react";
import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading, isStatus } from "../components/common";
import { FindingLink } from "../components/text";
import { useApi } from "../queries";

type Site = Schemas["SiteMetrics"];
type Flag = Schemas["SiteFlag"];
export type Band = Schemas["SiteMetrics"]["bands"][string];
type Rule = Schemas["ColumnRule"];

const STUDY = "MRD-201";

const BAND_TEXT: Record<Band, string> = {
  normal: "normal",
  warning: "warning",
  critical: "critical",
  untested_direction: "a direction the agent does not test",
  too_few: "too few to test",
  na: "not computed",
};

/** True while the element has columns hidden past its right edge. */
export function hasMoreRight(el: { scrollLeft: number; clientWidth: number; scrollWidth: number }): boolean {
  return el.scrollLeft + el.clientWidth < el.scrollWidth - 1;
}

function useMoreRight(): React.RefObject<HTMLDivElement | null> {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => {
      el.dataset.moreRight = String(hasMoreRight(el));
    };
    update();
    el.addEventListener("scroll", update, { passive: true });
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(update);
    observer?.observe(el);
    return () => {
      el.removeEventListener("scroll", update);
      observer?.disconnect();
    };
  }, []);
  return ref;
}

function flagText(flags: Flag[]): string {
  return flags.map((f) => `${f.finding_id}, ${f.agent_id} ${f.agent_version} (${f.status})`).join("; ");
}

function Cell({
  text,
  band,
  flags,
  reason,
}: {
  text: string;
  band: Band;
  flags: Flag[];
  reason?: string | undefined;
}) {
  const flagged = flags.length > 0;
  const why = band === "too_few" ? reason : undefined;
  const title = [why, flagged ? `Flagged by ${flagText(flags)}` : undefined].filter(Boolean).join(". ");
  return (
    <td
      className={`cell ${band}${flagged ? " flagged" : ""}`}
      title={title || undefined}
      data-flagged={flagged ? flags.map((f) => f.finding_id).join(" ") : undefined}
    >
      {text}
      <span className="visually-hidden">
        , {why ?? BAND_TEXT[band]}
        {flagged ? `, flagged by ${flagText(flags)}` : ""}
      </span>
    </td>
  );
}

/** Why a cell is too_few: the count, and the minimum its agent needs. */
export function tooFewReason(rule: Rule | undefined, count: number | null | undefined): string | undefined {
  if (!rule?.min_count) return undefined;
  const what = rule.count_label ?? "items";
  return `too few ${what} (${count ?? 0}; ${rule.agent} needs at least ${rule.min_count})`;
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
        <span className="fig">{qtl.rate === null ? "n/a" : `${(100 * rate).toFixed(1)}%`}</span> discontinued
        against a limit of <span className="fig">{(100 * qtl.limit).toFixed(1)}%</span>; approaching from{" "}
        <span className="fig">{(100 * qtl.approach_from).toFixed(1)}%</span>. Status:{" "}
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

function num(v: number, unit: Rule["unit"]): string {
  const text = Math.abs(v) >= 10 || Number.isInteger(v) ? v.toFixed(unit === "z" ? 2 : 1) : v.toFixed(2);
  return text.replace(/\.?0+$/, "") || "0";
}

/** The bands of one column in words, with their numbers, from the agent's rule. */
export function bandsText(rule: Rule): string {
  if (rule.unit === "days") {
    return `warning above ${num(rule.warning_from, "days")} days, critical above ${num(rule.critical_beyond, "days")} days`;
  }
  const side = rule.direction === "low" ? "below" : rule.direction === "high" ? "above" : "beyond";
  const sign = rule.direction === "low" ? "-" : rule.direction === "high" ? "+" : "\u00b1";
  const limit = num(Math.abs(rule.critical_beyond), "z");
  const untested =
    rule.direction === "both" ? "" : `; ${rule.direction === "low" ? "positive" : "negative"} z: no fill (not tested)`;
  return `warning from z ${sign}2 to ${sign}${limit}, critical ${side} ${sign}${limit}${untested}`;
}

function Heatmap({ data }: { data: Schemas["RbqmSites"] }) {
  const flags = data.sites.flatMap((s) => s.flagged_by.map((f) => ({ site: s.site_id, ...f })));
  const col = (key: string) => data.columns[key];
  const groups: { label: string; keys: string[] }[] = [
    { label: "AE rate z", keys: ["ae_rate_z"] },
    { label: "Deviation z", keys: ["deviation_z"] },
    { label: "Entry lag", keys: ["median_entry_lag_days"] },
    { label: "Open query age", keys: ["median_open_query_age_days"] },
    { label: "Spread z (SYSBP, DIABP, PULSE)", keys: data.parameters.map((p) => `csm_log_sd_z:${p}`) },
  ];
  const moreRight = useMoreRight();
  const cell = (s: Site, key: string, text: string, check: string, parameter?: string) => (
    <Cell
      key={key}
      text={text}
      band={s.bands[key] ?? "na"}
      flags={flagsFor(s, check, parameter)}
      reason={key === "median_open_query_age_days" ? tooFewReason(col(key), s.open_queries) : undefined}
    />
  );
  return (
    <section className="panel" aria-labelledby="heat-title">
      <div className="panel-head">
        <h2 id="heat-title">Site heatmap</h2>
        <span className="src mono">{data.cut_id}</span>
      </div>
      <div className="table-wrap heat-wrap" ref={moreRight}>
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
                {cell(s, "ae_rate_z", fmt(s.ae_rate_z, 2), "ae_rate")}
                {cell(s, "deviation_z", fmt(s.deviation_z, 2), "deviations")}
                {cell(s, "median_entry_lag_days", fmt(s.median_entry_lag_days, 1), "data_entry_lag")}
                {cell(s, "median_open_query_age_days", fmt(s.median_open_query_age_days, 1), "query_aging")}
                {data.parameters.map((p) => cell(s, `csm_log_sd_z:${p}`, fmt(s.csm_log_sd_z[p], 2), "variance", p))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <dl className="legend-list" aria-label="How to read the heatmap">
        <dt>Fill</dt>
        <dd>
          <i className="swatch warning" aria-hidden="true" />
          warning, <i className="swatch critical" aria-hidden="true" />
          critical, no fill otherwise. Each column follows what its agent tests: the direction and
          the agent's own threshold, so critical means the agent's threshold is passed.
        </dd>
        {groups.map((g) => {
          const rule = g.keys[0] ? col(g.keys[0]) : undefined;
          return rule ? (
            <div key={g.label} style={{ display: "contents" }}>
              <dt>{g.label}</dt>
              <dd>
                {bandsText(rule)}. <span className="muted">{rule.agent}: {rule.rule}.</span>
              </dd>
            </div>
          ) : null;
        })}
        <dt>
          <i className="swatch flagged" aria-hidden="true" />
          Outline
        </dt>
        <dd>
          a current finding (newest agent version, not superseded or retired, in any status) covers
          this cell:{" "}
          {flags.length === 0
            ? "none on this cut."
            : flags.map((f, i) => (
                <span key={`${f.finding_id}-${f.site}`}>
                  {i > 0 ? "; " : ""}
                  <FindingLink id={f.finding_id} className="mono" /> at site {f.site} by {f.agent_id}{" "}
                  {f.agent_version}
                  {i === flags.length - 1 ? "." : ""}
                </span>
              ))}
        </dd>
        <dt>Muted value</dt>
        <dd>
          shown but not tested: too few items for the agent (open query age needs at least{" "}
          {col("median_open_query_age_days")?.min_count ?? 5} open queries), or a direction the
          agent does not test.
        </dd>
        <dt>n/a</dt>
        <dd>not computed: the site has too few treated subjects, queries or readings for the test.</dd>
      </dl>
    </section>
  );
}
