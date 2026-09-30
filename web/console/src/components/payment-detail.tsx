"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { Button, Card, DescriptionList, Dialog, ErrorNotice, Field, Input, Loading, Money, PageHeader, StatusBadge, Table, Td, when } from "@/components/ui";
import { api } from "@/lib/api";
import { useIdempotencyKey } from "@/lib/idempotency";
import { formatMoney, parseMoneyInput } from "@/lib/money";
import { PaymentDetail, RefundResult } from "@/lib/schemas";

export function PaymentDetailView({ paymentId, source, canRefund }: { paymentId: string; source: "merchant" | "ops"; canRefund: boolean }) {
  const path = source === "merchant" ? `/bff/v1/payments/${paymentId}` : `/bff/v1/ops/payments/${paymentId}`;
  const query = useQuery({ queryKey: ["payment", paymentId], queryFn: () => api(path, PaymentDetail) });
  const [open, setOpen] = useState(false);
  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorNotice error={query.error} />;
  const p = query.data;
  const refunded = p.refunds.filter((r) => !["failed", "cancelled"].includes(r.status)).reduce((s, r) => s + r.amount_minor, 0);
  const refundable = p.status === "succeeded" ? p.amount_minor - refunded : 0;
  return (
    <>
      <PageHeader
        title={formatMoney(p.amount_minor, p.currency.trim())}
        subtitle={<span className="font-mono">{p.payment_id}</span>}
        actions={canRefund && refundable > 0 ? <Button onClick={() => setOpen(true)}>Refund</Button> : null}
      />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Details">
          <DescriptionList items={[
            ["Status", <StatusBadge key="s" status={p.status} />],
            ["Method", p.payment_method_type === "upi" ? `UPI ${p.payer_vpa} → ${p.payee_vpa}` : "Card (token)"],
            ["Created", when(p.created_at)],
            ["Refundable", <Money key="r" minor={refundable} />],
            ["Risk", p.risk_outcome ? `${String(p.risk_outcome.decision)} (${String(p.risk_outcome.source)})` : "—"],
            ["Risk reasons", p.risk_outcome && Array.isArray(p.risk_outcome.reason_codes) ? (p.risk_outcome.reason_codes as string[]).join(", ") || "—" : "—"],
          ]} />
        </Card>
        <Card title="Status timeline">
          <ol className="relative border-l border-zinc-200 pl-4 dark:border-zinc-700" aria-label="Status timeline">
            {p.timeline.map((t, i) => (
              <li key={i} className="mb-3">
                <p className="text-sm"><StatusBadge status={t.to_state} /> {t.accepted ? null : <span className="text-xs text-red-700">rejected attempt</span>}</p>
                <p className="text-xs text-zinc-600 dark:text-zinc-400">{when(t.occurred_at)} · {t.actor}</p>
                <p className="text-xs">{t.reason}</p>
              </li>
            ))}
          </ol>
        </Card>
        <Card title="Ledger entries" className="lg:col-span-2">
          {p.ledger.length === 0 ? <p className="text-sm text-zinc-600">No ledger effect (declined, reversed or pending).</p> : p.ledger.map((entry) => (
            <div key={`${entry.kind}-${entry.idempotency_key}`} className="mb-4">
              <p className="mb-1 text-sm font-medium">{entry.kind} {entry.entry_id ? `#${entry.entry_id}` : entry.hold_id ? `hold #${entry.hold_id}` : ""} {entry.status ? <StatusBadge status={entry.status} /> : null}</p>
              <p className="mb-2 font-mono text-xs text-zinc-600 dark:text-zinc-400">{entry.idempotency_key}</p>
              {entry.postings ? (
                <Table caption={`Postings of ${entry.kind}`} head={["Account", "Debit", "Credit"]}>
                  {entry.postings.map((posting, i) => (
                    <tr key={i}>
                      <Td className="font-mono text-xs">{posting.account_id}</Td>
                      <Td>{posting.direction === "debit" ? <Money minor={posting.amount_minor} /> : ""}</Td>
                      <Td>{posting.direction === "credit" ? <Money minor={posting.amount_minor} /> : ""}</Td>
                    </tr>
                  ))}
                </Table>
              ) : null}
            </div>
          ))}
        </Card>
        <Card title="Refunds" className="lg:col-span-2">
          {p.refunds.length === 0 ? <p className="text-sm text-zinc-600">No refunds.</p> : (
            <Table caption="Refunds" head={["Refund", "Amount", "Status", "Created"]}>
              {p.refunds.map((r) => (
                <tr key={r.refund_id}><Td className="font-mono text-xs">{r.refund_id}</Td><Td><Money minor={r.amount_minor} /></Td><Td><StatusBadge status={r.status} /></Td><Td>{when(r.created_at)}</Td></tr>
              ))}
            </Table>
          )}
        </Card>
      </div>
      <RefundDialog open={open} onClose={() => setOpen(false)} paymentId={p.payment_id} refundable={refundable} />
    </>
  );
}

function RefundDialog({ open, onClose, paymentId, refundable }: { open: boolean; onClose: () => void; paymentId: string; refundable: number }) {
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const idempotency = useIdempotencyKey();
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: (minor: number) =>
      api("/bff/v1/refunds", RefundResult, {
        method: "POST",
        body: { payment_id: paymentId, amount_minor: minor, reason: reason || null },
        idempotencyKey: idempotency.key(),
      }),
    onSuccess: () => {
      idempotency.rotate();
      void queryClient.invalidateQueries({ queryKey: ["payment", paymentId] });
    },
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    const parsed = parseMoneyInput(amount);
    if (!parsed.ok) return setError(parsed.error);
    if (parsed.minor > refundable) return setError(`At most ${formatMoney(refundable)} can be refunded.`);
    setError(null);
    mutation.mutate(parsed.minor);
  }

  return (
    <Dialog open={open} title="Refund payment" onClose={() => { mutation.reset(); onClose(); }}>
      {mutation.isSuccess ? (
        <div className="flex flex-col gap-3">
          <p role="status" className="text-sm">
            {mutation.data.status === "pending_approval"
              ? "This refund is above your approval threshold and is waiting for an approver."
              : `Refund created (${mutation.data.status}).`}
          </p>
          <Button onClick={() => { mutation.reset(); setAmount(""); onClose(); }}>Done</Button>
        </div>
      ) : (
        <form onSubmit={submit} className="flex flex-col gap-4">
          <Field label="Amount (₹)" hint={`Up to ${formatMoney(refundable)}`} error={error}>
            {(p) => <Input {...p} inputMode="decimal" required value={amount} onChange={(e) => setAmount(e.target.value)} />}
          </Field>
          <Field label="Reason (optional)">{(p) => <Input {...p} maxLength={200} value={reason} onChange={(e) => setReason(e.target.value)} />}</Field>
          {mutation.isError ? <ErrorNotice error={mutation.error} /> : null}
          <Button type="submit" busy={mutation.isPending}>Refund</Button>
        </form>
      )}
    </Dialog>
  );
}
