"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Button, Card, ErrorNotice, Field, PageHeader, Select } from "@/components/ui";
import { api } from "@/lib/api";
import { AnyRecord } from "@/lib/schemas";

const BANK_MODES = ["approve", "decline", "credit_failure", "timeout", "late_success", "status_unknown", "http_500", "reverse_timeout"];

export default function ChaosPanel() {
  const [banks, setBanks] = useState<Record<string, string>>({ "bank-a": "approve", "bank-b": "approve", "bank-c": "approve" });
  const [network, setNetwork] = useState("approve");
  const [psp, setPsp] = useState("approve");
  const apply = useMutation({
    mutationFn: () => api("/bff/v1/ops/chaos", AnyRecord, {
      method: "POST",
      body: { bank_modes: Object.fromEntries(Object.entries(banks).filter(([, m]) => m !== "approve")), card_network_mode: network, payer_psp_mode: psp },
      idempotencyKey: crypto.randomUUID(),
    }),
  });
  return (
    <>
      <PageHeader title="Chaos control" subtitle="Demo and staging only. Changes simulator behaviour for everyone; every change is audited." />
      <Card>
        <form className="grid gap-4 sm:grid-cols-3" onSubmit={(e) => { e.preventDefault(); apply.mutate(); }}>
          {Object.keys(banks).map((bank) => (
            <Field key={bank} label={`Bank ${bank}`}>{(p) => (
              <Select {...p} value={banks[bank]} onChange={(e) => setBanks({ ...banks, [bank]: e.target.value })}>{BANK_MODES.map((m) => <option key={m} value={m}>{m.replaceAll("_", " ")}</option>)}</Select>
            )}</Field>
          ))}
          <Field label="Card network">{(p) => <Select {...p} value={network} onChange={(e) => setNetwork(e.target.value)}>{["approve", "decline", "http_500", "timeout", "late_success"].map((m) => <option key={m}>{m}</option>)}</Select>}</Field>
          <Field label="Payer PSP">{(p) => <Select {...p} value={psp} onChange={(e) => setPsp(e.target.value)}>{["approve", "decline", "http_500"].map((m) => <option key={m}>{m}</option>)}</Select>}</Field>
          <div className="flex items-end"><Button type="submit" busy={apply.isPending}>Apply</Button></div>
        </form>
        {apply.isError ? <div className="mt-3"><ErrorNotice error={apply.error} /></div> : null}
        {apply.isSuccess ? <p role="status" className="mt-3 text-sm">Simulators updated.</p> : null}
      </Card>
    </>
  );
}
