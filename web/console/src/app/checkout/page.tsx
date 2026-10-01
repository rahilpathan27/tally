"use client";

import QRCode from "qrcode";
import Link from "next/link";
import { type FormEvent, useEffect, useRef, useState } from "react";
import { Button, Card, ErrorNotice, Field, Input, Money, Select, StatusBadge } from "@/components/ui";
import { formatMoney, parseMoneyInput } from "@/lib/money";

const VAULT = process.env.NEXT_PUBLIC_VAULT_URL ?? "http://127.0.0.1:8002";
const PUBLISHABLE_KEY = process.env.NEXT_PUBLIC_TALLY_PUBLISHABLE_KEY ?? "pk_test_tally_demo";
const PAYERS = ["asha@bank-a", "ravi@bank-c", "meera@bank-a", "payer@bank-a"];
// The demo rule set allow-lists asha@bank-a (a known, trusted customer), so the risk engine
// skips review and one-time codes for it; block rules still apply. Use another payer to see
// risk holds and step-up.
const PAYER_NOTES: Record<string, string> = { "asha@bank-a": " (trusted: skips risk review)" };

type Result = { payment_id?: string; status?: string; next_action?: string | null; challenge_id?: string | null; reason_codes?: string[] | null; detail?: { code?: string; message?: string } };

export default function Checkout() {
  const [amount, setAmount] = useState("499.00");
  const [method, setMethod] = useState<"upi" | "card">("upi");
  const [vpa, setVpa] = useState(PAYERS[0]);
  const [pan, setPan] = useState("4242 4242 4242 4242");
  const [expiry, setExpiry] = useState("12/30");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const [otp, setOtp] = useState("");
  const [qr, setQr] = useState<{ uri: string; data: string } | null>(null);
  const actionKey = useRef<string>(crypto.randomUUID());
  const device = useRef<string>("");
  useEffect(() => {
    try {
      device.current = localStorage.getItem("tally-demo-device") ?? crypto.randomUUID();
      localStorage.setItem("tally-demo-device", device.current);
    } catch {
      device.current = crypto.randomUUID();
    }
  }, []);

  const parsed = parseMoneyInput(amount);
  // A UPI intent URI a payer's app would scan (payee is the demo store's VPA).
  const qrUri =
    method === "upi" && parsed.ok
      ? `upi://pay?pa=merchant@bank-b&pn=Demo%20Store&am=${formatMoney(parsed.minor).replace(/[^0-9.]/g, "")}&cu=INR`
      : null;
  useEffect(() => {
    if (!qrUri) return;
    let live = true;
    QRCode.toDataURL(qrUri, { margin: 1, width: 180 })
      .then((data) => { if (live) setQr({ uri: qrUri, data }); })
      .catch(() => undefined);
    return () => { live = false; };
  }, [qrUri]);
  const qrData = qr && qr.uri === qrUri ? qr.data : null;

  // Pending outcomes resolve in the background; poll until they settle.
  useEffect(() => {
    if (!result?.payment_id || !["pending_unknown", "authorizing", "risk_review", "capturing"].includes(result.status ?? "")) return;
    if (result.next_action === "step_up") return;
    const timer = setInterval(async () => {
      const response = await fetch(`/api/demo/payments/${result.payment_id}`);
      if (response.ok) {
        const latest = await response.json();
        if (latest.status !== result.status) setResult({ ...result, status: latest.status, next_action: null });
      }
    }, 2_000);
    return () => clearInterval(timer);
  }, [result]);

  async function pay(event: FormEvent) {
    event.preventDefault();
    if (!parsed.ok) return setError(parsed.error);
    setBusy(true);
    setError(null);
    try {
      let token: string | undefined;
      if (method === "card") {
        const [month, year] = expiry.split("/").map((part) => Number(part.trim()));
        // The card goes straight to the vault; the store's server only ever sees the token.
        const tokenized = await fetch(`${VAULT}/public/v1/tokens`, {
          method: "POST",
          headers: { "content-type": "application/json", "x-publishable-key": PUBLISHABLE_KEY },
          body: JSON.stringify({ pan: pan.replace(/\s/g, ""), expiry_month: month, expiry_year: year < 100 ? 2000 + year : year }),
        });
        const body = await tokenized.json();
        if (!tokenized.ok) throw new Error(body?.detail?.message ?? "Card was not accepted.");
        token = body.payment_method_token;
      }
      const response = await fetch("/api/demo/payments", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ amount_minor: parsed.minor, method, payment_method_token: token, payer_vpa: method === "upi" ? vpa : undefined, device_id: device.current, idempotency_key: actionKey.current }),
      });
      const body: Result = await response.json();
      if (!response.ok) throw new Error(body.detail?.message ?? "Payment could not be started.");
      setResult(body);
      actionKey.current = crypto.randomUUID();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Payment failed.");
    } finally {
      setBusy(false);
    }
  }

  async function verify(event: FormEvent) {
    event.preventDefault();
    if (!result?.payment_id || !result.challenge_id) return;
    setBusy(true);
    const response = await fetch(`/api/demo/payments/${result.payment_id}/step-up`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ challenge_id: result.challenge_id, code: otp, idempotency_key: crypto.randomUUID() }),
    });
    const body: Result = await response.json();
    setBusy(false);
    if (!response.ok) return setError(body.detail?.message ?? "That code did not work.");
    setError(null);
    setResult({ ...result, ...body, next_action: null });
  }

  return (
    <main id="main" className="mx-auto max-w-lg px-6 py-12">
      <p className="mb-2 text-sm text-zinc-600 dark:text-zinc-400">Demo Store · test mode</p>
      <h1 className="mb-6 text-2xl font-semibold">Checkout</h1>
      {result ? (
        <Card title="Payment">
          <div className="flex flex-col gap-3" aria-live="polite">
            <p className="text-sm">Payment <span className="font-mono">{result.payment_id}</span></p>
            <p className="text-lg" data-testid="checkout-status">{result.status === "risk_review" && result.next_action === "step_up" ? <span>Verification needed</span> : <StatusBadge status={result.status ?? "unknown"} />}</p>
            {result.status === "succeeded" ? <p role="status">Paid <Money minor={parsed.ok ? parsed.minor : 0} />. Thank you!</p> : null}
            {result.status === "failed" ? <p role="status">The payment was declined{result.reason_codes?.length ? ` (${result.reason_codes.join(", ")})` : ""}. You have not been charged.</p> : null}
            {result.status === "pending_unknown" ? <p role="status">Your bank has not confirmed yet. We are checking; do not pay again.</p> : null}
            {result.status === "risk_review" && result.next_action === "await_review" ? <p role="status">This payment is being reviewed. We will update this page.</p> : null}
            {result.next_action === "step_up" ? (
              <form onSubmit={verify} className="flex flex-col gap-3">
                <p className="text-sm">Enter the one-time code sent to your phone. <Link className="underline" href={`/payer?challenge=${result.challenge_id}`} target="_blank">Open payer phone</Link></p>
                <Field label="One-time code">{(p) => <Input {...p} inputMode="numeric" pattern="[0-9]{6}" required value={otp} onChange={(e) => setOtp(e.target.value)} />}</Field>
                <Button type="submit" busy={busy}>Verify</Button>
              </form>
            ) : null}
            {error ? <ErrorNotice error={new Error(error)} /> : null}
            <Button variant="secondary" onClick={() => { setResult(null); setOtp(""); setError(null); }}>New payment</Button>
          </div>
        </Card>
      ) : (
        <Card>
          <form onSubmit={pay} className="flex flex-col gap-4">
            <Field label="Amount (₹)" error={!parsed.ok && amount ? parsed.error : null}>{(p) => <Input {...p} inputMode="decimal" value={amount} onChange={(e) => setAmount(e.target.value)} />}</Field>
            <fieldset className="flex gap-4">
              <legend className="mb-2 text-sm font-medium">Pay with</legend>
              {(["upi", "card"] as const).map((m) => (
                <label key={m} className="flex items-center gap-2 text-sm"><input type="radio" name="method" checked={method === m} onChange={() => setMethod(m)} />{m === "upi" ? "UPI" : "Card"}</label>
              ))}
            </fieldset>
            {method === "upi" ? (
              <>
                <Field label="Your UPI ID">{(p) => <Select {...p} value={vpa} onChange={(e) => setVpa(e.target.value)}>{PAYERS.map((v) => <option key={v} value={v}>{v}{PAYER_NOTES[v] ?? ""}</option>)}</Select>}</Field>
                {qrData ? <figure className="flex flex-col items-center gap-1">
                  {/* eslint-disable-next-line @next/next/no-img-element -- generated data URI, nothing to optimise */}
                  <img src={qrData} alt="UPI QR code for this payment" width={180} height={180} /><figcaption className="text-xs text-zinc-600">Or scan with a UPI app (simulated)</figcaption></figure> : null}
              </>
            ) : (
              <>
                <Field label="Card number" hint="Published test cards only, e.g. 4242 4242 4242 4242.">{(p) => <Input {...p} inputMode="numeric" autoComplete="cc-number" value={pan} onChange={(e) => setPan(e.target.value)} />}</Field>
                <Field label="Expiry (MM/YY)">{(p) => <Input {...p} autoComplete="cc-exp" value={expiry} onChange={(e) => setExpiry(e.target.value)} />}</Field>
                <p className="text-xs text-zinc-600 dark:text-zinc-400">No CVV is collected: the simulation never stores or sends card security codes.</p>
              </>
            )}
            {error ? <ErrorNotice error={new Error(error)} /> : null}
            <Button type="submit" busy={busy} disabled={!parsed.ok}>Pay {parsed.ok ? formatMoney(parsed.minor) : ""}</Button>
          </form>
        </Card>
      )}
    </main>
  );
}
