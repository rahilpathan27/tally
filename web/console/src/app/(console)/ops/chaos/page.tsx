"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Card, ErrorNotice, Field, PageHeader, Select } from "@/components/ui";
import { api } from "@/lib/api";
import { AnyRecord } from "@/lib/schemas";

const BANKS = ["bank-a", "bank-b", "bank-c"];
const BANK_MODES = ["approve", "decline", "credit_failure", "timeout", "late_success", "status_unknown", "http_500", "reverse_timeout"];
const NETWORK_MODES = ["approve", "decline", "http_500", "timeout", "late_success"];
const PSP_MODES = ["approve", "decline", "http_500"];

type Edits = { banks?: Record<string, string>; network?: string; psp?: string };

function live(data: Record<string, unknown> | undefined) {
  const section = (key: string) => (data?.[key] ?? {}) as Record<string, unknown>;
  const bankModes = (section("bank").modes ?? {}) as Record<string, string>;
  return {
    banks: Object.fromEntries(BANKS.map((b) => [b, bankModes[b] ?? "approve"])),
    network: String(section("network").mode ?? "approve"),
    psp: String(section("psp").mode ?? "approve"),
  };
}

export default function ChaosPanel() {
  const queryClient = useQueryClient();
  // The form starts from what the simulators are doing now (not defaults); unsaved edits overlay it.
  const current = useQuery({ queryKey: ["chaos"], queryFn: () => api("/bff/v1/ops/chaos", AnyRecord) });
  const [edits, setEdits] = useState<Edits>({});
  const server = live(current.data);
  const banks = { ...server.banks, ...edits.banks };
  const network = edits.network ?? server.network;
  const psp = edits.psp ?? server.psp;
  const apply = useMutation({
    mutationFn: () => api("/bff/v1/ops/chaos", AnyRecord, {
      method: "POST",
      body: { bank_modes: Object.fromEntries(Object.entries(banks).filter(([, m]) => m !== "approve")), card_network_mode: network, payer_psp_mode: psp },
      idempotencyKey: crypto.randomUUID(),
    }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["chaos"] });
      setEdits({});
    },
  });
  const degraded = [
    ...Object.entries(server.banks).filter(([, m]) => m !== "approve").map(([b, m]) => `${b}: ${m.replaceAll("_", " ")}`),
    ...(server.network !== "approve" ? [`card network: ${server.network}`] : []),
    ...(server.psp !== "approve" ? [`payer PSP: ${server.psp}`] : []),
  ];
  return (
    <>
      <PageHeader title="Chaos control" subtitle="Demo and staging only. Changes simulator behaviour for everyone; every change is audited." />
      <Card>
        <p role="status" className="mb-4 text-sm" data-testid="chaos-current">
          {current.isPending ? "Loading current simulator modes…" : degraded.length ? `Currently degraded: ${degraded.join(", ")}` : "All simulators are healthy (approve)."}
        </p>
        <form className="grid gap-4 sm:grid-cols-3" onSubmit={(e) => { e.preventDefault(); apply.mutate(); }}>
          {BANKS.map((bank) => (
            <Field key={bank} label={`Bank ${bank}`}>{(p) => (
              <Select {...p} value={banks[bank]} onChange={(e) => setEdits({ ...edits, banks: { ...edits.banks, [bank]: e.target.value } })}>{BANK_MODES.map((m) => <option key={m} value={m}>{m.replaceAll("_", " ")}</option>)}</Select>
            )}</Field>
          ))}
          <Field label="Card network">{(p) => <Select {...p} value={network} onChange={(e) => setEdits({ ...edits, network: e.target.value })}>{NETWORK_MODES.map((m) => <option key={m}>{m}</option>)}</Select>}</Field>
          <Field label="Payer PSP">{(p) => <Select {...p} value={psp} onChange={(e) => setEdits({ ...edits, psp: e.target.value })}>{PSP_MODES.map((m) => <option key={m}>{m}</option>)}</Select>}</Field>
          <div className="flex items-end"><Button type="submit" busy={apply.isPending}>Apply</Button></div>
        </form>
        {apply.isError ? <div className="mt-3"><ErrorNotice error={apply.error} /></div> : null}
        {apply.isSuccess ? <p role="status" className="mt-3 text-sm">Simulators updated. Set everything back to approve to restore normal behaviour.</p> : null}
      </Card>
    </>
  );
}
