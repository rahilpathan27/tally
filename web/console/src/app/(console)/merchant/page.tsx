"use client";

import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Card, ErrorNotice, Loading, Money, PageHeader, Table, Td } from "@/components/ui";
import { api } from "@/lib/api";
import { formatMoney } from "@/lib/money";
import { Analytics } from "@/lib/schemas";

function rate(succeeded: number, attempts: number): string {
  return attempts ? `${((succeeded * 1000) / attempts / 10).toFixed(1)}%` : "—";
}

export default function MerchantOverview() {
  const query = useQuery({ queryKey: ["analytics"], queryFn: () => api("/bff/v1/analytics?days=30", Analytics) });
  if (query.isPending) return <Loading />;
  if (query.isError) return <ErrorNotice error={query.error} />;
  const data = query.data;
  const volume = data.daily.reduce((sum, d) => sum + d.volume_minor, 0);
  const attempts = data.daily.reduce((sum, d) => sum + d.attempts, 0);
  const succeeded = data.daily.reduce((sum, d) => sum + d.succeeded, 0);
  return (
    <>
      <PageHeader title="Overview" subtitle="Last 30 days (IST business days)" />
      <div className="mb-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[
          ["Captured volume", <Money key="v" minor={volume} />],
          ["Success rate", rate(succeeded, attempts)],
          ["Refunded", <Money key="r" minor={data.refunds.refunded_minor} />],
          // Creation to authorised/succeeded, including time spent in risk review or entering a code.
          ["Time to complete p50 / p95", `${Math.round(data.confirm_latency_ms.p50 ?? 0)} / ${Math.round(data.confirm_latency_ms.p95 ?? 0)} ms`],
        ].map(([label, value]) => (
          <Card key={String(label)}>
            <p className="text-sm text-zinc-600 dark:text-zinc-400">{label}</p>
            <p className="mt-1 text-2xl font-semibold">{value}</p>
          </Card>
        ))}
      </div>
      <Card title="Daily captured volume" className="mb-6">
        <div className="h-64" role="img" aria-label="Bar chart of daily captured volume; the table below lists the same data">
          <ResponsiveContainer>
            <BarChart data={data.daily}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="day" fontSize={12} />
              <YAxis fontSize={12} tickFormatter={(v: number) => formatMoney(Math.round(v)).replace(".00", "")} width={90} />
              <Tooltip formatter={(v) => formatMoney(Number(v))} />
              <Bar dataKey="volume_minor" name="Volume" fill="#4f46e5" />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </Card>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Success rate by method">
          <Table caption="Success rate by payment method" head={["Method", "Attempts", "Success rate"]}>
            {data.by_method.map((row) => (
              <tr key={row.method}><Td>{row.method}</Td><Td>{row.attempts}</Td><Td>{rate(row.succeeded, row.attempts)}</Td></tr>
            ))}
          </Table>
        </Card>
        <Card title="Success rate by payer bank (UPI)">
          <Table caption="Success rate by payer bank" head={["Bank", "Attempts", "Success rate"]}>
            {data.by_bank.map((row) => (
              <tr key={row.bank_id}><Td>{row.bank_id}</Td><Td>{row.attempts}</Td><Td>{rate(row.succeeded, row.attempts)}</Td></tr>
            ))}
          </Table>
        </Card>
      </div>
    </>
  );
}
