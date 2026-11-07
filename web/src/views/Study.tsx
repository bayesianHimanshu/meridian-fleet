import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading, isStatus } from "../components/common";
import { useApi } from "../queries";

const STUDY = "MRD-201";

/** A cumulative line in inline SVG: months along x, treated subjects up y. */
export function EnrollmentChart({ data }: { data: Schemas["Enrollment"] }) {
  const width = 720;
  const height = 240;
  const pad = { left: 44, right: 12, top: 12, bottom: 28 };
  const n = data.months.length;
  const top = Math.max(...data.total, 1);
  const step = Math.pow(10, Math.floor(Math.log10(top)));
  const yMax = Math.ceil(top / step) * step;
  const x = (i: number) => pad.left + (n <= 1 ? 0 : (i * (width - pad.left - pad.right)) / (n - 1));
  const y = (v: number) => height - pad.bottom - (v * (height - pad.top - pad.bottom)) / yMax;
  const points = data.total.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(f * yMax));
  const labelEvery = Math.max(1, Math.ceil(n / 8));
  const last = data.total[n - 1] ?? 0;
  return (
    <figure className="chart" style={{ margin: 0 }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={`Cumulative treated subjects, ${data.months[0] ?? ""} to ${data.months[n - 1] ?? ""}, reaching ${last}`}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line className="grid" x1={pad.left} x2={width - pad.right} y1={y(t)} y2={y(t)} />
            <text x={pad.left - 6} y={y(t) + 4} textAnchor="end">
              {t}
            </text>
          </g>
        ))}
        <line className="axis" x1={pad.left} x2={width - pad.right} y1={y(0)} y2={y(0)} />
        {data.months.map((m, i) =>
          i % labelEvery === 0 || i === n - 1 ? (
            <text key={m} x={x(i)} y={height - 8} textAnchor="middle">
              {m}
            </text>
          ) : null,
        )}
        <polyline className="series" points={points} />
      </svg>
      <figcaption className="legend">Cumulative treated subjects by month of first treatment (blinded DM)</figcaption>
    </figure>
  );
}

export function StudyView() {
  const enrollment = useApi(["enrollment", STUDY], () =>
    unwrap(api.GET("/api/v1/studies/{study}/enrollment", { params: { path: { study: STUDY } } })),
  );
  return (
    <section aria-labelledby="study-title" className="stack">
      <div className="view-head">
        <div>
          <h1 id="study-title">Study MRD-201</h1>
          <p>
            Mild to moderate Alzheimer's disease, xanomeline transdermal. Data from the CDISC SDTM pilot,
            the study's current cut.
          </p>
        </div>
      </div>
      {enrollment.isPending ? <Loading what="enrollment" /> : null}
      {enrollment.isError ? (
        isStatus(enrollment.error, 403) ? (
          <p className="message">Your role does not see study enrollment.</p>
        ) : (
          <ErrorNote error={enrollment.error} />
        )
      ) : null}
      {enrollment.isSuccess ? (
        <>
          <section className="panel" aria-labelledby="enrol-title">
            <div className="panel-head">
              <h2 id="enrol-title">Enrollment</h2>
              <span className="src">
                cut <span className="mono">{enrollment.data.cut_id}</span>,{" "}
                <span className="num">{enrollment.data.total.at(-1) ?? 0}</span> treated
              </span>
            </div>
            <EnrollmentChart data={enrollment.data} />
          </section>
          <section className="panel" aria-labelledby="sites-title">
            <div className="panel-head">
              <h2 id="sites-title">Sites</h2>
              <span className="src">{enrollment.data.sites.length} sites</span>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Site</th>
                    <th scope="col" className="num">
                      Treated
                    </th>
                    <th scope="col">First treated</th>
                    <th scope="col">Last treated</th>
                  </tr>
                </thead>
                <tbody>
                  {enrollment.data.sites.map((s) => {
                    const months = enrollment.data.months;
                    const first = s.cumulative.findIndex((v) => v > 0);
                    const total = s.cumulative.at(-1) ?? 0;
                    const lastIndex = s.cumulative.findIndex((v) => v === total);
                    return (
                      <tr key={s.site_id}>
                        <th scope="row" className="mono">
                          {s.site_id}
                        </th>
                        <td className="num">{total}</td>
                        <td className="mono">{months[first] ?? "n/a"}</td>
                        <td className="mono">{months[lastIndex] ?? "n/a"}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </>
      ) : null}
    </section>
  );
}
