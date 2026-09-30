"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { z } from "zod";
import { Card, Empty, ErrorNotice, Loading, Money, PageHeader, StatusBadge, Table, Td } from "@/components/ui";
import { api } from "@/lib/api";
import { Settlement } from "@/lib/schemas";

export default function SettlementsPage() {
  const query = useQuery({ queryKey: ["settlements"], queryFn: () => api("/bff/v1/settlements", z.array(Settlement)) });
  return (
    <>
      <PageHeader title="Settlements" subtitle="T+N settlement statements with fees, GST on fees and reserves" />
      <Card>
        {query.isPending ? <Loading /> : query.isError ? <ErrorNotice error={query.error} /> : query.data.length === 0 ? <Empty>No settlements yet.</Empty> : (
          <Table caption="Settlements" head={["Business date", "Gross", "Fees + GST", "Reserve", "Net payout", "Payout"]}>
            {query.data.map((s) => (
              <tr key={s.settlement_id}>
                <Td><Link className="text-indigo-700 underline dark:text-indigo-300" href={`/merchant/settlements/${s.settlement_id}`}>{s.business_date}</Link></Td>
                <Td><Money minor={s.gross_minor} /></Td>
                <Td><Money minor={s.fee_minor + s.gst_minor} /></Td>
                <Td><Money minor={s.reserve_held_minor - s.reserve_released_minor} /></Td>
                <Td><Money minor={s.net_payout_minor} /></Td>
                <Td>{s.payout_status ? <StatusBadge status={s.payout_status} /> : <StatusBadge status={s.status} />}</Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
