"use client";

import { useQuery } from "@tanstack/react-query";
import { useParams } from "next/navigation";
import { Card, DescriptionList, ErrorNotice, Loading, Money, PageHeader, StatusBadge, Table, Td } from "@/components/ui";
import { api } from "@/lib/api";
import { SettlementDetail } from "@/lib/schemas";

export default function SettlementStatement() {
  const { id } = useParams<{ id: string }>();
  const query = useQuery({ queryKey: ["settlement", id], queryFn: () => api(`/bff/v1/settlements/${id}`, SettlementDetail) });
  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorNotice error={query.error} />;
  const s = query.data;
  const payout = s.payout as Record<string, unknown> | null | undefined;
  return (
    <>
      <PageHeader title={`Settlement ${s.business_date}`} subtitle={<StatusBadge status={s.status} />} />
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Statement">
          <DescriptionList items={[
            ["Gross sales", <Money key="g" minor={s.gross_minor} />],
            ["Refunds and chargebacks", <Money key="d" minor={-s.payable_debits_minor} />],
            ["Credits returned", <Money key="c" minor={s.payable_credits_minor} />],
            ["Fees", <Money key="f" minor={-s.fee_minor} />],
            ["GST on fees", <Money key="t" minor={-s.gst_minor} />],
            ["Reserve held", <Money key="h" minor={-s.reserve_held_minor} />],
            ["Reserve released", <Money key="l" minor={s.reserve_released_minor} />],
            ["Recovered from earlier shortfalls", <Money key="r" minor={-s.recovered_minor} />],
            ["New shortfall (receivable)", <Money key="s" minor={s.shortfall_minor} />],
            ["Net payout", <strong key="n"><Money minor={s.net_payout_minor} /></strong>],
          ]} />
        </Card>
        <Card title="Payout">
          {payout ? (
            <DescriptionList items={[
              ["Status", <StatusBadge key="p" status={String(payout.status)} />],
              ["Amount", <Money key="a" minor={Number(payout.amount_minor)} />],
              ["Bank reference", String(payout.bank_reference ?? "—")],
              ["Instruction file", String(payout.instruction_object_key ?? "—")],
              ["File SHA-256", <span key="h" className="font-mono text-xs">{String(payout.instruction_sha256 ?? "—")}</span>],
            ]} />
          ) : <p className="text-sm">No payout (net amount was zero).</p>}
        </Card>
        <Card title={`Items (${s.items.length})`} className="lg:col-span-2">
          <Table caption="Settlement items" head={["Type", "Item", "Amount", "Fee"]}>
            {s.items.map((item) => (
              <tr key={`${item.item_type}-${item.item_id}`}>
                <Td>{item.item_type.replaceAll("_", " ")}</Td>
                <Td className="font-mono text-xs">{item.item_id}</Td>
                <Td><Money minor={item.amount_minor} /></Td>
                <Td>{item.fee_minor ? <Money minor={item.fee_minor} /> : "—"}</Td>
              </tr>
            ))}
          </Table>
        </Card>
      </div>
    </>
  );
}
