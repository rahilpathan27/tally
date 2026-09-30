"use client";

import { useInfiniteQuery } from "@tanstack/react-query";
import Link from "next/link";
import { type FormEvent, useState } from "react";
import { Button, Card, Empty, ErrorNotice, Field, Input, Loading, Money, PageHeader, Select, StatusBadge, Table, Td, shortId, when } from "@/components/ui";
import { api } from "@/lib/api";
import { parseMoneyInput } from "@/lib/money";
import { PaymentPage } from "@/lib/schemas";

const STATUSES = ["", "created", "risk_review", "authorizing", "authorized", "succeeded", "failed", "pending_unknown", "reversed", "cancelled"];

export default function PaymentsPage() {
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [draft, setDraft] = useState({ status: "", method: "", q: "", min: "", max: "", from: "", to: "" });
  const [error, setError] = useState<string | null>(null);

  const query = useInfiniteQuery({
    queryKey: ["payments", filters],
    initialPageParam: "",
    queryFn: ({ pageParam, signal }) => {
      const params = new URLSearchParams({ ...filters, limit: "25" });
      if (pageParam) params.set("cursor", pageParam);
      return api(`/bff/v1/payments?${params}`, PaymentPage, { signal });
    },
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  function apply(event: FormEvent) {
    event.preventDefault();
    const next: Record<string, string> = {};
    if (draft.status) next.status = draft.status;
    if (draft.method) next.method = draft.method;
    if (draft.q.trim()) next.q = draft.q.trim();
    for (const [key, target] of [["min", "min_amount_minor"], ["max", "max_amount_minor"]] as const) {
      if (draft[key]) {
        const parsed = parseMoneyInput(draft[key]);
        if (!parsed.ok) return setError(`${key === "min" ? "Minimum" : "Maximum"}: ${parsed.error}`);
        next[target] = String(parsed.minor);
      }
    }
    if (draft.from) next.created_from = new Date(draft.from).toISOString();
    if (draft.to) next.created_to = new Date(draft.to).toISOString();
    setError(null);
    setFilters(next);
  }

  const rows = query.data?.pages.flatMap((page) => page.items) ?? [];
  const exportHref = `/bff/v1/payments/export.csv?${new URLSearchParams(filters)}`;
  return (
    <>
      <PageHeader title="Payments" actions={<a className="text-sm font-medium text-indigo-700 underline dark:text-indigo-300" href={exportHref}>Export CSV</a>} />
      <Card className="mb-4">
        <form onSubmit={apply} className="grid gap-3 sm:grid-cols-3 lg:grid-cols-7" aria-label="Filter payments">
          <Field label="Search">{(p) => <Input {...p} placeholder="ID or VPA" value={draft.q} onChange={(e) => setDraft({ ...draft, q: e.target.value })} />}</Field>
          <Field label="Status">{(p) => (
            <Select {...p} value={draft.status} onChange={(e) => setDraft({ ...draft, status: e.target.value })}>
              {STATUSES.map((s) => <option key={s} value={s}>{s ? s.replaceAll("_", " ") : "Any"}</option>)}
            </Select>
          )}</Field>
          <Field label="Method">{(p) => (
            <Select {...p} value={draft.method} onChange={(e) => setDraft({ ...draft, method: e.target.value })}>
              <option value="">Any</option><option value="upi">UPI</option><option value="card">Card</option>
            </Select>
          )}</Field>
          <Field label="Min amount (₹)">{(p) => <Input {...p} inputMode="decimal" value={draft.min} onChange={(e) => setDraft({ ...draft, min: e.target.value })} />}</Field>
          <Field label="Max amount (₹)">{(p) => <Input {...p} inputMode="decimal" value={draft.max} onChange={(e) => setDraft({ ...draft, max: e.target.value })} />}</Field>
          <Field label="From">{(p) => <Input {...p} type="date" value={draft.from} onChange={(e) => setDraft({ ...draft, from: e.target.value })} />}</Field>
          <div className="flex items-end"><Button type="submit">Apply</Button></div>
        </form>
        {error ? <p role="alert" className="mt-2 text-sm text-red-700">{error}</p> : null}
      </Card>
      <Card>
        {query.isPending ? <Loading /> : query.isError ? <ErrorNotice error={query.error} /> : rows.length === 0 ? <Empty>No payments match these filters.</Empty> : (
          <Table caption="Payments" head={["Payment", "Created", "Method", "Amount", "Status", "Risk"]}>
            {rows.map((row) => (
              <tr key={row.payment_id}>
                <Td><Link className="font-mono text-indigo-700 underline dark:text-indigo-300" href={`/merchant/payments/${row.payment_id}`}>{shortId(row.payment_id)}</Link></Td>
                <Td>{when(row.created_at)}</Td>
                <Td>{row.payment_method_type === "upi" ? `UPI · ${row.payer_vpa}` : "Card"}</Td>
                <Td><Money minor={row.amount_minor} currency={row.currency.trim()} /></Td>
                <Td><StatusBadge status={row.status} /></Td>
                <Td>{row.risk_decision ? <StatusBadge status={row.risk_decision} /> : "—"}</Td>
              </tr>
            ))}
          </Table>
        )}
        {query.hasNextPage ? (
          <div className="mt-4 flex justify-center">
            <Button variant="secondary" busy={query.isFetchingNextPage} onClick={() => query.fetchNextPage()}>Load more</Button>
          </div>
        ) : null}
      </Card>
    </>
  );
}
