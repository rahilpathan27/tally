"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useState } from "react";
import { Badge, Card, Empty, ErrorNotice, Loading, Money, PageHeader, StatusBadge, Table, Td, shortId } from "@/components/ui";
import { api } from "@/lib/api";
import { SwitchSnapshot } from "@/lib/schemas";

/** Live via server-sent events; falls back to polling if the stream is unavailable. */
function useSwitchFeed() {
  const [live, setLive] = useState<SwitchSnapshot | null>(null);
  const [streaming, setStreaming] = useState(false);
  useEffect(() => {
    const source = new EventSource("/bff/v1/ops/switch/stream");
    source.addEventListener("switch", (event) => {
      const parsed = SwitchSnapshot.safeParse(JSON.parse((event as MessageEvent<string>).data));
      if (parsed.success) { setLive(parsed.data); setStreaming(true); }
    });
    source.onerror = () => setStreaming(false);
    return () => source.close();
  }, []);
  const poll = useQuery({
    queryKey: ["switch"],
    queryFn: () => api("/bff/v1/ops/switch", SwitchSnapshot),
    refetchInterval: streaming ? false : 5_000,
  });
  return { data: live ?? poll.data, streaming, error: live ? null : poll.error, pending: !live && poll.isPending };
}

export default function SwitchMonitor() {
  const feed = useSwitchFeed();
  if (feed.pending) return <Loading />;
  if (!feed.data) return <ErrorNotice error={feed.error} />;
  const { in_flight: inFlight, banks, breakers } = feed.data;
  const unknown = inFlight.filter((p) => ["pending_unknown", "reversal_pending"].includes(p.status));
  return (
    <>
      <PageHeader title="Switch monitor" subtitle={<Badge tone={feed.streaming ? "green" : "amber"}>{feed.streaming ? "Live (SSE)" : "Polling every 5 s"}</Badge>} />
      <div className="mb-6 grid gap-4 md:grid-cols-3">
        <Card title="In flight"><p className="text-3xl font-semibold">{inFlight.length}</p></Card>
        <Card title="Unknown outcome"><p className="text-3xl font-semibold">{unknown.length}</p></Card>
        <Card title="Circuit breakers">
          <ul className="text-sm">
            {Object.entries(breakers).map(([name, value]) => {
              const state = value as { open?: boolean; failures?: number };
              return <li key={name} className="flex justify-between gap-2"><span>{name.replace("_breaker", "")}</span><StatusBadge status={state.open ? "open" : "ok"} /></li>;
            })}
          </ul>
        </Card>
      </div>
      <Card title="Banks (last hour)" className="mb-6">
        {banks.length === 0 ? <Empty>No UPI traffic in the last hour.</Empty> : (
          <Table caption="Per-bank outcomes" head={["Bank", "Succeeded", "Failed", "Unknown", "Success rate"]}>
            {banks.map((b) => (
              <tr key={b.bank_id}><Td>{b.bank_id}</Td><Td>{b.succeeded}</Td><Td>{b.failed}</Td><Td>{b.unknown}</Td><Td>{(b.success_rate_bps / 100).toFixed(1)}%</Td></tr>
            ))}
          </Table>
        )}
      </Card>
      <Card title="In-flight and unknown payments">
        {inFlight.length === 0 ? <Empty>Nothing in flight.</Empty> : (
          <Table caption="In-flight payments" head={["Payment", "Merchant", "Method", "Amount", "Status", "Age", "Status checks"]}>
            {inFlight.map((p) => (
              <tr key={p.payment_id}>
                <Td><Link className="font-mono text-indigo-700 underline dark:text-indigo-300" href={`/ops/payments/${p.payment_id}`}>{shortId(p.payment_id)}</Link></Td>
                <Td>{p.merchant_id}</Td><Td>{p.payment_method_type}</Td><Td><Money minor={p.amount_minor} /></Td>
                <Td><StatusBadge status={p.status} /></Td><Td>{p.age_seconds}s</Td><Td>{p.recovery_attempts}</Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
