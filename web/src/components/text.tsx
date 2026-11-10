import type { ReactNode } from "react";
import { Link, useLocation } from "react-router";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// An ISO 8601 instant with a time and an offset, as agents write them into titles,
// summaries and evidence (2013-01-20T22:02:00+00:00, or ending in Z).
const ISO_INSTANT = /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})/g;

/** "20 Jan 2013, 22:02 UTC"; the input unchanged if it is not a valid instant. */
export function humanInstant(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) {
    return iso;
  }
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}, ${hh}:${mm} UTC`;
}

/** Text with every ISO instant shown as "20 Jan 2013, 22:02 UTC", the ISO value kept in the
 * title attribute and dateTime. The ledger and the API keep ISO; only the display changes. */
export function WithTimes({ text }: { text: string }): ReactNode {
  const parts: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(ISO_INSTANT)) {
    const at = m.index;
    const iso = m[0];
    if (Number.isNaN(new Date(iso).getTime())) {
      continue;
    }
    parts.push(text.slice(last, at));
    parts.push(
      <time key={`${String(at)}-${iso}`} dateTime={iso} title={iso}>
        {humanInstant(iso)}
      </time>,
    );
    last = at + iso.length;
  }
  if (parts.length === 0) {
    return text;
  }
  parts.push(text.slice(last));
  return <>{parts}</>;
}

/** Where a finding was opened from, carried in the link's state for its back link. */
export interface From {
  path: string;
  label: string;
}

const ORIGINS: { prefix: string; label: string }[] = [
  { prefix: "/safety", label: "Back to the Safety page" },
  { prefix: "/rbqm", label: "Back to the RBQM page" },
  { prefix: "/quality", label: "Back to the TMF page" },
  { prefix: "/ledger", label: "Back to the ledger" },
  { prefix: "/review", label: "Back to the review queue" },
];

export const FALLBACK_FROM: From = { path: "/review", label: "Back to the review queue" };

/** The page a finding link is on, as a From (null on a page that is not an origin). */
export function useHere(): From | null {
  const location = useLocation();
  const origin = ORIGINS.find((o) => location.pathname.startsWith(o.prefix));
  return origin ? { path: `${location.pathname}${location.search}`, label: origin.label } : null;
}

export function isFrom(value: unknown): value is From {
  if (typeof value !== "object" || value === null) {
    return false;
  }
  const v = value as Record<string, unknown>;
  return (
    typeof v.path === "string" &&
    typeof v.label === "string" &&
    ORIGINS.some((o) => (v.path as string).startsWith(o.prefix))
  );
}

/** A link to a finding that remembers the page it was followed from. */
export function FindingLink({
  id,
  className,
  children,
}: {
  id: string;
  className?: string;
  children?: ReactNode;
}) {
  const here = useHere();
  return (
    <Link to={`/findings/${id}`} className={className} state={here ? { from: here } : undefined}>
      {children ?? id}
    </Link>
  );
}
