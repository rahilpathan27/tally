"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { z } from "zod";
import { hasAny, useSession } from "@/components/providers";
import { Badge, Button, Card, Empty, ErrorNotice, Field, Input, Loading, Money, PageHeader, StatusBadge, Table, Td, shortId, when } from "@/components/ui";
import { api } from "@/lib/api";
import { useIdempotencyKey } from "@/lib/idempotency";
import { AnyRecord, ReviewCase } from "@/lib/schemas";

export default function RiskConsole() {
  const session = useSession();
  const roles = session.data?.roles;
  return (
    <>
      <PageHeader title="Risk console" />
      <div className="flex flex-col gap-6">
        <ReviewQueue canAct={hasAny(roles, ["risk_analyst"])} />
        <Performance />
        <Rules canPropose={hasAny(roles, ["risk_analyst"])} />
        <Models canPropose={hasAny(roles, ["risk_analyst", "operator"])} />
      </div>
    </>
  );
}

function ReviewQueue({ canAct }: { canAct: boolean }) {
  const queryClient = useQueryClient();
  const cases = useQuery({ queryKey: ["reviews"], queryFn: () => api("/bff/v1/ops/risk/reviews", z.array(ReviewCase)), refetchInterval: 15_000 });
  const [selected, setSelected] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const idempotency = useIdempotencyKey();
  const resolve = useMutation({
    mutationFn: ({ caseId, outcome }: { caseId: string; outcome: "approve" | "decline" }) =>
      api(`/bff/v1/ops/risk/reviews/${caseId}/resolve`, AnyRecord, { method: "POST", body: { outcome, note: note || `${outcome}d by analyst` }, idempotencyKey: idempotency.key() }),
    onSuccess: () => { idempotency.rotate(); setSelected(null); setNote(""); void queryClient.invalidateQueries({ queryKey: ["reviews"] }); },
  });
  const current = cases.data?.find((c) => c.case_id === selected);
  return (
    <Card title="Review queue">
      {cases.isPending ? <Loading /> : cases.isError ? <ErrorNotice error={cases.error} /> : cases.data.length === 0 ? <Empty>No payments waiting for review.</Empty> : (
        <div className="grid gap-4 lg:grid-cols-2">
          <Table caption="Open review cases" head={["Payment", "Amount", "Score", "Top reasons", "SLA"]}>
            {cases.data.map((c) => (
              <tr key={c.case_id} className={c.case_id === selected ? "bg-indigo-50 dark:bg-zinc-800" : ""}>
                <Td><Button variant="ghost" onClick={() => setSelected(c.case_id)} aria-label={`Open case for payment ${c.payment_id}`}>{shortId(c.payment_id)}</Button></Td>
                <Td><Money minor={c.amount_minor} /> <Badge>{c.method}</Badge></Td>
                <Td>{c.model_score === null || c.model_score === undefined ? "—" : c.model_score.toFixed(3)}</Td>
                <Td className="text-xs">{c.reason_codes.slice(0, 3).map((r) => r.code).join(", ")}</Td>
                <Td>{c.sla_breached ? <Badge tone="red">breached</Badge> : when(c.sla_due_at)}</Td>
              </tr>
            ))}
          </Table>
          {current ? (
            <div className="flex flex-col gap-3">
              <h3 className="font-semibold">Case {shortId(current.case_id)}</h3>
              <ul className="text-sm">
                {current.reason_codes.map((r) => <li key={r.code + r.source}><Badge tone={r.source === "rule" ? "blue" : "zinc"}>{r.source}</Badge> <strong>{r.code}</strong>: {r.description}</li>)}
              </ul>
              <details>
                <summary className="cursor-pointer text-sm">Feature values at decision time</summary>
                <dl className="mt-2 grid grid-cols-2 gap-1 text-xs">
                  {Object.entries(current.features).map(([k, v]) => <div key={k} className="contents"><dt>{k}</dt><dd className="font-mono">{v}</dd></div>)}
                </dl>
              </details>
              {canAct ? (
                <>
                  <Field label="Analyst note">{(p) => <Input {...p} value={note} onChange={(e) => setNote(e.target.value)} />}</Field>
                  {resolve.isError ? <ErrorNotice error={resolve.error} /> : null}
                  <div className="flex gap-2">
                    <Button busy={resolve.isPending} onClick={() => resolve.mutate({ caseId: current.case_id, outcome: "approve" })}>Approve payment</Button>
                    <Button variant="danger" busy={resolve.isPending} onClick={() => resolve.mutate({ caseId: current.case_id, outcome: "decline" })}>Decline as fraud</Button>
                  </div>
                </>
              ) : null}
            </div>
          ) : <p className="text-sm">Select a case to see its reasons and features.</p>}
        </div>
      )}
    </Card>
  );
}

function Performance() {
  const perf = useQuery({ queryKey: ["risk-perf"], queryFn: () => api("/bff/v1/ops/risk/model-performance", AnyRecord) });
  const [drift, setDrift] = useState<Record<string, unknown> | null>(null);
  const run = useMutation({
    mutationFn: () => api("/bff/v1/ops/risk/drift/run?sample=50", AnyRecord, { method: "POST", idempotencyKey: crypto.randomUUID() }),
    onSuccess: setDrift,
  });
  const features = (drift?.features as { feature: string; psi: number; ks: number; status: string }[] | undefined) ?? [];
  return (
    <Card title="Model health" actions={<Button variant="secondary" busy={run.isPending} onClick={() => run.mutate()}>Run drift check</Button>}>
      {perf.isPending ? <Loading /> : perf.isError ? <ErrorNotice error={perf.error} /> : (
        <dl className="mb-4 grid grid-cols-2 gap-2 text-sm md:grid-cols-4">
          <div><dt className="text-zinc-600 dark:text-zinc-400">Decision mix (24h)</dt><dd>{Object.entries((perf.data.decision_mix_24h as Record<string, number>) ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ")}</dd></div>
          <div><dt className="text-zinc-600 dark:text-zinc-400">Latency p50 / p99</dt><dd>{String(perf.data.latency_us_p50 ?? "—")} / {String(perf.data.latency_us_p99 ?? "—")} µs</dd></div>
          <div><dt className="text-zinc-600 dark:text-zinc-400">Labelled precision</dt><dd>{perf.data.labelled_precision == null ? "—" : Number(perf.data.labelled_precision).toFixed(2)}</dd></div>
          <div><dt className="text-zinc-600 dark:text-zinc-400">Labelled recall</dt><dd>{perf.data.labelled_recall == null ? "—" : Number(perf.data.labelled_recall).toFixed(2)}</dd></div>
        </dl>
      )}
      {run.isError ? <ErrorNotice error={run.error} /> : null}
      {features.length ? (
        <Table caption="Drift by feature" head={["Feature", "PSI", "KS", "Status"]}>
          {features.map((f) => <tr key={f.feature}><Td className="font-mono text-xs">{f.feature}</Td><Td>{f.psi}</Td><Td>{f.ks}</Td><Td><StatusBadge status={f.status} /></Td></tr>)}
        </Table>
      ) : null}
    </Card>
  );
}

function Rules({ canPropose }: { canPropose: boolean }) {
  const rules = useQuery({ queryKey: ["rules"], queryFn: () => api("/bff/v1/ops/risk/rules", AnyRecord) });
  const [draft, setDraft] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const idempotency = useIdempotencyKey();
  const propose = useMutation({
    mutationFn: (definition: unknown) => api("/bff/v1/ops/risk/rules/proposals", AnyRecord, { method: "POST", body: { definition }, idempotencyKey: idempotency.key() }),
    onSuccess: () => { idempotency.rotate(); setDraft(null); },
  });
  if (rules.isPending) return <Card title="Rules"><Loading /></Card>;
  if (rules.isError) return <Card title="Rules"><ErrorNotice error={rules.error} /></Card>;
  const list = (rules.data.rules as { rule_id: string; description: string; action: string; reason_code: string }[]) ?? [];
  return (
    <Card title={`Rules (version ${String(rules.data.version)})`} actions={canPropose && draft === null ? <Button variant="secondary" onClick={() => setDraft(JSON.stringify({ rules: rules.data.rules, lists: { blocklist: {}, allowlist: {} } }, null, 2))}>Propose change</Button> : null}>
      <Table caption="Active rules" head={["Rule", "Description", "Action", "Reason code"]}>
        {list.map((r) => <tr key={r.rule_id}><Td>{r.rule_id}</Td><Td>{r.description}</Td><Td><StatusBadge status={r.action} /></Td><Td className="font-mono text-xs">{r.reason_code}</Td></tr>)}
      </Table>
      {propose.isSuccess ? <p role="status" className="mt-3 text-sm">Proposal submitted; an approver must activate it.</p> : null}
      {draft !== null ? (
        <form className="mt-4 flex flex-col gap-2" onSubmit={(e) => {
          e.preventDefault();
          try { setError(null); propose.mutate(JSON.parse(draft)); } catch { setError("The rule set is not valid JSON."); }
        }}>
          <label htmlFor="rules-json" className="text-sm font-medium">Rule set JSON (validated by the risk service)</label>
          <textarea id="rules-json" rows={14} value={draft} onChange={(e) => setDraft(e.target.value)} className="rounded-md border border-zinc-300 p-2 font-mono text-xs dark:border-zinc-600 dark:bg-zinc-800" />
          {error ? <p role="alert" className="text-sm text-red-700">{error}</p> : null}
          {propose.isError ? <ErrorNotice error={propose.error} /> : null}
          <div className="flex gap-2"><Button type="submit" busy={propose.isPending}>Submit for approval</Button><Button variant="secondary" onClick={() => setDraft(null)}>Cancel</Button></div>
        </form>
      ) : null}
    </Card>
  );
}

function Models({ canPropose }: { canPropose: boolean }) {
  const models = useQuery({ queryKey: ["models"], queryFn: () => api("/bff/v1/ops/risk/models", z.array(AnyRecord)) });
  const propose = useMutation({
    mutationFn: (version: string) => api(`/bff/v1/ops/risk/models/${version}/champion-proposals`, AnyRecord, { method: "POST", idempotencyKey: crypto.randomUUID() }),
  });
  return (
    <Card title="Models">
      {models.isPending ? <Loading /> : models.isError ? <ErrorNotice error={models.error} /> : (
        <Table caption="Model registry" head={["Version", "Stage", "PR-AUC", "Recall @1% FPR", ""]}>
          {models.data.map((m) => {
            const metrics = (m.metrics as Record<string, number>) ?? {};
            return (
              <tr key={String(m.version)}>
                <Td className="font-mono text-xs">{String(m.version)}</Td><Td><StatusBadge status={String(m.stage)} /></Td>
                <Td>{metrics.pr_auc?.toFixed(3) ?? "—"}</Td><Td>{metrics["recall_at_fpr_0.01"]?.toFixed(3) ?? "—"}</Td>
                <Td>{canPropose && m.stage !== "champion" ? <Button variant="ghost" onClick={() => propose.mutate(String(m.version))}>Propose as champion</Button> : null}</Td>
              </tr>
            );
          })}
        </Table>
      )}
      {propose.isError ? <ErrorNotice error={propose.error} /> : null}
      {propose.isSuccess ? <p role="status" className="mt-2 text-sm">Promotion proposed; waiting for an approver.</p> : null}
    </Card>
  );
}
