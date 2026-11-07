import { useState } from "react";
import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading, isStatus } from "../components/common";
import { useApi } from "../queries";

type Site = Schemas["SiteMetrics"];
type Level = "l0" | "l1" | "l2" | "na";

const STUDY = "MRD-201";

/** A between-site z: under 2 in size is ordinary, 2 to 3 worth a look, 3 or more strong. */
export function zLevel(z: number | null | undefined): Level {
  if (z === null || z === undefined) return "na";
  const size = Math.abs(z);
  return size < 2 ? "l0" : size < 3 ? "l1" : "l2";
}

/** A value against its threshold: below three quarters ordinary, then near, then over. */
export function ratioLevel(value: number | null | undefined, threshold: number | undefined): Level {
  if (value === null || value === undefined || !threshold) return "na";
  const ratio = value / threshold;
  return ratio < 0.75 ? "l0" : ratio < 1 ? "l1" : "l2";
}

/** Vital-sign spread: only unusually small spread (negative z) is a concern. */
export function sdLevel(z: number | null | undefined, threshold: number | undefined): Level {
  if (z === null || z === undefined) return "na";
  const limit = threshold ?? -3.5;
  return z > -2 ? "l0" : z > limit ? "l1" : "l2";
}

const LEVEL_TEXT: Record<Level, string> = {
  l0: "ordinary",
  l1: "worth a look",
  l2: "strong signal",
  na: "not tested",
};

function Cell({ text, level, flagged }: { text: string; level: Level; flagged: boolean }) {
  return (
    <td className={`cell ${level}${flagged ? " flagged" : ""}`}>
      {text}
      <span className="visually-hidden">
        , {LEVEL_TEXT[level]}
        {flagged ? ", flagged by an agent" : ""}
      </span>
    </td>
  );
}

function fmt(v: number | null | undefined, digits: number): string {
  return v === null || v === undefined ? "n/a" : v.toFixed(digits);
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
  const flagged = (s: Site, check: string) => s.flagged_checks.includes(check);
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
              <th scope="col" className="num">
                Treated
              </th>
              <th scope="col" className="num">
                AE rate z
              </th>
              <th scope="col" className="num">
                Deviation z
              </th>
              <th scope="col" className="num">
                Entry lag, days
              </th>
              <th scope="col" className="num">
                Open query age, days
              </th>
              {data.parameters.map((p) => (
                <th scope="col" className="num" key={p}>
                  {p} spread z
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.sites.map((s) => (
              <tr key={s.site_id}>
                <th scope="row" className="mono">
                  {s.site_id}
                  {s.tested ? null : <span className="muted"> (too small to test)</span>}
                </th>
                <td className="num">{s.treated}</td>
                <Cell text={fmt(s.ae_rate_z, 2)} level={zLevel(s.ae_rate_z)} flagged={flagged(s, "ae_rate")} />
                <Cell text={fmt(s.deviation_z, 2)} level={zLevel(s.deviation_z)} flagged={flagged(s, "deviations")} />
                <Cell
                  text={fmt(s.median_entry_lag_days, 1)}
                  level={ratioLevel(s.median_entry_lag_days, t.data_entry_lag_days)}
                  flagged={flagged(s, "data_entry_lag")}
                />
                <Cell
                  text={fmt(s.median_open_query_age_days, 1)}
                  level={ratioLevel(s.median_open_query_age_days, t.open_query_age_days)}
                  flagged={flagged(s, "query_aging")}
                />
                {data.parameters.map((p) => (
                  <Cell
                    key={p}
                    text={fmt(s.csm_log_sd_z[p], 2)}
                    level={sdLevel(s.csm_log_sd_z[p], t.csm_log_sd_z)}
                    flagged={flagged(s, "variance")}
                  />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="legend">
        <span>
          <i className="swatch l0" aria-hidden="true" />
          Ordinary
        </span>
        <span>
          <i className="swatch l1" aria-hidden="true" />
          Worth a look (z 2 to 3, or over three quarters of a threshold)
        </span>
        <span>
          <i className="swatch l2" aria-hidden="true" />
          Strong signal (z 3 or more, or over the threshold)
        </span>
        <span>
          <i className="swatch flagged" aria-hidden="true" />
          Flagged by an agent
        </span>
        <span>
          Thresholds: entry lag {t.data_entry_lag_days} days, query age {t.open_query_age_days} days, spread z{" "}
          {t.csm_log_sd_z}
        </span>
      </div>
    </section>
  );
}
