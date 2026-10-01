// Merchant payment load: signed UPI create + confirm through the real core API.
//
// Env: CORE_URL, MERCHANTS_FILE (JSON list of {key_id, secret_b64}), PAYERS, SCENARIO
// (ramp | steady | spike), RATE (payments/s for steady; peak for ramp/spike), DURATION.
// Each iteration is one payment; arrival-rate executors keep the offered load independent of
// latency, so a slow system shows up as latency and dropped iterations, not as less load.
import http from "k6/http";
import crypto from "k6/crypto";
import encoding from "k6/encoding";
import { check } from "k6";
import { Counter, Trend } from "k6/metrics";

const CORE = __ENV.CORE_URL || "http://tally-core:8000";
const MERCHANTS = JSON.parse(open(__ENV.MERCHANTS_FILE || "/creds/merchants.json")).map((m) => ({
  keyId: m.key_id,
  secret: encoding.b64decode(m.secret_b64, "std"),
}));
const PAYERS = parseInt(__ENV.PAYERS || "20000", 10);
const RATE = parseInt(__ENV.RATE || "50", 10);
const DURATION = __ENV.DURATION || "5m";

const paymentE2E = new Trend("payment_e2e_ms", true);
const createLatency = new Trend("create_ms", true);
const confirmLatency = new Trend("confirm_ms", true);
const outcomes = new Counter("payment_outcomes");

const scenarios = {
  // Step the offered rate up to find where latency or errors break the SLO.
  ramp: {
    executor: "ramping-arrival-rate",
    startRate: Math.max(1, Math.floor(RATE / 10)),
    timeUnit: "1s",
    preAllocatedVUs: 200,
    maxVUs: 1500,
    stages: [
      { target: Math.floor(RATE * 0.25), duration: "1m" },
      { target: Math.floor(RATE * 0.5), duration: "1m" },
      { target: Math.floor(RATE * 0.75), duration: "1m" },
      { target: RATE, duration: "1m" },
      { target: RATE, duration: "1m" },
    ],
  },
  steady: {
    executor: "constant-arrival-rate",
    rate: RATE,
    timeUnit: "1s",
    duration: DURATION,
    preAllocatedVUs: 200,
    maxVUs: 1500,
  },
  // Quiet, then 3x the rate for a minute, then quiet again: does the system recover?
  spike: {
    executor: "ramping-arrival-rate",
    startRate: Math.max(1, Math.floor(RATE / 3)),
    timeUnit: "1s",
    preAllocatedVUs: 300,
    maxVUs: 2000,
    stages: [
      { target: Math.floor(RATE / 3), duration: "1m" },
      { target: RATE, duration: "10s" },
      { target: RATE, duration: "1m" },
      { target: Math.floor(RATE / 3), duration: "10s" },
      { target: Math.floor(RATE / 3), duration: "2m" },
    ],
  },
};

export const options = {
  scenarios: { load: scenarios[__ENV.SCENARIO || "steady"] },
  discardResponseBodies: false,
  thresholds: {
    http_req_failed: ["rate<0.01"],
    payment_e2e_ms: ["p(50)<300", "p(99)<1500"],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

function signed(merchant, path, body) {
  const raw = JSON.stringify(body);
  const ts = Math.floor(Date.now() / 1000);
  const nonce = `k6-${__VU}-${__ITER}-${Math.random().toString(36).slice(2)}`;
  const canonical = `POST\n${path}\n${crypto.sha256(raw, "hex")}\n${ts}\n${nonce}`;
  return http.post(`${CORE}${path}`, raw, {
    headers: {
      "content-type": "application/json",
      "x-tally-key-id": merchant.keyId,
      "x-tally-timestamp": String(ts),
      "x-tally-nonce": nonce,
      "x-tally-signature": crypto.hmac("sha256", merchant.secret, canonical, "hex"),
      "idempotency-key": `${__VU}-${__ITER}-${nonce}`,
    },
    timeout: "10s",
  });
}

export default function () {
  const merchant = MERCHANTS[(__VU + __ITER) % MERCHANTS.length];
  const payerIndex = Math.floor(Math.random() * PAYERS);
  const payer = `load${payerIndex}@bank-a`;
  const started = Date.now();
  const created = signed(merchant, "/v1/payment_intents", {
    amount_minor: 1000 + Math.floor(Math.random() * 400000),
    currency: "INR",
    payment_method_type: "upi",
    payer_vpa: payer,
    payee_vpa: "merchant@bank-b",
    // Checkout context as a real integration sends it: each payer has their own phone.
    // Without it every request looks like one device paying thousands of times, which the
    // risk engine (correctly) holds or blocks.
    risk_context: {
      device_id: `phone-${payerIndex}`,
      ip_address: `49.36.${payerIndex % 250}.${(payerIndex >> 8) % 250}`,
      ip_country: "IN",
    },
  });
  createLatency.add(created.timings.duration);
  if (!check(created, { "create 201": (r) => r.status === 201 })) {
    outcomes.add(1, { outcome: `create_${created.status}` });
    return;
  }
  const id = created.json("payment_id");
  const confirmed = signed(merchant, `/v1/payment_intents/${id}/confirm`, {});
  confirmLatency.add(confirmed.timings.duration);
  paymentE2E.add(Date.now() - started);
  check(confirmed, { "confirm 200": (r) => r.status === 200 });
  outcomes.add(1, {
    outcome: confirmed.status === 200 ? `status_${confirmed.json("status")}` : `confirm_${confirmed.status}`,
  });
}

export function handleSummary(data) {
  return { stdout: `K6_SUMMARY ${JSON.stringify(data)}\n` };
}
