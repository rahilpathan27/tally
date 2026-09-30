import { z } from "zod";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)tally_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

async function parseError(response: Response): Promise<ApiError> {
  let code = `HTTP_${response.status}`;
  let message = response.statusText || "Request failed";
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (detail && typeof detail === "object" && !Array.isArray(detail)) {
      code = String(detail.code ?? code);
      message = String(detail.message ?? message);
    } else if (typeof detail === "string") {
      message = detail;
    }
  } catch {
    // Non-JSON error body: keep the status text.
  }
  return new ApiError(response.status, code, message);
}

export async function api<T>(
  path: string,
  schema: z.ZodType<T>,
  init: { method?: string; body?: unknown; idempotencyKey?: string; signal?: AbortSignal } = {},
): Promise<T> {
  const method = init.method ?? "GET";
  const headers: Record<string, string> = {};
  if (init.body !== undefined) headers["content-type"] = "application/json";
  if (method !== "GET") headers["x-csrf-token"] = csrfToken();
  if (init.idempotencyKey) headers["idempotency-key"] = init.idempotencyKey;
  const response = await fetch(path, {
    method,
    headers,
    body: init.body === undefined ? undefined : JSON.stringify(init.body),
    credentials: "same-origin",
    signal: init.signal,
    cache: "no-store",
  });
  if (response.status === 401 && typeof window !== "undefined" && !path.startsWith("/auth/")) {
    // A plain helper (not a component) has no router; a full navigation also drops stale state.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.assign(`/login?next=${encodeURIComponent(window.location.pathname)}`);
  }
  if (!response.ok) throw await parseError(response);
  const json: unknown = await response.json();
  const parsed = schema.safeParse(json);
  if (!parsed.success) {
    throw new ApiError(502, "INVALID_RESPONSE", `Unexpected response from ${path}`);
  }
  return parsed.data;
}
