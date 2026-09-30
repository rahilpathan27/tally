import "server-only";
import { createHash, createHmac, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";

/**
 * The demo store's backend. It holds the merchant API secret (never sent to the browser) and
 * signs requests to the Tally merchant API exactly as libs/security/hmac_auth.py verifies them.
 */
type Credentials = { core_url: string; key_id: string; secret_b64: string; risk_url: string; internal_key: string };

function credentials(): Credentials {
  if (process.env.TALLY_DEMO_KEY_ID && process.env.TALLY_DEMO_SECRET_B64) {
    return {
      core_url: process.env.TALLY_CORE_URL ?? "http://127.0.0.1:8000",
      key_id: process.env.TALLY_DEMO_KEY_ID,
      secret_b64: process.env.TALLY_DEMO_SECRET_B64,
      risk_url: process.env.TALLY_RISK_URL ?? "http://127.0.0.1:8030",
      internal_key: process.env.TALLY_RISK_INTERNAL_KEY ?? "",
    };
  }
  const file = path.resolve(process.cwd(), "../../.data/dev-stack.json");
  const data = JSON.parse(readFileSync(file, "utf8"));
  return { ...data, risk_url: "http://127.0.0.1:8030", internal_key: data.risk_key };
}

export async function merchantApi(method: "GET" | "POST", pathname: string, body?: unknown, idempotencyKey?: string) {
  const creds = credentials();
  const raw = body === undefined ? "" : JSON.stringify(body);
  const timestamp = Math.floor(Date.now() / 1000);
  const nonce = `demo-${randomUUID()}`;
  const bodyHash = createHash("sha256").update(raw).digest("hex");
  const canonical = `${method}\n${pathname}\n${bodyHash}\n${timestamp}\n${nonce}`;
  const signature = createHmac("sha256", Buffer.from(creds.secret_b64, "base64")).update(canonical).digest("hex");
  const headers: Record<string, string> = {
    "x-tally-key-id": creds.key_id,
    "x-tally-timestamp": String(timestamp),
    "x-tally-nonce": nonce,
    "x-tally-signature": signature,
  };
  if (raw) headers["content-type"] = "application/json";
  if (idempotencyKey) headers["idempotency-key"] = idempotencyKey;
  const response = await fetch(`${creds.core_url}${pathname}`, { method, headers, body: raw || undefined, cache: "no-store" });
  return { status: response.status, body: await response.json() };
}

export async function payerOtp(challengeId: string): Promise<string | null> {
  const creds = credentials();
  const response = await fetch(`${creds.risk_url}/internal/v1/step_up/${challengeId}/code`, {
    headers: { "x-internal-key": creds.internal_key, "x-actor": "payer-simulator" },
    cache: "no-store",
  });
  if (!response.ok) return null;
  return String((await response.json()).code);
}
