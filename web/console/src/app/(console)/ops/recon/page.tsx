"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { z } from "zod";
import { Card, Empty, ErrorNotice, Field, Loading, Money, PageHeader, Select, StatusBadge, Table, Td, when } from "@/components/ui";
import { api } from "@/lib/api";
import { ReconBreak, ReconRun } from "@/lib/schemas";

function age(iso: string): string {
  const hours = Math.floor((Date.now() - new Date(iso).getTime()) / 3_600_000);
  return hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
}

export default function ReconWorkbench() {
  const [status, setStatus] = useState("open");
  const [type, setType] = useState("");
  const runs = useQuery({ queryKey: ["recon-runs"], queryFn: () => api("/bff/v1/ops/recon/runs?limit=10", z.array(ReconRun)) });
  const breaks = useQuery({
    queryKey: ["recon-breaks", status, type],
    queryFn: () => api(`/bff/v1/ops/recon/breaks?${new URLSearchParams({ status, ...(type ? { break_type: type } : {}), limit: "200" })}`, z.array(ReconBreak)),
  });
  return (
    <>
      <PageHeader title="Reconciliation" subtitle="Ledger nostro vs switch log vs bank statement" />
      <Card title="Recent runs" className="mb-6">
        {runs.isPending ? <Loading /> : runs.isError ? <ErrorNotice error={runs.error} /> : runs.data.length === 0 ? <Empty>No runs yet.</Empty> : (
          <Table caption="Reconciliation runs" head={["Date", "Source", "Bank lines", "Matched", "Match rate", "Breaks", "Open value", "Finished"]}>
            {runs.data.map((run) => (
              <tr key={run.run_id}>
                <Td>{run.business_date}</Td><Td>{run.source}</Td><Td>{run.bank_lines}</Td><Td>{run.matched_lines}</Td>
                <Td>{run.bank_lines ? `${((run.matched_lines * 1000) / run.bank_lines / 10).toFixed(1)}%` : "—"}</Td>
                <Td className="text-xs">{Object.entries(run.breaks_by_type).map(([k, v]) => `${k.replaceAll("_", " ")} ${v}`).join(", ") || "none"}</Td>
                <Td><Money minor={run.open_break_value_minor} /></Td><Td>{when(run.finished_at)}</Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
      <Card title="Break queue" actions={
        <div className="flex gap-3">
          <Field label="Status">{(p) => (
            <Select {...p} value={status} onChange={(e) => setStatus(e.target.value)}>
              {["open", "pending_approval", "resolved", "auto_resolved"].map((s) => <option key={s} value={s}>{s.replaceAll("_", " ")}</option>)}
            </Select>
          )}</Field>
          <Field label="Type">{(p) => (
            <Select {...p} value={type} onChange={(e) => setType(e.target.value)}>
              <option value="">Any</option>
              {["missing_at_bank", "missing_internally", "amount_mismatch", "duplicate", "status_mismatch", "timing_difference", "fee_tax_mismatch", "unknown"].map((t) => <option key={t} value={t}>{t.replaceAll("_", " ")}</option>)}
            </Select>
          )}</Field>
        </div>
      }>
        {breaks.isPending ? <Loading /> : breaks.isError ? <ErrorNotice error={breaks.error} /> : breaks.data.length === 0 ? <Empty>No breaks in this view.</Empty> : (
          <Table caption="Reconciliation breaks" head={["Type", "Reference", "Amount", "Status", "Age", "SLA due", "Detail"]}>
            {breaks.data.map((b) => (
              <tr key={b.break_id}>
                <Td><Link className="text-indigo-700 underline dark:text-indigo-300" href={`/ops/recon/${b.break_id}`}>{b.break_type.replaceAll("_", " ")}</Link></Td>
                <Td className="font-mono text-xs">{b.reference ?? b.bank_reference}</Td>
                <Td><Money minor={b.amount_minor} /></Td><Td><StatusBadge status={b.status} /></Td>
                <Td>{age(b.created_at)}</Td>
                <Td className={new Date(b.sla_due_at) < new Date() ? "text-red-700" : ""}>{when(b.sla_due_at)}</Td>
                <Td className="max-w-xs text-xs">{b.detail}</Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
