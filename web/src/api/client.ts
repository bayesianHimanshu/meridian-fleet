// The Fleet API client, generated from openapi/meridian-api.json (src/api/schema.ts) and
// called with openapi-fetch. Every request carries the development identity headers; a
// request without an identity is never sent (only /people and /health need none).
import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";

export type Schemas = components["schemas"];
export type Role = Schemas["Role"];

export interface Identity {
  actor: string;
  role: Role;
}

export const ACTOR_HEADER = "X-Meridian-Actor";
export const ROLE_HEADER = "X-Meridian-Role";
const PUBLIC_PATHS = new Set(["/api/v1/people", "/api/v1/health"]);

let current: Identity | null = null;

export function setIdentity(identity: Identity | null): void {
  current = identity;
}

export class MissingIdentityError extends Error {
  constructor() {
    super("No person is selected, so no request was sent.");
    this.name = "MissingIdentityError";
  }
}

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const identityMiddleware: Middleware = {
  onRequest({ request, schemaPath }) {
    if (PUBLIC_PATHS.has(schemaPath)) {
      return request;
    }
    if (current === null) {
      throw new MissingIdentityError();
    }
    request.headers.set(ACTOR_HEADER, current.actor);
    request.headers.set(ROLE_HEADER, current.role);
    return request;
  },
};

export const api = createClient<paths>({
  baseUrl: window.location.origin,
  // Looked up at call time, not captured at import (tests install MSW after import).
  fetch: (request: Request) => globalThis.fetch(request),
});
api.use(identityMiddleware);

interface ValidationItem {
  msg?: string;
  loc?: unknown[];
}

/** The API's own message, as it sent it: a string detail, or FastAPI's validation list. */
export function messageOf(error: unknown, status: number): string {
  if (typeof error === "object" && error !== null && "detail" in error) {
    const detail = (error).detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      return (detail as ValidationItem[])
        .map((d) => {
          const where = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
          return where ? `${where}: ${d.msg ?? "invalid"}` : (d.msg ?? "invalid");
        })
        .join("; ");
    }
  }
  return `The request failed (HTTP ${status}).`;
}

/** The data of an openapi-fetch result, or an ApiError carrying the API's message. */
export async function unwrap<T>(
  call: Promise<{ data?: T; error?: unknown; response: Response }>,
): Promise<T> {
  const { data, error, response } = await call;
  if (!response.ok || data === undefined) {
    throw new ApiError(response.status, messageOf(error, response.status));
  }
  return data;
}
