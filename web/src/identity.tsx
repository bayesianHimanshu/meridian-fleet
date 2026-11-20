// Development identity: who the Command Center is acting as. Not sign-in. The six demo
// people come from GET /api/v1/people (config/people.toml); the choice is kept in
// localStorage and sent as two headers on every request.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { api, setIdentity, unwrap, type Role, type Schemas } from "./api/client";

export type Person = Schemas["PersonView"];

const STORAGE_KEY = "meridian.command-center.actor";

export const ROLE_LABELS: Record<Role, string> = {
  clinical_ops_lead: "Clinical operations lead",
  medical_monitor: "Medical monitor",
  qa_auditor: "QA auditor",
  portfolio_lead: "Portfolio lead",
  toxicologist: "Toxicologist",
  unblinded_statistician: "Unblinded statistician",
};

function readStored(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null; // storage may be unavailable (private mode, blocked): fall back
  }
}

function writeStored(actor: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, actor);
  } catch {
    // keep working without persistence
  }
}

interface IdentityValue {
  people: Person[];
  person: Person;
  choose: (actor: string) => void;
}

const IdentityContext = createContext<IdentityValue | null>(null);

export function useIdentity(): IdentityValue {
  const value = useContext(IdentityContext);
  if (value === null) {
    throw new Error("useIdentity outside IdentityProvider");
  }
  return value;
}

export function IdentityProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const people = useQuery({
    queryKey: ["people"],
    queryFn: () => unwrap(api.GET("/api/v1/people")),
    staleTime: Infinity,
  });
  const [actor, setActor] = useState<string | null>(readStored);

  const list = people.data ?? [];
  const person = list.find((p) => p.actor === actor) ?? list[0];
  // Set before any child renders, so the first query already carries the headers.
  setIdentity(person ? { actor: person.actor, role: person.role } : null);

  const choose = useCallback(
    (next: string) => {
      writeStored(next);
      setActor(next);
      // Nothing cached for one person may be shown to another.
      queryClient.removeQueries({ predicate: (q) => q.queryKey[0] !== "people" });
    },
    [queryClient],
  );

  const value = useMemo(
    () => (person ? { people: list, person, choose } : null),
    [list, person, choose],
  );

  if (people.isError) {
    return (
      <p className="message error" role="alert">
        The Fleet API did not answer. Start it with <code>meridian api serve</code> and reload.
      </p>
    );
  }
  if (value === null) {
    return <p className="message">Loading people</p>;
  }
  return <IdentityContext.Provider value={value}>{children}</IdentityContext.Provider>;
}
