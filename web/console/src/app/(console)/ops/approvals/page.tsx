"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { z } from "zod";
import { hasAny, useSession } from "@/components/providers";
import { Button, Card, Empty, ErrorNotice, Field, Input, Loading, PageHeader, Select, StatusBadge, when } from "@/components/ui";
import { api } from "@/lib/api";
import { AnyRecord, Approval } from "@/lib/schemas";

export default function ApprovalsInbox() {
  const session = useSession();
  const canDecide = hasAny(session.data?.roles, ["approver"]);
  const [status, setStatus] = useState("pending");
  const queryClient = useQueryClient();
  const inbox = useQuery({ queryKey: ["approvals", status], queryFn: () => api(`/bff/v1/ops/approvals?status=${status}`, z.array(Approval)), refetchInterval: 10_000 });
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const decide = useMutation({
    mutationFn: ({ id, verb }: { id: string; verb: "approve" | "reject" }) =>
      api(`/bff/v1/ops/approvals/${id}/${verb}`, AnyRecord, { method: "POST", body: { reason: reasons[id] || `${verb}d` }, idempotencyKey: `${id}:${verb}` }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["approvals"] }),
  });
  return (
    <>
      <PageHeader title="Approvals" subtitle="Dual control: the person who proposed a change cannot approve it." actions={
        <Field label="Status">{(p) => <Select {...p} value={status} onChange={(e) => setStatus(e.target.value)}>{["pending", "executed", "rejected", "failed"].map((s) => <option key={s}>{s}</option>)}</Select>}</Field>
      } />
      {inbox.isPending ? <Loading /> : inbox.isError ? <ErrorNotice error={inbox.error} /> : inbox.data.length === 0 ? <Card><Empty>No requests.</Empty></Card> : (
        <ul className="flex flex-col gap-4">
          {inbox.data.map((r) => (
            <li key={r.request_id}>
              <Card title={<>{r.action_type.replaceAll("_", " ")} <StatusBadge status={r.status} /></>}>
                <p className="text-sm">Proposed by <strong>{r.maker}</strong> · {when(r.created_at)}</p>
                <pre className="mt-2 max-h-48 overflow-auto rounded bg-zinc-100 p-2 text-xs dark:bg-zinc-800">{JSON.stringify(r.payload, null, 2)}</pre>
                {canDecide && r.status === "pending" ? (
                  r.can_decide === false ? <p className="mt-2 text-sm text-amber-800 dark:text-amber-300">You proposed this; another approver must decide.</p> : (
                    <div className="mt-3 flex flex-wrap items-end gap-2">
                      <div className="min-w-64 flex-1"><Field label="Reason">{(p) => <Input {...p} value={reasons[r.request_id] ?? ""} onChange={(e) => setReasons({ ...reasons, [r.request_id]: e.target.value })} />}</Field></div>
                      <Button busy={decide.isPending} onClick={() => decide.mutate({ id: r.request_id, verb: "approve" })}>Approve</Button>
                      <Button variant="danger" busy={decide.isPending} onClick={() => decide.mutate({ id: r.request_id, verb: "reject" })}>Reject</Button>
                    </div>
                  )
                ) : null}
              </Card>
            </li>
          ))}
        </ul>
      )}
      {decide.isError ? <div className="mt-4"><ErrorNotice error={decide.error} /></div> : null}
    </>
  );
}
