"use client";

import { useSearchParams } from "next/navigation";
import { type FormEvent, Suspense, useState } from "react";
import { Button, Card, ErrorNotice, Field, Input } from "@/components/ui";

function Phone() {
  const params = useSearchParams();
  const [challenge, setChallenge] = useState(params.get("challenge") ?? "");
  const [code, setCode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function fetchCode(event: FormEvent) {
    event.preventDefault();
    const response = await fetch(`/api/payer/otp?challenge_id=${encodeURIComponent(challenge)}`);
    if (!response.ok) { setCode(null); return setError("No pending code for that challenge."); }
    setError(null);
    setCode((await response.json()).code);
  }
  return (
    <Card title="Messages">
      <form onSubmit={fetchCode} className="flex flex-col gap-3">
        <Field label="Challenge ID" hint="Shown in the checkout link.">{(p) => <Input {...p} value={challenge} onChange={(e) => setChallenge(e.target.value)} />}</Field>
        <Button type="submit">Check messages</Button>
      </form>
      {error ? <div className="mt-3"><ErrorNotice error={new Error(error)} /></div> : null}
      {code ? <p className="mt-4 text-sm" role="status">Your Tally verification code is <strong data-testid="otp" className="font-mono text-xl">{code}</strong>. Never share it.</p> : null}
    </Card>
  );
}

export default function PayerPhone() {
  return (
    <main id="main" className="mx-auto max-w-sm px-6 py-12">
      <h1 className="mb-2 text-2xl font-semibold">Payer phone (simulated)</h1>
      <p className="mb-6 text-sm text-zinc-600 dark:text-zinc-400">Stands in for the SMS or banking-app prompt a real payer would receive.</p>
      <Suspense><Phone /></Suspense>
    </main>
  );
}
