import { ApiError, type Schemas } from "../api/client";

export function ErrorNote({ error }: { error: unknown }) {
  const text = error instanceof Error ? error.message : "Something went wrong.";
  return (
    <p className="message error" role="alert">
      {text}
    </p>
  );
}

export function isStatus(error: unknown, status: number): boolean {
  return error instanceof ApiError && error.status === status;
}

const SEVERITY_CLASS: Record<Schemas["Severity"], string> = {
  high: "critical",
  medium: "warning",
  low: "",
};

const SEVERITY_TEXT: Record<Schemas["Severity"], string> = {
  high: "High",
  medium: "Medium",
  low: "Low",
};

export function SeverityChip({ severity }: { severity: Schemas["Severity"] }) {
  return <span className={`chip ${SEVERITY_CLASS[severity]}`}>{SEVERITY_TEXT[severity]}</span>;
}

/** Status chips are neutral and outlined; only escalated gets the warning outline. The
 * accent colour is kept for navigation, selection and actions. */
export function StatusChip({ status }: { status: string }) {
  const label = status.charAt(0).toUpperCase() + status.slice(1);
  return <span className={`chip status${status === "escalated" ? " status-warning" : ""}`}>{label}</span>;
}

export function when(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function localTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : `Your local time: ${d.toLocaleString()}`;
}

/** A time in UTC, with the reader's local time on hover. */
export function Time({ iso }: { iso: string }) {
  return (
    <time dateTime={iso} title={localTime(iso)}>
      {when(iso)}
    </time>
  );
}

export function agentName(agentId: string): string {
  return agentId.replace(/^rbqm\./, "");
}

export function Loading({ what }: { what: string }) {
  return <p className="muted">Loading {what}</p>;
}
