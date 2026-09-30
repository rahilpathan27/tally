"use client";

import {
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  useEffect,
  useId,
  useRef,
} from "react";
import { ApiError } from "@/lib/api";
import { formatMoney } from "@/lib/money";

export function cx(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}

type Variant = "primary" | "secondary" | "danger" | "ghost";
const VARIANTS: Record<Variant, string> = {
  primary: "bg-indigo-600 text-white hover:bg-indigo-700 disabled:bg-indigo-300",
  secondary:
    "border border-zinc-300 bg-white text-zinc-900 hover:bg-zinc-50 dark:border-zinc-600 dark:bg-zinc-800 dark:text-zinc-100 dark:hover:bg-zinc-700",
  danger: "bg-red-600 text-white hover:bg-red-700 disabled:bg-red-300",
  ghost: "text-indigo-700 hover:bg-indigo-50 dark:text-indigo-300 dark:hover:bg-zinc-800",
};

export function Button({
  variant = "primary",
  className,
  busy,
  children,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant; busy?: boolean }) {
  return (
    <button
      type="button"
      {...props}
      disabled={props.disabled || busy}
      aria-busy={busy || undefined}
      className={cx(
        "inline-flex items-center gap-2 rounded-md px-3 py-2 text-sm font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed",
        VARIANTS[variant],
        className,
      )}
    >
      {busy ? <Spinner /> : null}
      {children}
    </button>
  );
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2">
      <span
        aria-hidden
        className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent"
      />
      <span className="sr-only">{label ?? "Loading"}</span>
    </span>
  );
}

export function Card({
  title,
  actions,
  children,
  className,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={cx(
        "rounded-lg border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-700 dark:bg-zinc-900",
        className,
      )}
    >
      {(title || actions) && (
        <header className="mb-3 flex items-center justify-between gap-2">
          {title ? <h2 className="text-base font-semibold">{title}</h2> : <span />}
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle ? <p className="mt-1 text-sm text-zinc-600 dark:text-zinc-400">{subtitle}</p> : null}
      </div>
      {actions}
    </div>
  );
}

const TONES: Record<string, string> = {
  green: "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200",
  red: "bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200",
  amber: "bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200",
  blue: "bg-sky-100 text-sky-900 dark:bg-sky-900/40 dark:text-sky-200",
  zinc: "bg-zinc-100 text-zinc-800 dark:bg-zinc-800 dark:text-zinc-200",
};
const STATUS_TONE: Record<string, keyof typeof TONES> = {
  succeeded: "green", paid: "green", posted: "green", executed: "green", resolved: "green",
  won: "green", approved: "green", allow: "green", ok: "green", auto_resolved: "green",
  failed: "red", reversed: "red", block: "red", lost: "red", rejected: "red", dead: "red",
  returned: "red", declined: "red", alert: "red",
  pending_unknown: "amber", risk_review: "amber", review: "amber", step_up: "amber",
  requires_action: "amber", pending_approval: "amber", open: "amber", needs_response: "amber",
  processing: "blue", authorizing: "blue", authorized: "blue", capturing: "blue", pending: "blue",
  sent: "blue", under_review: "blue",
};

export function Badge({ children, tone = "zinc" }: { children: ReactNode; tone?: keyof typeof TONES }) {
  return (
    <span className={cx("inline-flex rounded-full px-2 py-0.5 text-xs font-medium", TONES[tone])}>
      {children}
    </span>
  );
}

export function StatusBadge({ status }: { status: string }) {
  return <Badge tone={STATUS_TONE[status] ?? "zinc"}>{status.replaceAll("_", " ")}</Badge>;
}

export function Money({ minor, currency = "INR" }: { minor: number; currency?: string }) {
  return <span className="tabular-nums">{formatMoney(minor, currency)}</span>;
}

export function Table({
  caption,
  head,
  children,
}: {
  caption: string;
  head: ReactNode[];
  children: ReactNode;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-zinc-200 text-sm dark:divide-zinc-700">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr>
            {head.map((cell, i) => (
              <th key={i} scope="col" className="px-3 py-2 text-left font-medium text-zinc-600 dark:text-zinc-400">
                {cell}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-zinc-100 dark:divide-zinc-800">{children}</tbody>
      </table>
    </div>
  );
}

export function Td({ children, className }: { children: ReactNode; className?: string }) {
  return <td className={cx("px-3 py-2 align-top", className)}>{children}</td>;
}

export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: string;
  hint?: string;
  error?: string | null;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: boolean }) => ReactNode;
}) {
  const id = useId();
  const describedBy = error ? `${id}-error` : hint ? `${id}-hint` : undefined;
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {hint && !error ? <p id={`${id}-hint`} className="text-xs text-zinc-600 dark:text-zinc-400">{hint}</p> : null}
      {error ? <p id={`${id}-error`} role="alert" className="text-xs text-red-700 dark:text-red-300">{error}</p> : null}
    </div>
  );
}

const INPUT =
  "rounded-md border border-zinc-300 bg-white px-3 py-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-2 focus:ring-indigo-500 dark:border-zinc-600 dark:bg-zinc-800";

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cx(INPUT, props.className)} />;
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={cx(INPUT, props.className)} />;
}

export function Dialog({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const opener = useRef<Element | null>(null);
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      opener.current = document.activeElement;
      dialog.showModal(); // native focus trap and Escape handling
    } else if (!open && dialog.open) {
      dialog.close();
      (opener.current as HTMLElement | null)?.focus?.();
    }
  }, [open]);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-labelledby="dialog-title"
      className="m-auto w-full max-w-lg rounded-lg bg-white p-0 text-zinc-900 shadow-xl backdrop:bg-black/40 dark:bg-zinc-900 dark:text-zinc-100"
    >
      <div className="p-5">
        <div className="mb-4 flex items-center justify-between">
          <h2 id="dialog-title" className="text-lg font-semibold">
            {title}
          </h2>
          <Button variant="ghost" onClick={onClose} aria-label="Close dialog">
            ✕
          </Button>
        </div>
        {children}
      </div>
    </dialog>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-8 text-center text-sm text-zinc-600 dark:text-zinc-400">{children}</p>;
}

export function ErrorNotice({ error }: { error: unknown }) {
  const message =
    error instanceof ApiError
      ? `${error.message} (${error.code})`
      : error instanceof Error
        ? error.message
        : "Something went wrong.";
  return (
    <div role="alert" className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-900 dark:border-red-800 dark:bg-red-950 dark:text-red-200">
      {message}
    </div>
  );
}

export function Loading() {
  return (
    <div className="flex justify-center py-10">
      <Spinner />
    </div>
  );
}

export function DescriptionList({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-2">
      {items.map(([term, value]) => (
        <div key={term} className="flex flex-col">
          <dt className="text-zinc-600 dark:text-zinc-400">{term}</dt>
          <dd className="break-all font-medium">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}
