import { render, type RenderResult } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { App } from "../src/App";
import { Providers, makeQueryClient } from "../src/root";

type Entry = string | { pathname: string; search?: string; state?: unknown };

export function renderApp(path: Entry, actor = "asha.rao"): RenderResult {
  window.localStorage.setItem("meridian.command-center.actor", actor);
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { retry: false, staleTime: 0 } });
  return render(
    <Providers client={client}>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </Providers>,
  );
}
