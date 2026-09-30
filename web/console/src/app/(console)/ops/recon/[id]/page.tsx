"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { type FormEvent, useState } from "react";
import { hasAny, useSession } from "@/components/providers";
import { Button, Card, DescriptionList, ErrorNotice, Field, Input, Loading, Money, PageHeader, Select, StatusBadge, when } from "@/components/ui";
import { api } from "@/lib/api";
import { useIdempotencyKey } from "@/lib/idempotency";
import { AnyRecord, ReconBreakDetail } from "@/lib/schemas";

function Records({ title, records }: { title: string; records: Record<string, unknown>[] }) {
  return (
    <Card title={title}>
      {records.length === 0 ? <p className="text-sm text-zinc-600 dark:text-zinc-400">No record.</p> : records.map((record, i) => (
        <dl key={i} className="mb-3 grid grid-cols-2 gap-1 text-xs">
          {Object.entries(record).map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="text-zinc-600 dark:text-zinc-400">{key}</dt>
              <dd className="break-all font-mono">{key.endsWith("amount_minor") && typeof value === "number" ? <Money minor={value} /> : String(value ?? "—")}</dd>
            </div>
          ))}
        </dl>
      ))}
    </Card>
  );
}

export default function BreakDetail() {
  const { id } = useParams<{ id: string }>();
  const session = useSession();
  const queryClient = useQueryClient();
  const detail = useQuery({ queryKey: ["break", id], queryFn: () => api(`/bff/v1/ops/recon/breaks/${id}`, ReconBreakDetail) });
  const [action, setAction] = useState("book_to_suspense");
  const [note, setNote] = useState("");
  const idempotency = useIdempotencyKey();
  const propose = useMutation({
    mutationFn: () => api(`/bff/v1/ops/recon/breaks/${id}/adjustments`, AnyRecord, { method: "POST", body: { action, note }, idempotencyKey: idempotency.key() }),
    onSuccess: () => { idempotency.rotate(); setNote(""); void queryClient.invalidateQueries({ queryKey: ["break", id] }); },
  });
  const comment = useMutation({
    mutationFn: (text: string) => api(`/bff/v1/ops/recon/breaks/${id}/comments`, AnyRecord, { method: "POST", body: { note: text }, idempotencyKey: crypto.randomUUID() }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["break", id] }),
  });
  if (detail.isPending) return <Loading />;
  if (detail.isError) return <ErrorNotice error={detail.error} />;
  const b = detail.data;
  const canAct = hasAny(session.data?.roles, ["ops_analyst"]);
  function submit(event: FormEvent) { event.preventDefault(); propose.mutate(); }
  return (
    <>
      <PageHeader title={b.break_type.replaceAll("_", " ")} subtitle={<><StatusBadge status={b.status} /> <span className="ml-2 font-mono text-xs">{b.break_id}</span></>} />
      <Card className="mb-4">
        <DescriptionList items={[
          ["Amount", <Money key="a" minor={b.amount_minor} />],
          ["Business date / source", `${b.business_date} · ${b.source}`],
          ["Detail", b.detail],
          ["Suggested action", b.suggested_action],
          ["SLA due", when(b.sla_due_at)],
        ]} />
      </Card>
      <h2 className="mb-2 text-lg font-semibold">Three-way view</h2>
      <div className="mb-6 grid gap-4 lg:grid-cols-3">
        <Records title="Ledger (nostro postings)" records={b.evidence.ledger} />
        <Records title="Switch log" records={b.evidence.switch ? [b.evidence.switch] : []} />
        <Records title="Bank statement" records={b.evidence.bank} />
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        {canAct && b.status === "open" ? (
          <Card title="Propose resolution (requires an approver)">
            <form onSubmit={submit} className="flex flex-col gap-3">
              <Field label="Adjustment">{(p) => (
                <Select {...p} value={action} onChange={(e) => setAction(e.target.value)}>
                  {Object.entries(b.adjustments_available).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                </Select>
              )}</Field>
              <Field label="Justification">{(p) => <Input {...p} required minLength={1} maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />}</Field>
              {propose.isError ? <ErrorNotice error={propose.error} /> : null}
              <Button type="submit" busy={propose.isPending}>Submit for approval</Button>
            </form>
          </Card>
        ) : null}
        <Card title="Activity">
          <ol className="mb-3 flex flex-col gap-2 text-sm">
            {b.actions.map((a, i) => (
              <li key={i}><span className="font-medium">{a.actor}</span> {a.action.replaceAll("_", " ")} <span className="text-xs text-zinc-600 dark:text-zinc-400">{when(a.created_at)}</span>{a.note ? <p className="text-xs">{a.note}</p> : null}</li>
            ))}
          </ol>
          {canAct ? (
            <form onSubmit={(e) => { e.preventDefault(); const form = e.currentTarget; const text = new FormData(form).get("note"); if (text) { comment.mutate(String(text)); form.reset(); } }} className="flex gap-2">
              <label htmlFor="comment" className="sr-only">Comment</label>
              <Input id="comment" name="note" placeholder="Add a comment" className="flex-1" />
              <Button type="submit" variant="secondary" busy={comment.isPending}>Comment</Button>
            </form>
          ) : null}
        </Card>
      </div>
    </>
  );
}
