"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";
import { z } from "zod";
import { hasAny, useSession } from "@/components/providers";
import { Button, Card, Dialog, Empty, ErrorNotice, Field, Input, Loading, Money, PageHeader, StatusBadge, Table, Td, shortId, when } from "@/components/ui";
import { api } from "@/lib/api";
import { useIdempotencyKey } from "@/lib/idempotency";
import { AnyRecord, Dispute } from "@/lib/schemas";

const MAX_BYTES = 1_000_000;

export default function DisputesPage() {
  const session = useSession();
  const canRespond = hasAny(session.data?.roles, ["merchant_admin"]);
  const query = useQuery({ queryKey: ["disputes"], queryFn: () => api("/bff/v1/disputes", z.array(Dispute)) });
  const [target, setTarget] = useState<string | null>(null);
  return (
    <>
      <PageHeader title="Disputes" subtitle="Chargebacks debit your balance when opened; winning returns the funds." />
      <Card>
        {query.isPending ? <Loading /> : query.isError ? <ErrorNotice error={query.error} /> : query.data.length === 0 ? <Empty>No disputes.</Empty> : (
          <Table caption="Disputes" head={["Dispute", "Payment", "Amount", "Reason", "Status", "Respond by", ""]}>
            {query.data.map((d) => (
              <tr key={d.dispute_id}>
                <Td className="font-mono">{shortId(d.dispute_id)}</Td>
                <Td className="font-mono">{shortId(d.payment_id)}</Td>
                <Td><Money minor={d.amount_minor} /></Td>
                <Td>{d.reason_code.replaceAll("_", " ")}</Td>
                <Td><StatusBadge status={d.status} /></Td>
                <Td>{when(d.respond_by)}</Td>
                <Td>{canRespond && d.status === "needs_response" ? <Button variant="secondary" onClick={() => setTarget(d.dispute_id)}>Submit evidence</Button> : null}</Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
      <EvidenceDialog disputeId={target} onClose={() => setTarget(null)} />
    </>
  );
}

function EvidenceDialog({ disputeId, onClose }: { disputeId: string | null; onClose: () => void }) {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState<string | null>(null);
  const idempotency = useIdempotencyKey();
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: async () => {
      let document: { document_base64: string; document_name: string } | Record<string, never> = {};
      if (file) {
        const bytes = new Uint8Array(await file.arrayBuffer());
        let binary = "";
        bytes.forEach((b) => { binary += String.fromCharCode(b); });
        document = { document_base64: btoa(binary), document_name: file.name.replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 100) };
      }
      return api(`/bff/v1/disputes/${disputeId}/evidence`, AnyRecord, {
        method: "POST", body: { text, ...document }, idempotencyKey: idempotency.key(),
      });
    },
    onSuccess: () => {
      idempotency.rotate();
      void queryClient.invalidateQueries({ queryKey: ["disputes"] });
      onClose();
    },
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (file && file.size > MAX_BYTES) return setError("Evidence files must be 1 MB or smaller.");
    setError(null);
    mutation.mutate();
  }
  return (
    <Dialog open={disputeId !== null} title="Submit evidence" onClose={onClose}>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field label="Explanation">{(p) => <textarea {...p} required maxLength={20_000} rows={5} value={text} onChange={(e) => setText(e.target.value)} className="rounded-md border border-zinc-300 p-2 text-sm dark:border-zinc-600 dark:bg-zinc-800" />}</Field>
        <Field label="Document (optional, ≤ 1 MB)" error={error}>{(p) => <Input {...p} type="file" accept=".pdf,.png,.jpg,.jpeg,.txt" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />}</Field>
        {mutation.isError ? <ErrorNotice error={mutation.error} /> : null}
        <Button type="submit" busy={mutation.isPending}>Submit</Button>
      </form>
    </Dialog>
  );
}
