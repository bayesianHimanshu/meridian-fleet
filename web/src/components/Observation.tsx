import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type SyntheticEvent,
} from "react";
import { api, unwrap, type Schemas } from "../api/client";
import { useIdentity } from "../identity";
import { ErrorNote } from "./common";
import { FindingLink } from "./text";

/** What an observation refers to: a finding, or one ledger event. */
export type RefersTo = { finding_id: string } | { ledger_seq: number };

function refersText(r: RefersTo): string {
  return "finding_id" in r
    ? `finding ${r.finding_id}`
    : `ledger seq ${String(r.ledger_seq)}`;
}

/** "Raise an observation", for the QA auditor only (nobody else sees it). A short form:
 * title, description, severity. Enter submits only from the description field (Shift+Enter
 * starts a new line there); in the title or the severity it does nothing. */
export function RaiseObservation({ refersTo }: { refersTo: RefersTo }) {
  const { person } = useIdentity();
  const [open, setOpen] = useState(false);
  if (person.role !== "qa_auditor") return null;
  return open ? (
    <ObservationForm
      refersTo={refersTo}
      onClose={() => {
        setOpen(false);
      }}
    />
  ) : (
    <button
      type="button"
      className="btn"
      onClick={() => {
        setOpen(true);
      }}
    >
      Raise an observation
    </button>
  );
}

export function ObservationForm({
  refersTo,
  onClose,
}: {
  refersTo: RefersTo;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const { person } = useIdentity();
  const ids = useId();
  const descriptionRef = useRef<HTMLTextAreaElement>(null);
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [severity, setSeverity] = useState<Schemas["Severity"]>("medium");
  const [localError, setLocalError] = useState<string | null>(null);
  const raise = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/quality/observations", {
          body: {
            title: title.trim(),
            description: description.trim(),
            severity,
            ...refersTo,
          },
        }),
      ),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: [person.actor] });
    },
  });
  const submit = (e?: SyntheticEvent) => {
    e?.preventDefault();
    setLocalError(null);
    if (title.trim() === "" || description.trim() === "") {
      setLocalError("Give the observation a title and a description.");
      return;
    }
    raise.mutate();
  };
  const onKey = (e: KeyboardEvent<HTMLFormElement>) => {
    if (e.key !== "Enter") return;
    if (e.target === descriptionRef.current && !e.shiftKey) {
      e.preventDefault();
      submit();
    } else if (e.target !== descriptionRef.current) {
      e.preventDefault(); // never from the title, the severity or a button
    }
  };
  if (raise.isSuccess) {
    return (
      <p className="message ok" role="status" data-testid="observation-raised">
        Observation recorded as{" "}
        <FindingLink id={raise.data.finding_id} className="mono" /> (ledger seq{" "}
        <span className="mono">{raise.data.observation_seq}</span>). Clinical
        operations or the medical monitor will decide it.
      </p>
    );
  }
  return (
    <form
      className="observation"
      onSubmit={submit}
      onKeyDown={onKey}
      aria-label={`Raise an observation about ${refersText(refersTo)}`}
    >
      <p className="muted">
        About {refersText(refersTo)}. You raise it; someone else decides it.
      </p>
      <label className="field" htmlFor={`${ids}-title`}>
        Title
      </label>
      <input
        id={`${ids}-title`}
        type="text"
        value={title}
        maxLength={200}
        autoFocus
        onChange={(e) => {
          setTitle(e.target.value);
        }}
      />
      <label className="field" htmlFor={`${ids}-description`}>
        Description{" "}
        <span className="muted">
          (Enter records it; Shift+Enter for a new line)
        </span>
      </label>
      <textarea
        id={`${ids}-description`}
        ref={descriptionRef}
        rows={3}
        value={description}
        maxLength={4000}
        onChange={(e) => {
          setDescription(e.target.value);
        }}
      />
      <label className="field" htmlFor={`${ids}-severity`}>
        Severity
      </label>
      <select
        id={`${ids}-severity`}
        value={severity}
        onChange={(e) => {
          setSeverity(e.target.value as Schemas["Severity"]);
        }}
      >
        <option value="high">High</option>
        <option value="medium">Medium</option>
        <option value="low">Low</option>
      </select>
      <div className="observation-actions">
        <button
          type="submit"
          className="btn primary"
          disabled={raise.isPending}
        >
          Record observation
        </button>
        <button type="button" className="btn" onClick={onClose}>
          Cancel
        </button>
      </div>
      <div aria-live="polite">
        {localError ? (
          <p className="message error" role="alert">
            {localError}
          </p>
        ) : null}
        {raise.isError ? <ErrorNote error={raise.error} /> : null}
      </div>
    </form>
  );
}
