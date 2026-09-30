"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { z } from "zod";
import { hasAny, useSession } from "@/components/providers";
import { Button, Card, Empty, ErrorNotice, Field, Input, Loading, PageHeader, StatusBadge, Table, Td, when } from "@/components/ui";
import { api } from "@/lib/api";
import { AnyRecord } from "@/lib/schemas";

export default function AmlPage() {
  const session = useSession();
  const canAct = hasAny(session.data?.roles, ["ops_analyst", "risk_analyst"]);
  const queryClient = useQueryClient();
  const alerts = useQuery({ queryKey: ["aml"], queryFn: () => api("/bff/v1/ops/aml/alerts", z.array(AnyRecord)) });
  const [chosen, setChosen] = useState<string[]>([]);
  const [summary, setSummary] = useState("");
  const run = useMutation({
    mutationFn: () => api("/bff/v1/ops/aml/run", AnyRecord, { method: "POST", idempotencyKey: crypto.randomUUID() }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["aml"] }),
  });
  const open = useMutation({
    mutationFn: () => api("/bff/v1/ops/aml/cases", AnyRecord, { method: "POST", body: { alert_ids: chosen, summary }, idempotencyKey: crypto.randomUUID() }),
    onSuccess: () => { setChosen([]); setSummary(""); void queryClient.invalidateQueries({ queryKey: ["aml"] }); },
  });
  return (
    <>
      <PageHeader title="AML alerts" subtitle="Demonstration rules only; not a compliance programme." actions={canAct ? <Button variant="secondary" busy={run.isPending} onClick={() => run.mutate()}>Run detectors</Button> : null} />
      <Card>
        {alerts.isPending ? <Loading /> : alerts.isError ? <ErrorNotice error={alerts.error} /> : alerts.data.length === 0 ? <Empty>No open alerts.</Empty> : (
          <Table caption="Open AML alerts" head={["", "Type", "Subject", "Details", "Status", "Raised"]}>
            {alerts.data.map((a) => {
              const id = String(a.alert_id);
              return (
                <tr key={id}>
                  <Td>{canAct ? <input type="checkbox" aria-label={`Select alert ${id}`} checked={chosen.includes(id)} onChange={(e) => setChosen(e.target.checked ? [...chosen, id] : chosen.filter((c) => c !== id))} /> : null}</Td>
                  <Td>{String(a.alert_type).replaceAll("_", " ")}</Td><Td className="font-mono text-xs">{String(a.subject)}</Td>
                  <Td className="text-xs">{JSON.stringify(a.details)}</Td><Td><StatusBadge status={String(a.status)} /></Td><Td>{when(String(a.created_at))}</Td>
                </tr>
              );
            })}
          </Table>
        )}
        {canAct && chosen.length ? (
          <form className="mt-4 flex flex-wrap items-end gap-3" onSubmit={(e) => { e.preventDefault(); open.mutate(); }}>
            <div className="min-w-72 flex-1"><Field label="Case summary">{(p) => <Input {...p} required minLength={5} value={summary} onChange={(e) => setSummary(e.target.value)} />}</Field></div>
            <Button type="submit" busy={open.isPending}>Open case ({chosen.length})</Button>
          </form>
        ) : null}
        {open.isError ? <ErrorNotice error={open.error} /> : null}
      </Card>
    </>
  );
}
