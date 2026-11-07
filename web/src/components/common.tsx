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

export function SeverityChip({ severity }: { severity: Schemas["Severity"] }) {
  return <span className={`chip ${SEVERITY_CLASS[severity]}`}>{severity} severity</span>;
}

const STATUS_CLASS: Record<string, string> = {
  open: "accent",
  escalated: "warning",
  accepted: "good",
  dismissed: "",
};

export function StatusChip({ status }: { status: string }) {
  return <span className={`chip ${STATUS_CLASS[status] ?? ""}`}>{status}</span>;
}

export function when(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  return `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

export function agentName(agentId: string): string {
  return agentId.replace(/^rbqm\./, "");
}

export function Loading({ what }: { what: string }) {
  return <p className="muted">Loading {what}</p>;
}
