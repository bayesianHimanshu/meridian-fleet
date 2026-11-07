import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { ApiError } from "./api/client";
import { IdentityProvider } from "./identity";

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // An answer from the API (404, 403, ...) is final; only network failures retry.
        retry: (count, error) => !(error instanceof ApiError) && count < 2,
        refetchOnWindowFocus: false,
        staleTime: 15_000,
      },
    },
  });
}

export function Providers({ client, children }: { client: QueryClient; children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <IdentityProvider>{children}</IdentityProvider>
    </QueryClientProvider>
  );
}
