import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type SyntheticEvent } from "react";
import { api, unwrap, type Schemas } from "../api/client";
import { ErrorNote, Loading, SeverityChip, StatusChip, Time, isStatus } from "../components/common";
import { RaiseObservation } from "../components/Observation";
import { FindingLink } from "../components/text";
import { useIdentity } from "../identity";
import { useApi } from "../queries";

type Study = Schemas["PreclinicalStudy"];
type Table = Schemas["EndpointTableView"];
type Group = Schemas["EndpointGroupView"];
type Decision = Schemas["NoaelDecisionView"];
type Sex = "M" | "F";
type Phase = "main" | "recovery";

const AGENT = "preclinical.tox_study_monitor";
const SEX_TEXT: Record<string, string> = { M: "Males", F: "Females", combined: "Both sexes" };
const PHASE_TEXT: Record<Phase, string> = { main: "Main study", recovery: "Recovery groups" };
const GRADE_TEXT: Record<number, string> = { 1: "minimal", 2: "mild", 3: "moderate", 4: "marked", 5: "severe" };
const CHECK_TEXT: Record<string, string> = {
  dose_related_change: "Dose-related change",
  pairwise_change: "Change without a trend",
  dose_related_incidence: "Dose-related incidence",
  pairwise_incidence: "Higher incidence without a trend",
  recovery_only_change: "Change only at recovery",
  recovery_only_incidence: "Incidence only at recovery",
  organ_weight_secondary_to_bw: "May be secondary to lower body weight",
  neoplasm: "Neoplasm in a treated group",
};

function num(v: number | null | undefined): string {
  if (v === null || v === undefined) return "-";
  return Math.abs(v) >= 100 ? v.toFixed(0) : v.toPrecision(3);
}

function pctText(v: number | null | undefined): string {
  if (v === null || v === undefined) return "";
  return `${v > 0 ? "+" : ""}${v.toFixed(0)}%`;
}

function pText(p: number | null | undefined): string {
  if (p === null || p === undefined) return "not tested";
  return p < 0.001 ? "p < 0.001" : `p = ${p.toFixed(3)}`;
}

function doseText(dose: number, unit: string): string {
  return dose === 0 ? "Control" : `${String(dose)} ${unit}`;
}

export function PreclinicalView() {
  const studies = useApi(["preclinical", "studies"], () => unwrap(api.GET("/api/v1/preclinical/studies")));
  return (
    <section aria-labelledby="preclinical-title" className="stack">
      <div className="view-head">
        <div>
          <h1 id="preclinical-title">Preclinical</h1>
          <p>
            MRD-101's GLP rat study. Every dose group is compared with the concurrent control of the same sex.
            The monitor says what changed with dose; whether a change is adverse, and the NOAEL, are the
            toxicologist's decisions.
          </p>
        </div>
      </div>
      {studies.isPending ? <Loading what="the preclinical studies" /> : null}
      {studies.isError ? (
        isStatus(studies.error, 403) ? (
          <p className="message">
            Your role does not see preclinical studies. They are shown to the toxicologist, the portfolio lead
            and the QA auditor.
          </p>
        ) : (
          <ErrorNote error={studies.error} />
        )
      ) : null}
      {studies.isSuccess && studies.data.length === 0 ? (
        <p className="message">No preclinical study has been loaded.</p>
      ) : null}
      {studies.isSuccess
        ? studies.data.map((s) => (
            <div key={s.study_id} className="stack">
              <Design study={s} />
              <Endpoints study={s} />
              <Findings study={s} />
              <NoaelPanel study={s} />
              <DosePanel study={s} />
              <TranslationalPanel study={s} />
              {s.asset_id ? <IndChecklist assetId={s.asset_id} /> : null}
            </div>
          ))
        : null}
    </section>
  );
}

function Design({ study }: { study: Study }) {
  return (
    <section className="panel" aria-labelledby="design-title">
      <div className="panel-head">
        <h2 id="design-title">Study design</h2>
        <span className="src mono">{study.study_id}</span>
      </div>
      <p>{study.title}</p>
      <p className="muted">
        {study.species.toLowerCase()} ({study.strain.toLowerCase()}), {study.route.toLowerCase()}, dosing{" "}
        {study.duration}
      </p>
      <div className="table-wrap" tabIndex={0} aria-label="Dose groups table">
        <table>
          <caption className="visually-hidden">Dose groups by phase, with the animals of each sex</caption>
          <thead>
            <tr>
              <th scope="col">Phase</th>
              <th scope="col">Group</th>
              <th scope="col">Dose</th>
              <th scope="col" className="num">
                Males
              </th>
              <th scope="col" className="num">
                Females
              </th>
            </tr>
          </thead>
          <tbody>
            {study.groups.map((g) => (
              <tr key={g.setcd}>
                <td>{PHASE_TEXT[g.phase]}</td>
                <th scope="row" className="mono">
                  {g.setcd}
                </th>
                <td>{doseText(g.dose, study.dose_unit)}</td>
                <td className="num">{g.males}</td>
                <td className="num">{g.females}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Cell({ table, group, unit }: { table: Table; group: Group; unit: string }) {
  const label = doseText(group.dose, unit);
  if (table.kind === "incidence") {
    const grades = (group.grades ?? []).map((g) => `${GRADE_TEXT[g.grade] ?? String(g.grade)} ${String(g.animals)}`);
    return (
      <td className={group.flagged ? "num flagged" : "num"} title={`${label}: ${pText(group.p)}`}>
        <span className="fig">
          {group.affected ?? 0}/{group.n}
        </span>
        {grades.length ? <div className="muted">{grades.join(", ")}</div> : null}
        {group.flagged ? <span className="visually-hidden"> (flagged)</span> : null}
      </td>
    );
  }
  return (
    <td className={group.flagged ? "num flagged" : "num"} title={`${label}: n = ${String(group.n)}, ${pText(group.p)}`}>
      <span className="fig">{num(group.mean)}</span>
      {group.rank > 0 ? <div className="muted">{pctText(group.percent_vs_control)}</div> : null}
      {group.flagged ? <span className="visually-hidden"> (flagged)</span> : null}
    </td>
  );
}

function Endpoints({ study }: { study: Study }) {
  const ids = useId();
  const [sex, setSex] = useState<Sex>("M");
  const [phase, setPhase] = useState<Phase>("main");
  const [flaggedOnly, setFlaggedOnly] = useState(true);
  const endpoints = useApi(["preclinical", study.study_id, "endpoints"], () =>
    unwrap(api.GET("/api/v1/preclinical/{study}/endpoints", { params: { path: { study: study.study_id } } })),
  );
  const doses = study.groups.filter((g) => g.phase === phase).map((g) => g.dose);
  return (
    <section className="panel" aria-labelledby="endpoints-title">
      <div className="panel-head">
        <h2 id="endpoints-title">Endpoints by sex and dose</h2>
        <span className="src">against the concurrent control</span>
      </div>
      <div className="toolbar">
        <label className="field" htmlFor={`${ids}-sex`}>
          Sex
          <select
            id={`${ids}-sex`}
            value={sex}
            onChange={(e) => {
              setSex(e.target.value as Sex);
            }}
          >
            <option value="M">Males</option>
            <option value="F">Females</option>
          </select>
        </label>
        <label className="field" htmlFor={`${ids}-phase`}>
          Phase
          <select
            id={`${ids}-phase`}
            value={phase}
            onChange={(e) => {
              setPhase(e.target.value as Phase);
            }}
          >
            <option value="main">Main study</option>
            <option value="recovery">Recovery groups</option>
          </select>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={flaggedOnly}
            onChange={(e) => {
              setFlaggedOnly(e.target.checked);
            }}
          />{" "}
          Only endpoints with a flagged group
        </label>
      </div>
      {endpoints.isPending ? <Loading what="the endpoint tables" /> : null}
      {endpoints.isError ? <ErrorNote error={endpoints.error} /> : null}
      {endpoints.isSuccess ? (
        <>
          <div className="table-wrap heat-wrap" tabIndex={0} aria-label="Endpoint table, scrolls sideways">
            <table className="heat">
              <caption className="visually-hidden">
                {SEX_TEXT[sex]}, {PHASE_TEXT[phase].toLowerCase()}: group mean or incidence per dose; flagged
                groups differ from control by the pre-registered rule
              </caption>
              <thead>
                <tr>
                  <th scope="col">Endpoint</th>
                  {doses.map((d) => (
                    <th scope="col" key={d} className="num">
                      {doseText(d, endpoints.data.dose_unit)}
                    </th>
                  ))}
                  <th scope="col">Trend</th>
                  <th scope="col">Finding</th>
                </tr>
              </thead>
              <tbody>
                {endpoints.data.tables
                  .filter((t) => t.sex === sex && t.phase === phase)
                  .filter((t) => !flaggedOnly || t.check !== null || t.groups.some((g) => g.flagged))
                  .map((t) => (
                    <tr key={t.endpoint}>
                      <th scope="row">
                        {t.label}
                        {t.unit ? <span className="muted"> ({t.unit})</span> : null}
                      </th>
                      {doses.map((d) => {
                        const g = t.groups.find((x) => x.dose === d);
                        return g ? (
                          <Cell key={d} table={t} group={g} unit={endpoints.data.dose_unit} />
                        ) : (
                          <td key={d} className="na">
                            -
                          </td>
                        );
                      })}
                      <td>{pText(t.trend_p)}</td>
                      <td>
                        {t.finding_id ? (
                          <>
                            <FindingLink id={t.finding_id} className="mono" />
                            <div className="muted">{CHECK_TEXT[t.check ?? ""] ?? ""}</div>
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
          <DoseSupport data={endpoints.data} phase={phase} />
        </>
      ) : null}
    </section>
  );
}

function DoseSupport({ data, phase }: { data: Schemas["PreclinicalEndpoints"]; phase: Phase }) {
  const rows = data.by_dose.filter((d) => d.phase === phase);
  return (
    <div className="stack" aria-label="Findings per dose group">
      <h3>What flags each dose group ({PHASE_TEXT[phase].toLowerCase()})</h3>
      <p className="muted">
        Decision support for the NOAEL: the findings that flag each dose group. It is not a NOAEL.
      </p>
      {rows.length === 0 ? <p className="muted">No dose group is flagged.</p> : null}
      <dl className="kv">
        {rows.map((d) => (
          <div key={d.dose}>
            <dt>{doseText(d.dose, data.dose_unit)}</dt>
            <dd>
              {d.dose_related.length ? (
                <>
                  {String(d.dose_related.length)} dose-related:{" "}
                  {d.dose_related.map((id, i) => (
                    <span key={id}>
                      {i ? ", " : ""}
                      <FindingLink id={id} className="mono" />
                    </span>
                  ))}
                </>
              ) : null}
              {d.without_trend.length ? (
                <span className="muted">
                  {" "}
                  ({String(d.without_trend.length)} without a trend)
                </span>
              ) : null}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function usePreclinicalFindings(study: Study) {
  return useApi(["preclinical", study.study_id, "findings"], () =>
    unwrap(api.GET("/api/v1/findings", { params: { query: { agent: AGENT, limit: 500 } } })),
  );
}

function Findings({ study }: { study: Study }) {
  const findings = usePreclinicalFindings(study);
  const items = findings.data?.items.filter((f) => f.study_id === study.study_id) ?? [];
  return (
    <section className="panel" aria-labelledby="pre-findings-title">
      <div className="panel-head">
        <h2 id="pre-findings-title">Findings</h2>
        <span className="src">{findings.isSuccess ? `${String(items.length)} from the monitor` : ""}</span>
      </div>
      {findings.isPending ? <Loading what="the findings" /> : null}
      {findings.isError ? <ErrorNote error={findings.error} /> : null}
      <ul className="findings">
        {items.map((f) => (
          <li key={f.finding_id} className="finding">
            <div className="finding-meta">
              <FindingLink id={f.finding_id} className="mono" />
              <SeverityChip severity={f.severity} />
              <StatusChip status={f.status} />
            </div>
            <p className="headline">{f.title}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}

function DecisionText({ d }: { d: Decision }) {
  return (
    <>
      <p>
        <strong>{SEX_TEXT[d.sex]}:</strong>{" "}
        {d.established ? `NOAEL ${String(d.noael_dose)} ${d.dose_unit}` : "NOAEL not established"}
        {d.loael_dose !== null ? `, LOAEL ${String(d.loael_dose)} ${d.dose_unit}` : ""}
      </p>
      <p>{d.rationale}</p>
      <p className="muted">
        Decided by {d.decided_by}, <Time iso={d.recorded_at} />
        {d.supersedes ? (
          <>
            ; revises <span className="mono">{d.supersedes}</span>
          </>
        ) : null}
        {d.finding_ids.length ? (
          <>
            ; rests on{" "}
            {d.finding_ids.map((id, i) => (
              <span key={id}>
                {i ? ", " : ""}
                <FindingLink id={id} className="mono" />
              </span>
            ))}
          </>
        ) : null}
      </p>
    </>
  );
}

function NoaelPanel({ study }: { study: Study }) {
  const noael = useApi(["preclinical", study.study_id, "noael"], () =>
    unwrap(api.GET("/api/v1/preclinical/{study}/noael", { params: { path: { study: study.study_id } } })),
  );
  return (
    <section className="panel" aria-labelledby="noael-title">
      <div className="panel-head">
        <h2 id="noael-title">NOAEL</h2>
        <span className="src">the toxicologist's decision</span>
      </div>
      {noael.isPending ? <Loading what="the NOAEL decisions" /> : null}
      {noael.isError ? <ErrorNote error={noael.error} /> : null}
      {noael.isSuccess ? (
        <>
          {noael.data.current.length === 0 ? (
            <p className="message" data-testid="noael-none">
              Not yet decided.
            </p>
          ) : (
            <ul className="stack" aria-label="Current decisions">
              {noael.data.current.map((d) => (
                <li key={d.decision_id} className="finding" data-testid="noael-current">
                  <DecisionText d={d} />
                  <RaiseObservation refersTo={{ ledger_seq: d.seq }} decidedBy="The portfolio lead" />
                </li>
              ))}
            </ul>
          )}
          {noael.data.history.some((d) => !d.current) ? (
            <details>
              <summary>Earlier decisions ({String(noael.data.history.filter((d) => !d.current).length)})</summary>
              <ul className="stack">
                {noael.data.history
                  .filter((d) => !d.current)
                  .map((d) => (
                    <li key={d.decision_id} className="finding">
                      <DecisionText d={d} />
                      <p className="muted">
                        Superseded by <span className="mono">{d.superseded_by}</span>
                      </p>
                    </li>
                  ))}
              </ul>
            </details>
          ) : null}
          {noael.data.may_record ? <NoaelForm study={study} history={noael.data} /> : null}
        </>
      ) : null}
    </section>
  );
}

type Candidate = Schemas["NoaelCandidate"];
type DecisionSex = "M" | "F" | "combined";
const DECISION_SEX: Record<DecisionSex, string> = { M: "Males", F: "Females", combined: "Both sexes" };

/** The first lines of a rationale, as the summary shows them. */
export function firstLines(text: string, lines = 3, chars = 300): string {
  const kept = text.trim().split(/\r?\n/).filter((l) => l.trim() !== "").slice(0, lines).join(" ");
  return kept.length > chars ? `${kept.slice(0, chars).trimEnd()}...` : kept;
}

/** The form's rules, the same as the API's (the API checks them again). */
export function noaelProblem(args: {
  sex: DecisionSex;
  established: boolean;
  noael: number | null;
  loael: number | null;
  treated: number[];
  chosen: Candidate[];
  rationale: string;
  minChars: number;
}): string | null {
  const { sex, established, noael, loael, treated, chosen, rationale, minChars } = args;
  const lowest = Math.min(...treated);
  if (!established && loael === null) {
    return "A NOAEL that is not established needs the LOAEL: the lowest dose with an adverse finding.";
  }
  if (!established && loael !== lowest) {
    return `Not established means an adverse finding at the lowest dose, so the LOAEL is ${String(lowest)}.`;
  }
  if (established && noael !== null && loael !== null && loael <= noael) {
    return "The LOAEL must be above the NOAEL.";
  }
  if (loael !== null && chosen.length === 0) {
    return "A LOAEL rests on at least one finding: choose the findings.";
  }
  const other = chosen.find((c) => sex !== "combined" && c.sex !== sex);
  if (other) {
    return `${other.finding_id} is not a finding about ${DECISION_SEX[sex].toLowerCase()}: a ${sex} decision rests only on that sex's findings.`;
  }
  if (rationale.trim().length < minChars) {
    return `Give a rationale of at least ${String(minChars)} characters (now ${String(rationale.trim().length)}): what is adverse at the LOAEL and why the changes at the NOAEL are not.`;
  }
  return null;
}

interface CandidateGroup {
  key: string;
  label: string;
  items: Candidate[];
}

/** Findings grouped by sex, then by the lowest main-study dose where they flag a group. */
export function groupCandidates(items: Candidate[], sex: DecisionSex, unit: string): CandidateGroup[] {
  const order: (Candidate["sex"])[] = sex === "combined" ? ["M", "F", null] : [sex];
  const groups: CandidateGroup[] = [];
  for (const s of order) {
    const mine = items.filter((c) => c.sex === s);
    const byDose = new Map<string, Candidate[]>();
    for (const c of mine) {
      const at =
        c.main_doses.length > 0
          ? `from ${String(Math.min(...c.main_doses))} ${unit}`
          : c.recovery_doses.length > 0
            ? "at recovery only"
            : "no dose group flagged";
      byDose.set(at, [...(byDose.get(at) ?? []), c]);
    }
    const keys = [...byDose.keys()].sort((a, b) => {
      const da = Number(/from ([\d.]+)/.exec(a)?.[1] ?? Infinity);
      const db = Number(/from ([\d.]+)/.exec(b)?.[1] ?? Infinity);
      return da - db || a.localeCompare(b);
    });
    for (const k of keys) {
      const who = s === null ? "No sex (data quality)" : DECISION_SEX[s];
      groups.push({ key: `${String(s)}-${k}`, label: `${who}, ${k}`, items: byDose.get(k) ?? [] });
    }
  }
  return groups;
}

function NoaelForm({ study, history }: { study: Study; history: Schemas["NoaelHistory"] }) {
  const ids = useId();
  const queryClient = useQueryClient();
  const { person } = useIdentity();
  const treated = history.doses.filter((d) => d > 0);
  const candidates = history.candidates ?? [];
  const minChars = history.rationale_min_chars;
  const [sex, setSex] = useState<DecisionSex>("combined");
  const [established, setEstablished] = useState(true);
  const [noaelDose, setNoaelDose] = useState<string>(treated[0] === undefined ? "" : String(treated[0]));
  const [loaelDose, setLoaelDose] = useState<string>("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [rationale, setRationale] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [reviewing, setReviewing] = useState(false);
  const current = history.current.find((d) => d.sex === sex);
  const picked = candidates.filter((c) => chosen.includes(c.finding_id));
  const loael = loaelDose === "" ? null : Number(loaelDose);
  const noael = established ? Number(noaelDose) : null;
  const record = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/preclinical/{study}/noael", {
          params: { path: { study: study.study_id } },
          body: {
            sex,
            established,
            noael_dose: noael,
            loael_dose: loael,
            finding_ids: chosen,
            rationale: rationale.trim(),
            supersedes: current?.decision_id ?? null,
          },
        }),
      ),
    onSuccess: () => {
      setReviewing(false);
      void queryClient.invalidateQueries({ queryKey: [person.actor] });
    },
  });
  const review = (e: SyntheticEvent) => {
    e.preventDefault();
    const problem = noaelProblem({ sex, established, noael, loael, treated, chosen: picked, rationale, minChars });
    setLocalError(problem);
    if (problem === null) setReviewing(true);
  };
  const changeSex = (next: DecisionSex) => {
    setSex(next);
    if (next !== "combined") {
      setChosen(chosen.filter((id) => candidates.find((c) => c.finding_id === id)?.sex === next));
    }
  };
  const groups = groupCandidates(candidates, sex, history.dose_unit);
  const doseText = (d: number | null) => (d === null ? "none" : `${String(d)} ${history.dose_unit}`);
  return (
    <form className="decision" onSubmit={review} aria-label="Record a NOAEL decision">
      <fieldset disabled={reviewing}>
        <legend>{current ? "Revise the NOAEL" : "Record the NOAEL"}</legend>
        {current ? (
          <p className="muted">
            This revises <span className="mono">{current.decision_id}</span>; both stay in the ledger.
          </p>
        ) : null}
        <p className="muted">
          One decision per sex (or both sexes), resting on every finding it cites. A finding you judge incidental
          is dismissed on its own page, with its reason; this form records only the NOAEL.
        </p>
        <label className="field" htmlFor={`${ids}-sex`}>
          Sex
          <select
            id={`${ids}-sex`}
            value={sex}
            onChange={(e) => {
              changeSex(e.target.value as DecisionSex);
            }}
          >
            <option value="combined">Both sexes</option>
            <option value="M">Males</option>
            <option value="F">Females</option>
          </select>
        </label>
        <label className="check">
          <input
            type="checkbox"
            checked={!established}
            onChange={(e) => {
              setEstablished(!e.target.checked);
            }}
          />{" "}
          NOAEL not established (adverse effects at every dose)
        </label>
        {established ? (
          <label className="field" htmlFor={`${ids}-noael`}>
            NOAEL
            <select
              id={`${ids}-noael`}
              value={noaelDose}
              onChange={(e) => {
                setNoaelDose(e.target.value);
              }}
            >
              {treated.map((d) => (
                <option key={d} value={String(d)}>
                  {String(d)} {history.dose_unit}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <label className="field" htmlFor={`${ids}-loael`}>
          LOAEL
          <select
            id={`${ids}-loael`}
            value={loaelDose}
            onChange={(e) => {
              setLoaelDose(e.target.value);
            }}
          >
            <option value="">None</option>
            {treated.map((d) => (
              <option key={d} value={String(d)}>
                {String(d)} {history.dose_unit}
              </option>
            ))}
          </select>
        </label>
        <fieldset aria-label="The findings it rests on">
          <legend>
            The findings it rests on ({String(chosen.length)} chosen
            {sex === "combined" ? "" : `; ${DECISION_SEX[sex].toLowerCase()} only`})
          </legend>
          <div className="stack" style={{ maxHeight: "20rem", overflowY: "auto" }}>
            {groups.map((g) => (
              <fieldset key={g.key} aria-label={g.label}>
                <legend>
                  {g.label} ({String(g.items.length)})
                </legend>
                {g.items.map((f) => (
                  <div key={f.finding_id}>
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={chosen.includes(f.finding_id)}
                        onChange={(e) => {
                          setChosen(
                            e.target.checked ? [...chosen, f.finding_id] : chosen.filter((x) => x !== f.finding_id),
                          );
                        }}
                      />{" "}
                      <span className="mono">{f.finding_id}</span> {f.title}
                    </label>{" "}
                    <FindingLink id={f.finding_id} className="muted">
                      open <span className="visually-hidden">{f.finding_id}</span>
                    </FindingLink>
                  </div>
                ))}
              </fieldset>
            ))}
          </div>
        </fieldset>
        <label className="field" htmlFor={`${ids}-rationale`}>
          Rationale (required, at least {String(minChars)} characters)
          <textarea
            id={`${ids}-rationale`}
            className="reason"
            rows={6}
            value={rationale}
            onChange={(e) => {
              setRationale(e.target.value);
            }}
          />
        </label>
        <div aria-live="polite">
          {localError ? (
            <p className="message error" role="alert">
              {localError}
            </p>
          ) : null}
        </div>
        {!reviewing ? (
          <button type="submit" className="btn">
            Review the decision
          </button>
        ) : null}
      </fieldset>
      {reviewing ? (
        <section className="panel" aria-label="Summary before recording" data-testid="noael-summary">
          <h3>Before you record</h3>
          <dl className="kv">
            <div>
              <dt>Sex</dt>
              <dd>{DECISION_SEX[sex]}</dd>
            </div>
            <div>
              <dt>NOAEL</dt>
              <dd>{established ? doseText(noael) : "not established"}</dd>
            </div>
            <div>
              <dt>LOAEL</dt>
              <dd>{doseText(loael)}</dd>
            </div>
            <div>
              <dt>Findings ({String(picked.length)})</dt>
              <dd>
                <ul>
                  {picked.map((f) => (
                    <li key={f.finding_id}>
                      <span className="mono">{f.finding_id}</span> {f.title}
                    </li>
                  ))}
                </ul>
              </dd>
            </div>
            <div>
              <dt>Rationale</dt>
              <dd>{firstLines(rationale)}</dd>
            </div>
          </dl>
          {current ? (
            <p className="message" data-testid="noael-replaces">
              This replaces decision <span className="mono">{current.decision_id}</span> recorded at{" "}
              <Time iso={current.recorded_at} />.
            </p>
          ) : (
            <p className="muted">This is the first {DECISION_SEX[sex].toLowerCase()} decision for the study.</p>
          )}
          <div className="toolbar">
            <button
              type="button"
              className="btn"
              disabled={record.isPending}
              onClick={() => {
                record.mutate();
              }}
            >
              Confirm and record
            </button>
            <button
              type="button"
              className="btn outline"
              disabled={record.isPending}
              onClick={() => {
                setReviewing(false);
              }}
            >
              Back to edit
            </button>
          </div>
          {record.isError ? <ErrorNote error={record.error} /> : null}
        </section>
      ) : null}
      {record.isSuccess ? (
        <p className="message ok" role="status" data-testid="noael-recorded">
          Recorded as <span className="mono">{record.data.decision_id}</span> (ledger seq{" "}
          <span className="mono">{record.data.seq}</span>).
        </p>
      ) : null}
    </form>
  );
}

const STEP_LABEL: Record<string, string> = {
  "mrsd:noael_mg_per_kg": "NOAEL",
  "mrsd:noael_not_established": "NOAEL",
  "mrsd:hed_mg_per_kg": "Human equivalent dose",
  "mrsd:hed_cross_check_mg_per_kg": "Cross-check",
  "mrsd:most_appropriate_species": "Most appropriate species",
  "mrsd:mrsd_mg_per_kg": "Maximum recommended starting dose",
  "mrsd:starting_dose_mg": "Starting dose, 60 kg adult",
  "mrsd:decision_considered": "Decision considered",
  "mrsd:study_design": "Study design",
  "mrsd:exposure_information_only": "Exposure (information only)",
  "mrsd:limitation": "Limitation",
  "mrsd:safety_factor_used": "Safety factor used",
};

function DosePanel({ study }: { study: Study }) {
  const dose = useApi(["preclinical", study.study_id, "dose-translation"], () =>
    unwrap(
      api.GET("/api/v1/preclinical/{study}/dose-translation", { params: { path: { study: study.study_id } } }),
    ),
  );
  const steps = dose.data?.current?.steps ?? [];
  const working = steps.filter((x) => x.role === "primary" || x.statistic === "mrsd:hed_cross_check_mg_per_kg" || x.statistic === "mrsd:most_appropriate_species");
  const context = steps.filter((x) => !working.includes(x));
  return (
    <section className="panel" aria-labelledby="dose-title">
      <div className="panel-head">
        <h2 id="dose-title">Dose translation</h2>
        <span className="src">preclinical.dose_translation, FDA 2005; a proposal, not a decision</span>
      </div>
      {dose.isPending ? <Loading what="the dose translation" /> : null}
      {dose.isError ? <ErrorNote error={dose.error} /> : null}
      {dose.isSuccess ? (
        <>
          <p className={dose.data.current ? "muted" : "message"} data-testid="dose-why">
            {dose.data.why}
          </p>
          {dose.data.current ? (
            <div className="stack" data-testid="dose-current">
              <div className="finding-meta">
                <FindingLink id={dose.data.current.finding_id} className="mono" />
                <StatusChip status={dose.data.current.status} />
                {dose.data.current.decided_by ? (
                  <span className="muted">
                    by {dose.data.current.decided_by}, ledger seq{" "}
                    <span className="mono">{dose.data.current.decided_seq}</span>
                  </span>
                ) : null}
              </div>
              <p className="headline">{dose.data.current.title}</p>
              <ol className="stack" aria-label="The working, step by step">
                {working.map((x) => (
                  <li key={`${x.statistic}-${x.selector}`}>
                    <strong>{STEP_LABEL[x.statistic] ?? x.statistic}:</strong> {x.display}{" "}
                    <span className="src mono">source {x.source}</span>
                  </li>
                ))}
              </ol>
              <ul className="stack" aria-label="Context and limitations">
                {context.map((x) => (
                  <li key={`${x.statistic}-${x.selector}`} className="muted">
                    <strong>{STEP_LABEL[x.statistic] ?? x.statistic}:</strong> {x.display}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {dose.data.superseded.length ? (
            <p className="muted">
              Superseded proposals (an earlier NOAEL):{" "}
              {dose.data.superseded.map((id, i) => (
                <span key={id}>
                  {i ? ", " : ""}
                  <FindingLink id={id} className="mono" />
                </span>
              ))}
            </p>
          ) : null}
        </>
      ) : null}
    </section>
  );
}

const REQ_STATUS: Record<Schemas["RequirementView"]["status"], string> = {
  met: "met",
  not_met: "not met",
  not_applicable: "not applicable",
};

const PROBLEM_TEXT: Record<string, string> = {
  required_study_missing: "no study",
  duration_short: "shorter than required",
  report_not_ready: "report not QA audited by submission",
  glp_status_missing: "GLP status not recorded",
};

function IndChecklist({ assetId }: { assetId: string }) {
  const ind = useApi(["preclinical", assetId, "ind-readiness"], () =>
    unwrap(api.GET("/api/v1/preclinical/assets/{asset}/ind-readiness", { params: { path: { asset: assetId } } })),
  );
  return (
    <section className="panel" aria-labelledby="ind-title">
      <div className="panel-head">
        <h2 id="ind-title">IND readiness</h2>
        <span className="src">preclinical.ind_readiness, ICH M3(R2)</span>
      </div>
      {ind.isPending ? <Loading what="the IND readiness checklist" /> : null}
      {ind.isError ? <ErrorNote error={ind.error} /> : null}
      {ind.isSuccess && ind.data.status === "not_loaded" ? (
        <p className="message">No nonclinical inventory has been loaded for {assetId}.</p>
      ) : null}
      {ind.isSuccess && ind.data.status === "assessed" ? (
        <>
          <p data-testid="ind-summary">
            <span className="fig">{String(ind.data.percent_met ?? 0)}%</span> of applicable requirements met (
            {String(ind.data.met)} of {String(ind.data.applicable)}), for a trial of{" "}
            {String(ind.data.planned_clinical_duration_days)} days of dosing
            {ind.data.multiple_dose ? " (multiple doses)" : " (one dose)"} and an IND planned for{" "}
            {ind.data.planned_submission_date}. A summary, not a finding; it does not say the IND can be submitted.
          </p>
          <div className="table-wrap" tabIndex={0} aria-label="IND readiness table">
            <table>
              <caption className="visually-hidden">Each first-in-human requirement, its status and its study</caption>
              <thead>
                <tr>
                  <th scope="col">Requirement</th>
                  <th scope="col">Status</th>
                  <th scope="col">Study</th>
                  <th scope="col">Gap</th>
                  <th scope="col">Source</th>
                </tr>
              </thead>
              <tbody>
                {ind.data.requirements.map((r) => (
                  <tr key={r.id} data-testid={`req-${r.id}`}>
                    <th scope="row">{r.title}</th>
                    <td>
                      <span className={`chip ${r.status === "met" ? "good" : r.status === "not_met" ? "warning" : "outline"}`}>
                        {REQ_STATUS[r.status]}
                      </span>
                    </td>
                    <td>
                      {r.study_id ? (
                        <>
                          <span className="mono">{r.study_id}</span>
                          <div className="muted">
                            {r.duration_weeks !== null ? `${String(r.duration_weeks)} weeks` : ""}
                            {r.required_weeks !== null ? ` (at least ${r.required_weeks.toPrecision(2)})` : ""}
                            {r.glp ? `, GLP ${r.glp}` : ""}
                            {r.report_status ? `, report ${r.report_status.replace("_", " ")}` : ""}
                          </div>
                        </>
                      ) : (
                        <span className="muted">none</span>
                      )}
                    </td>
                    <td>
                      {r.problems.map((p) => PROBLEM_TEXT[p] ?? p).join("; ")}
                      {r.finding_ids.map((id) => (
                        <div key={id}>
                          <FindingLink id={id} className="mono" />
                        </div>
                      ))}
                    </td>
                    <td>
                      <span title={r.quote}>
                        {r.source} {r.section}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : null}
    </section>
  );
}

function TranslationalPanel({ study }: { study: Study }) {
  const t = useApi(["preclinical", study.study_id, "translational"], () =>
    unwrap(
      api.GET("/api/v1/preclinical/{study}/translational", { params: { path: { study: study.study_id } } }),
    ),
  );
  const rec = t.data?.current;
  return (
    <section className="panel" aria-labelledby="translational-title">
      <div className="panel-head">
        <h2 id="translational-title">Translational</h2>
        <span className="src">preclinical.translational_bridge; a proposal, not a decision</span>
      </div>
      {t.isPending ? <Loading what="the monitoring recommendation" /> : null}
      {t.isError ? <ErrorNote error={t.error} /> : null}
      {t.isSuccess ? (
        <p className={rec ? "muted" : "message"} data-testid="translational-why">
          {t.data.why}
        </p>
      ) : null}
      {rec ? (
        <div className="stack" data-testid="translational-current">
          <div className="finding-meta">
            <FindingLink id={rec.finding_id} className="mono" />
            <StatusChip status={rec.status} />
            {rec.decided_by ? (
              <span className="muted">
                by {rec.decided_by}, ledger seq <span className="mono">{rec.decided_seq}</span>
              </span>
            ) : null}
          </div>
          <p className="headline">{rec.title}</p>
          {rec.items.length === 0 ? <p className="muted">No target organ in the findings the NOAEL rests on.</p> : null}
          {rec.items.map((i) => (
            <article key={i.organ} className="finding" aria-label={`Monitoring: ${i.label}`}>
              <h3>{i.label}</h3>
              <dl className="kv">
                <div>
                  <dt>Measure</dt>
                  <dd>{i.measure}</dd>
                </div>
                <div>
                  <dt>Frequency</dt>
                  <dd>{i.frequency}</dd>
                </div>
                <div>
                  <dt>Review</dt>
                  <dd>{i.review}</dd>
                </div>
                <div>
                  <dt>Stopping</dt>
                  <dd>{i.stopping}</dd>
                </div>
                <div>
                  <dt>Rests on</dt>
                  <dd>
                    {i.findings.map((id, n) => (
                      <span key={id}>
                        {n ? ", " : ""}
                        <FindingLink id={id} className="mono" />
                      </span>
                    ))}
                  </dd>
                </div>
              </dl>
              <details>
                <summary>Sources ({String(i.sources.length)})</summary>
                <ul className="stack">
                  {i.sources.map((src) => (
                    <li key={`${src.source}-${src.section}`}>
                      <strong>
                        {src.source}, {src.section}:
                      </strong>{" "}
                      <q>{src.quote}</q>
                    </li>
                  ))}
                </ul>
              </details>
            </article>
          ))}
          {rec.unmapped.length ? (
            <div aria-label="Unmapped findings">
              <h3>Unmapped (no mapping entry, no rule given)</h3>
              <ul>
                {rec.unmapped.map((u) => (
                  <li key={u}>{u}</li>
                ))}
              </ul>
            </div>
          ) : null}
          <div aria-label="Safety margins">
            <h3>Safety margins</h3>
            <p className="muted">{rec.starting_dose}</p>
            <table>
              <caption className="visually-hidden">NOAEL and LOAEL as human equivalent doses against the starting dose</caption>
              <thead>
                <tr>
                  <th scope="col">Dose</th>
                  <th scope="col">Human equivalent</th>
                  <th scope="col" className="num">
                    Margin
                  </th>
                </tr>
              </thead>
              <tbody>
                {rec.margins.map((m) => (
                  <tr key={m.label} data-testid={`margin-${m.label}`}>
                    <th scope="row">{m.label}</th>
                    <td>{m.display}</td>
                    <td className="num">{m.ratio === null ? "no starting dose" : `${m.ratio.toPrecision(3)} x`}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="muted">{rec.basis}</p>
        </div>
      ) : null}
    </section>
  );
}
