// Every query is keyed by the person it was made as, so switching person refetches and
// never shows another person's data.
import { useQuery, type UseQueryResult } from "@tanstack/react-query";
import { useIdentity } from "./identity";

export function useApi<T>(key: readonly unknown[], fetcher: () => Promise<T>): UseQueryResult<T> {
  const { person } = useIdentity();
  return useQuery({ queryKey: [person.actor, ...key], queryFn: fetcher });
}
