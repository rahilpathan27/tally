"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { z } from "zod";
import { Badge, Button, Card, Dialog, Empty, ErrorNotice, Field, Input, Loading, PageHeader, Select, StatusBadge, Table, Td, when } from "@/components/ui";
import { api } from "@/lib/api";
import { useIdempotencyKey } from "@/lib/idempotency";
import { AnyRecord, ApiKey, CreatedKey, WebhookEndpoint } from "@/lib/schemas";

const SCOPES = ["payments:read", "payments:write", "settlements:read", "disputes:read", "disputes:write", "webhooks:read", "webhooks:write"];

export default function DevelopersPage() {
  return (
    <>
      <PageHeader title="Developers" subtitle="API keys sign requests with HMAC-SHA256; webhooks are signed with a per-endpoint secret." />
      <div className="flex flex-col gap-6">
        <ApiKeys />
        <Webhooks />
      </div>
    </>
  );
}

function SecretOnce({ secret, onDone }: { secret: string; onDone: () => void }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex flex-col gap-3">
      <p role="status" className="text-sm">Copy this secret now. It is shown once and cannot be retrieved later.</p>
      <code className="break-all rounded bg-zinc-100 p-2 text-xs dark:bg-zinc-800" data-testid="one-time-secret">{secret}</code>
      <div className="flex gap-2">
        <Button variant="secondary" onClick={async () => { await navigator.clipboard.writeText(secret); setCopied(true); }}>{copied ? "Copied" : "Copy"}</Button>
        <Button onClick={onDone}>I have stored it</Button>
      </div>
    </div>
  );
}

function ApiKeys() {
  const queryClient = useQueryClient();
  const keys = useQuery({ queryKey: ["api-keys"], queryFn: () => api("/bff/v1/api-keys", z.array(ApiKey)) });
  const [open, setOpen] = useState(false);
  const [scopes, setScopes] = useState<string[]>(["payments:read", "payments:write"]);
  const [mode, setMode] = useState("test");
  const [secret, setSecret] = useState<string | null>(null);
  const idempotency = useIdempotencyKey();
  const create = useMutation({
    mutationFn: () => api("/bff/v1/api-keys", CreatedKey, { method: "POST", body: { scopes, mode }, idempotencyKey: idempotency.key() }),
    onSuccess: (created) => { idempotency.rotate(); setSecret(`${created.key_id}:${created.secret}`); void queryClient.invalidateQueries({ queryKey: ["api-keys"] }); },
  });
  const act = useMutation({
    mutationFn: ({ keyId, action }: { keyId: string; action: "rotate" | "revoke" }) =>
      api(`/bff/v1/api-keys/${keyId}/${action}`, AnyRecord, { method: "POST", idempotencyKey: crypto.randomUUID() }),
    onSuccess: (result) => {
      if (typeof result.secret === "string") setSecret(`${String(result.key_id)}:${result.secret}`);
      void queryClient.invalidateQueries({ queryKey: ["api-keys"] });
    },
  });
  function submit(event: FormEvent) { event.preventDefault(); create.mutate(); }
  return (
    <Card title="API keys" actions={<Button onClick={() => { setSecret(null); create.reset(); setOpen(true); }}>Create key</Button>}>
      {keys.isPending ? <Loading /> : keys.isError ? <ErrorNotice error={keys.error} /> : keys.data.length === 0 ? <Empty>No keys.</Empty> : (
        <Table caption="API keys" head={["Key ID", "Mode", "Scopes", "Expires", "Status", ""]}>
          {keys.data.map((k) => (
            <tr key={k.key_id}>
              <Td className="font-mono text-xs">{k.key_id}</Td>
              <Td><Badge>{k.mode}</Badge></Td>
              <Td className="text-xs">{k.scopes.join(", ")}</Td>
              <Td>{when(k.expires_at)}</Td>
              <Td>{k.revoked_at ? <StatusBadge status="revoked" /> : <StatusBadge status="active" />}</Td>
              <Td>{k.revoked_at ? null : (
                <span className="flex gap-2">
                  <Button variant="secondary" onClick={() => act.mutate({ keyId: k.key_id, action: "rotate" })}>Rotate</Button>
                  <Button variant="danger" onClick={() => { if (confirm(`Revoke ${k.key_id}? Requests signed with it will fail immediately.`)) act.mutate({ keyId: k.key_id, action: "revoke" }); }}>Revoke</Button>
                </span>
              )}</Td>
            </tr>
          ))}
        </Table>
      )}
      {act.isError ? <ErrorNotice error={act.error} /> : null}
      {secret && !open ? <div className="mt-4"><SecretOnce secret={secret} onDone={() => setSecret(null)} /></div> : null}
      <Dialog open={open} title="Create API key" onClose={() => setOpen(false)}>
        {secret ? <SecretOnce secret={secret} onDone={() => { setSecret(null); setOpen(false); }} /> : (
          <form onSubmit={submit} className="flex flex-col gap-4">
            <fieldset>
              <legend className="mb-2 text-sm font-medium">Scopes</legend>
              <div className="grid grid-cols-2 gap-2">
                {SCOPES.map((scope) => (
                  <label key={scope} className="flex items-center gap-2 text-sm">
                    <input type="checkbox" checked={scopes.includes(scope)} onChange={(e) => setScopes(e.target.checked ? [...scopes, scope] : scopes.filter((s) => s !== scope))} />
                    {scope}
                  </label>
                ))}
              </div>
            </fieldset>
            <Field label="Mode">{(p) => <Select {...p} value={mode} onChange={(e) => setMode(e.target.value)}><option value="test">Test</option><option value="live">Live (simulated)</option></Select>}</Field>
            {create.isError ? <ErrorNotice error={create.error} /> : null}
            <Button type="submit" busy={create.isPending} disabled={scopes.length === 0}>Create</Button>
          </form>
        )}
      </Dialog>
    </Card>
  );
}

function Webhooks() {
  const queryClient = useQueryClient();
  const endpoints = useQuery({ queryKey: ["webhooks"], queryFn: () => api("/bff/v1/webhooks", z.array(WebhookEndpoint)), refetchInterval: 10_000 });
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState<string | null>(null);
  const idempotency = useIdempotencyKey();
  const create = useMutation({
    mutationFn: () => api("/bff/v1/webhooks", AnyRecord, { method: "POST", body: { url, enabled_events: ["payment_intent.*", "refund.*", "settlement.*", "payout.*", "dispute.*"] }, idempotencyKey: idempotency.key() }),
    onSuccess: (result) => { idempotency.rotate(); setSecret(String(result.secret)); setUrl(""); void queryClient.invalidateQueries({ queryKey: ["webhooks"] }); },
  });
  const redeliver = useMutation({
    mutationFn: (deliveryId: string) => api(`/bff/v1/webhooks/deliveries/${deliveryId}/redeliver`, AnyRecord, { method: "POST", idempotencyKey: crypto.randomUUID() }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["webhooks"] }),
  });
  return (
    <Card title="Webhooks">
      <form onSubmit={(e) => { e.preventDefault(); create.mutate(); }} className="mb-4 flex flex-wrap items-end gap-3">
        <div className="min-w-72 flex-1">
          <Field label="Endpoint URL" hint="HTTPS on port 443 or 8443 to a public address.">{(p) => <Input {...p} type="url" required value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://example.com/tally/webhooks" />}</Field>
        </div>
        <Button type="submit" busy={create.isPending}>Add endpoint</Button>
      </form>
      {create.isError ? <ErrorNotice error={create.error} /> : null}
      {secret ? <div className="mb-4"><SecretOnce secret={secret} onDone={() => setSecret(null)} /></div> : null}
      {endpoints.isPending ? <Loading /> : endpoints.isError ? <ErrorNotice error={endpoints.error} /> : endpoints.data.length === 0 ? <Empty>No endpoints.</Empty> : endpoints.data.map((endpoint) => (
        <div key={endpoint.endpoint_id} className="mb-6">
          <p className="mb-2 text-sm"><span className="font-mono">{endpoint.url}</span> <StatusBadge status={endpoint.status} /></p>
          {endpoint.deliveries.length === 0 ? <p className="text-sm text-zinc-600">No deliveries yet.</p> : (
            <Table caption={`Deliveries to ${endpoint.url}`} head={["Event", "Status", "Attempts", "Last response", "Created", ""]}>
              {endpoint.deliveries.map((d) => (
                <tr key={d.delivery_id}>
                  <Td>{d.event_type}</Td>
                  <Td><StatusBadge status={d.status} /></Td>
                  <Td>{d.attempts}</Td>
                  <Td className="text-xs">{d.last_status_code ?? d.last_error ?? "—"}</Td>
                  <Td>{when(d.created_at)}</Td>
                  <Td><Button variant="ghost" onClick={() => redeliver.mutate(d.delivery_id)}>Redeliver</Button></Td>
                </tr>
              ))}
            </Table>
          )}
        </div>
      ))}
    </Card>
  );
}
