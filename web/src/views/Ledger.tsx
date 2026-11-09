import { useInfiniteQuery, useMutation } from "@tanstack/react-query";
import { type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import { api, unwrap, type Schemas } from "../api/client";
import { Hash } from "../components/Hash";
import { ErrorNote, Loading, Time } from "../components/common";
import { FindingLink as Linked, WithTimes } from "../components/text";
import { ROLE_LABELS, useIdentity, type Person } from "../identity";

type Entry = Schemas["LedgerPage"]["entries"][number];
type EventType = Schemas["EventType"];

const PAGE = 50;
const TYPES: EventType[] = [
  "data_cut",
  "actor_woke",
  "actor_suspended",
  "finding_raised",
  "finding_narrative",
  "human_decision",
  "policy_denied",
  "containment_check_completed",
  "document_filed",
];

function typesText(types: EventType[]): string {
  return types.map((t) => t.replaceAll("_", " ")).join(" and ");
}

export function isRedacted(entry: Entry): entry is Schemas["RedactedEvent"] {
  return "marker" in entry;
}

function FindingLink({ id }: { id: string | null | undefined }) {
  return id ? (
    <Linked id={id} className="mono" />
  ) : (
    <span>Finding</span>
  );
}

/** One line on what the event says, in plain words. A person is named once, from
 * config/people.toml; agents keep their ids. Finding ids link to the finding. */
export function describe(entry: Schemas["Event"], people: Person[]): ReactNode {
  const p = entry.payload;
  switch (p.type) {
    case "data_cut":
      return `Data cut ${p.cut_id} for ${p.study_id}`;
    case "actor_woke":
      return `${p.agent_id} ${p.agent_version ?? ""} started on trigger ${p.trigger_seq}`;
    case "actor_suspended":
      return `${p.agent_id} ${p.agent_version ?? ""} finished trigger ${p.trigger_seq}${p.executor ? ` (${p.executor})` : ""}`;
    case "finding_raised":
      return (
        <>
          <FindingLink id={p.finding.finding_id} />: <WithTimes text={p.finding.title} />
        </>
      );
    case "finding_narrative":
      return (
        <>
          Narrative for <FindingLink id={p.finding_id} />: {p.status}
        </>
      );
    case "human_decision": {
      const d = p.decision;
      const name = people.find((x) => x.actor === d.actor)?.name ?? d.actor;
      return (
        <>
          <FindingLink id={d.finding_id} /> {d.action} by {name} ({ROLE_LABELS[d.role]})
        </>
      );
    }
    case "policy_denied":
      return `Refused: ${p.resource} (${p.check ?? p.reason})`;
    case "containment_check_completed":
      return `Containment check ${p.policy_name ?? p.zone}: ${p.passed ? "passed" : "failed"}`;
    case "document_filed":
      return `Document ${p.doc_id} filed`;
  }
}

function VerifyPanel() {
  const run = useMutation({ mutationFn: () => unwrap(api.GET("/api/v1/ledger/verify")) });
  return (
    <div className="stack" style={{ gap: "8px" }}>
      <div>
        <button
          type="button"
          className="btn"
          onClick={() => {
            run.mutate();
          }}
          disabled={run.isPending}
        >
          {run.isPending ? "Verifying" : "Verify the hash chain"}
        </button>
      </div>
      <div aria-live="polite">
        {run.isError ? <ErrorNote error={run.error} /> : null}
        {run.isSuccess ? (
          <p className={`message ${run.data.ok ? "ok" : "error"}`}>
            {run.data.ok
              ? `The chain verifies: ${run.data.events_checked} events checked, every hash recomputes.`
              : `Broken at seq ${run.data.first_broken_seq ?? "?"}: ${run.data.problem ?? "unknown problem"}`}
          </p>
        ) : null}
      </div>
    </div>
  );
}

export function LedgerView() {
  const { person, people } = useIdentity();
  // Filters live in the URL so another page can link to them (the fleet page links to an
  // agent's run events): ?agent=<agent id>&type=<event type>, type repeatable.
  const [params, setParams] = useSearchParams();
  const types = params.getAll("type").filter((t): t is EventType => (TYPES as string[]).includes(t));
  const agent = params.get("agent");
  const type: EventType | "" = types.length === 1 ? (types[0] ?? "") : "";
  const setType = (value: EventType | "") => {
    const next = new URLSearchParams(params);
    next.delete("type");
    if (value) {
      next.append("type", value);
    }
    setParams(next);
  };
  const pages = useInfiniteQuery({
    queryKey: [person.actor, "ledger", types.join(","), agent ?? ""],
    initialPageParam: undefined as number | undefined,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/api/v1/ledger", {
          params: {
            query: {
              order: "desc",
              limit: PAGE,
              ...(pageParam === undefined ? {} : { before: pageParam }),
              ...(types.length > 0 ? { type: types } : {}),
              ...(agent ? { agent } : {}),
            },
          },
        }),
      ),
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
  const entries = pages.data?.pages.flatMap((p) => p.entries) ?? [];

  return (
    <section aria-labelledby="ledger-title" className="stack">
      <div className="view-head">
        <div>
          <h1 id="ledger-title">Event ledger</h1>
          <p>
            Every event, newest first, as your role may see it. The ledger is append-only and hash-chained:
            nothing is ever changed or deleted. Restricted events show only that they exist.
          </p>
        </div>
        <VerifyPanel />
      </div>
      <div className="toolbar">
        <label className="field">
          Event type
          <select
            value={type}
            onChange={(e) => {
              setType(e.target.value as EventType | "");
            }}
          >
            <option value="">{types.length > 1 ? typesText(types) : "All types"}</option>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {t.replaceAll("_", " ")}
              </option>
            ))}
          </select>
        </label>
        {agent ? (
          <p className="message" data-testid="agent-filter">
            Only events of <span className="mono">{agent}</span>
            {types.length > 0 ? ` (${typesText(types)})` : ""}. A run that started and never finished shows as
            a started event with no finished one. <Link to="/ledger">Show every event</Link>
          </p>
        ) : null}
      </div>
      {pages.isPending ? <Loading what="the ledger" /> : null}
      {pages.isError ? <ErrorNote error={pages.error} /> : null}
      {pages.isSuccess ? (
        <section className="panel">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th scope="col" className="num">
                    Seq
                  </th>
                  <th scope="col">Recorded</th>
                  <th scope="col">Type</th>
                  <th scope="col">What happened</th>
                  <th scope="col">Hash</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e) =>
                  isRedacted(e) ? (
                    <tr key={e.seq} className="ledger-row redacted" data-testid="redacted-row">
                      <td className="num">{e.seq}</td>
                      <td className="mono"><Time iso={e.recorded_at} /></td>
                      <td>{e.event_type.replaceAll("_", " ")}</td>
                      <td>Redacted: a restricted event your role may not read.</td>
                      <td>
                        <Hash value={e.hash} label="event hash" />
                      </td>
                    </tr>
                  ) : (
                    <tr key={e.seq} className={`ledger-row${e.visibility === "restricted" ? " restricted-row" : ""}`}>
                      <td className="num">{e.seq}</td>
                      <td className="mono"><Time iso={e.recorded_at} /></td>
                      <td>{e.payload.type.replaceAll("_", " ")}</td>
                      <td>
                        {describe(e, people)}
                        {e.payload.type === "human_decision" ? null : (
                          <>
                            <br />
                            <span className="muted">by {e.actor}</span>
                          </>
                        )}
                      </td>
                      <td>
                        <Hash value={e.hash} label="event hash" />
                      </td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          </div>
          {pages.hasNextPage ? (
            <p style={{ marginTop: "12px" }}>
              <button
                type="button"
                className="btn"
                disabled={pages.isFetchingNextPage}
                onClick={() => {
                  void pages.fetchNextPage();
                }}
              >
                Show older events
              </button>
            </p>
          ) : (
            <p className="muted" style={{ marginTop: "12px" }}>
              This is the first event.
            </p>
          )}
        </section>
      ) : null}
    </section>
  );
}
