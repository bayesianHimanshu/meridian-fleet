import { useLayoutEffect, useRef, useState } from "react";
import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading, isStatus } from "../components/common";
import { useApi } from "../queries";

const STUDY = "MRD-201";

export const CHART_HEIGHT = 240;
export const PAD = { left: 48, right: 16, top: 16, bottom: 30 };
export const LABEL_PX = 12; // axis labels: 12 px mono
const CHAR_PX = 0.6 * LABEL_PX; // IBM Plex Mono advance width is 0.6 em
export const MIN_LABEL_SPACING = 80; // at most one x label per 80 px

export type Anchor = "start" | "middle" | "end";

export interface XTick {
  index: number;
  x: number;
  anchor: Anchor;
}

/** x positions for n points across [0, plotWidth] (plot coordinates). */
export function xAt(i: number, n: number, plotWidth: number): number {
  return n <= 1 ? 0 : (i * plotWidth) / (n - 1);
}

/** Which months get a label: at least MIN_LABEL_SPACING apart, always the first and the
 * last, the first anchored at its start and the last at its end so neither leaves the plot. */
export function xTicks(n: number, plotWidth: number, minSpacing = MIN_LABEL_SPACING): XTick[] {
  if (n === 0) return [];
  if (n === 1) return [{ index: 0, x: 0, anchor: "start" }];
  const perPoint = plotWidth / (n - 1);
  const step = Math.max(1, Math.ceil(minSpacing / perPoint));
  const lastX = xAt(n - 1, n, plotWidth);
  const ticks: XTick[] = [];
  for (let i = 0; i < n - 1; i += step) {
    const x = xAt(i, n, plotWidth);
    if (lastX - x < minSpacing) break;
    ticks.push({ index: i, x, anchor: i === 0 ? "start" : "middle" });
  }
  ticks.push({ index: n - 1, x: lastX, anchor: "end" });
  return ticks;
}

/** The horizontal extent of a label, for overlap checks. */
export function labelBox(x: number, anchor: Anchor, text: string): [number, number] {
  const w = text.length * CHAR_PX;
  if (anchor === "start") return [x, x + w];
  if (anchor === "end") return [x - w, x];
  return [x - w / 2, x + w / 2];
}

/** Y ticks in nice steps (1, 2, 2.5 or 5 times a power of ten), at most about six, capped
 * at or below the maximum: 254 gives 0, 50, ..., 250. */
export function yTicks(max: number, target = 5): number[] {
  if (max <= 0) return [0];
  const raw = max / target;
  const power = Math.pow(10, Math.floor(Math.log10(raw)));
  // The smallest nice step that gives at most target + 1 ticks.
  const step =
    [1, 2, 2.5, 5, 10].map((m) => m * power).find((s) => Math.floor(max / s) + 1 <= target + 1) ??
    10 * power;
  const ticks: number[] = [];
  for (let v = 0; v <= max + 1e-9; v += step) ticks.push(Math.round(v * 1000) / 1000);
  return ticks;
}

function useWidth(fallback: number): [React.RefObject<HTMLDivElement | null>, number] {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(fallback);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const w = el.getBoundingClientRect().width;
      if (w > 0) setWidth(Math.round(w));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => {
      observer.disconnect();
    };
  }, []);
  return [ref, width];
}

/** A cumulative line in inline SVG, drawn in pixels at the measured width, so text keeps
 * its size at any width. */
export function EnrollmentChart({ data }: { data: Schemas["Enrollment"] }) {
  const [ref, width] = useWidth(720);
  const height = CHART_HEIGHT;
  const plotW = Math.max(1, width - PAD.left - PAD.right);
  const plotH = height - PAD.top - PAD.bottom;
  const n = data.months.length;
  const last = data.total[n - 1] ?? 0;
  const top = Math.max(...data.total, 1);
  const y = (v: number) => PAD.top + plotH - (v * plotH) / top;
  const x = (i: number) => PAD.left + xAt(i, n, plotW);
  const points = data.total.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <figure className="chart" style={{ margin: 0 }}>
      <div ref={ref}>
        <svg
          width={width}
          height={height}
          role="img"
          aria-label={`Cumulative treated subjects, ${data.months[0] ?? ""} to ${data.months[n - 1] ?? ""}, reaching ${last}`}
        >
          {yTicks(top).map((t) => (
            <g key={t}>
              <line className="grid" x1={PAD.left} x2={width - PAD.right} y1={y(t)} y2={y(t)} />
              <text x={PAD.left - 8} y={y(t) + 4} textAnchor="end">
                {t}
              </text>
            </g>
          ))}
          <line className="axis" x1={PAD.left} x2={width - PAD.right} y1={y(0)} y2={y(0)} />
          {xTicks(n, plotW).map((t) => (
            <text key={t.index} x={PAD.left + t.x} y={height - 10} textAnchor={t.anchor}>
              {data.months[t.index]}
            </text>
          ))}
          <polyline className="series" points={points} />
          {n > 0 ? (
            <>
              <circle className="end-point" cx={x(n - 1)} cy={y(last)} r={3} />
              <text className="end-label" x={x(n - 1) - 6} y={y(last) - 6} textAnchor="end">
                {last}
              </text>
            </>
          ) : null}
        </svg>
      </div>
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
