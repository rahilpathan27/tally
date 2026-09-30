"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter, useSearchParams } from "next/navigation";
import { type FormEvent, Suspense, useState } from "react";
import { Button, Card, ErrorNotice, Field, Input } from "@/components/ui";
import { api } from "@/lib/api";
import { LoginResult } from "@/lib/schemas";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mfaToken, setMfaToken] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  function land(roles: string[], merchant: string | null | undefined) {
    const next = params.get("next");
    queryClient.removeQueries();
    router.replace(next && next.startsWith("/") && !next.startsWith("//") ? next : merchant ? "/merchant" : roles.includes("risk_analyst") ? "/ops/risk" : "/ops");
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = mfaToken
        ? await api("/auth/mfa", LoginResult, { method: "POST", body: { mfa_token: mfaToken, code } })
        : await api("/auth/login", LoginResult, { method: "POST", body: { email, password } });
      if ("mfa_required" in result) setMfaToken(result.mfa_token);
      else land(result.roles, result.merchant_id);
    } catch (caught) {
      setError(caught);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title={mfaToken ? "Two-factor code" : "Sign in"}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        {mfaToken ? (
          <Field label="Authenticator code" hint="Six digits from your authenticator app.">
            {(props) => (
              <Input {...props} inputMode="numeric" autoComplete="one-time-code" pattern="[0-9]{6}" required value={code} onChange={(e) => setCode(e.target.value)} />
            )}
          </Field>
        ) : (
          <>
            <Field label="Email">
              {(props) => <Input {...props} type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />}
            </Field>
            <Field label="Password">
              {(props) => <Input {...props} type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />}
            </Field>
          </>
        )}
        {error ? <ErrorNotice error={error} /> : null}
        <Button type="submit" busy={busy}>
          {mfaToken ? "Verify" : "Sign in"}
        </Button>
      </form>
    </Card>
  );
}

export default function LoginPage() {
  return (
    <main id="main" className="mx-auto flex max-w-sm flex-col gap-6 px-6 py-20">
      <h1 className="text-2xl font-semibold">Tally console</h1>
      <Suspense>
        <LoginForm />
      </Suspense>
      <p className="text-xs text-zinc-600 dark:text-zinc-400">
        Local demo users are listed in <code>.data/dev-stack.json</code> after <code>make dev</code>.
      </p>
    </main>
  );
}
