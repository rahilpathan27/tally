"use client";

import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Badge, Button, Card, ErrorNotice, Loading, Money, PageHeader, StatusBadge, Table, Td, when } from "@/components/ui";
import { api } from "@/lib/api";
import { IntegrityChecks, Statement, TrialBalance } from "@/lib/schemas";

export default function LedgerExplorer() {
  const [account, setAccount] = useState<string | null>(null);
  const trial = useQuery({ queryKey: ["trial-balance"], queryFn: () => api("/bff/v1/ops/ledger/trial-balance", TrialBalance) });
  const integrity = useQuery({ queryKey: ["integrity"], queryFn: () => api("/bff/v1/ops/ledger/integrity", IntegrityChecks) });
  return (
    <>
      <PageHeader title="Ledger explorer" subtitle="Balances are derived from immutable postings; fee-income shards roll up." />
      <Card title="Integrity" className="mb-6">
        {integrity.isPending ? <Loading /> : integrity.isError ? <ErrorNotice error={integrity.error} /> : (
          <ul className="flex flex-wrap gap-4 text-sm">
            {integrity.data.map((check) => (
              <li key={check.check_name} className="flex items-center gap-2"><StatusBadge status={check.ok ? "ok" : "alert"} />{check.detail}</li>
            ))}
          </ul>
        )}
      </Card>
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Trial balance">
          {trial.isPending ? <Loading /> : trial.isError ? <ErrorNotice error={trial.error} /> : (
            <>
              <p className="mb-3 text-sm">Debits <Money minor={trial.data.total_debit_minor} /> · Credits <Money minor={trial.data.total_credit_minor} /> <Badge tone={trial.data.balanced ? "green" : "red"}>{trial.data.balanced ? "balanced" : "UNBALANCED"}</Badge></p>
              <Table caption="Trial balance" head={["Account", "Type", "Balance", ""]}>
                {trial.data.rows.filter((row) => row.debit_minor || row.credit_minor).map((row) => (
                  <tr key={row.account_id}>
                    <Td className="font-mono text-xs">{row.account_id}</Td><Td>{row.account_type}</Td>
                    <Td><Money minor={row.balance_minor} /></Td>
                    <Td><Button variant="ghost" onClick={() => setAccount(row.account_id)} aria-label={`Statement for ${row.account_id}`}>Statement</Button></Td>
                  </tr>
                ))}
              </Table>
            </>
          )}
        </Card>
        {account ? <StatementCard account={account} /> : <Card title="Statement"><p className="text-sm">Choose an account.</p></Card>}
      </div>
    </>
  );
}

function StatementCard({ account }: { account: string }) {
  const query = useInfiniteQuery({
    queryKey: ["statement", account],
    initialPageParam: "",
    queryFn: ({ pageParam }) => api(`/bff/v1/ops/ledger/accounts/${encodeURIComponent(account)}/statement?limit=50${pageParam ? `&cursor=${pageParam}` : ""}`, Statement),
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const lines = query.data?.pages.flatMap((p) => p.lines) ?? [];
  return (
    <Card title={<span className="font-mono text-sm">{account}</span>}>
      {query.isPending ? <Loading /> : query.isError ? <ErrorNotice error={query.error} /> : (
        <Table caption={`Statement for ${account}`} head={["Entry", "Key", "Change", "When"]}>
          {lines.map((line) => (
            <tr key={line.posting_id}>
              <Td>#{line.entry_id}</Td><Td className="max-w-xs break-all font-mono text-xs">{line.source_key}</Td>
              <Td><Money minor={line.natural_delta_minor} /></Td><Td>{when(line.created_at)}</Td>
            </tr>
          ))}
        </Table>
      )}
      {query.hasNextPage ? <Button variant="secondary" className="mt-3" onClick={() => query.fetchNextPage()}>Older</Button> : null}
    </Card>
  );
}
