"use client";

import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { Session } from "@/lib/schemas";

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 10_000,
            // Retry transient server errors only; client errors and contract violations are final.
            retry: (count, error) =>
              !(error instanceof ApiError && (error.status < 500 || error.code === "INVALID_RESPONSE")) &&
              count < 2,
            refetchOnWindowFocus: false,
          },
        },
      }),
  );
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

export function useSession() {
  return useQuery({
    queryKey: ["session"],
    queryFn: () => api("/auth/session", Session),
    staleTime: 60_000,
  });
}

export function hasAny(roles: string[] | undefined, allowed: string[]): boolean {
  return !!roles?.some((role) => allowed.includes(role));
}
