"use client";

import { useQuery } from "@tanstack/react-query";
import { Badge, Card, ErrorNotice, Loading, PageHeader, Table, Td, when } from "@/components/ui";
import { api } from "@/lib/api";
import { AuditPage } from "@/lib/schemas";

export default function AuditLog() {
  const query = useQuery({ queryKey: ["audit"], queryFn: () => api("/bff/v1/ops/audit?limit=200", AuditPage) });
  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorNotice error={query.error} />;
  const { chain, items, latest_anchor: anchor } = query.data;
  return (
    <>
      <PageHeader title="Audit log" subtitle={<>Hash chain <Badge tone={chain.ok ? "green" : "red"}>{chain.ok ? `verified (${chain.entries} entries)` : `broken at ${chain.broken_at}`}</Badge> {anchor ? <>· last anchored at #{String(anchor.seq)}</> : " · not yet anchored"}</>} />
      <Card>
        <Table caption="Audit events" head={["#", "When", "Actor", "Action", "Subject"]}>
          {items.map((item) => (
            <tr key={item.seq}><Td>{item.seq}</Td><Td>{when(item.occurred_at)}</Td><Td>{item.actor}</Td><Td>{item.action.replaceAll("_", " ")}</Td><Td className="font-mono text-xs">{item.subject}</Td></tr>
          ))}
        </Table>
      </Card>
    </>
  );
}
