"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { hasAny, useSession } from "@/components/providers";
import { Button, cx, Loading } from "@/components/ui";
import { api } from "@/lib/api";
import { Anything } from "@/lib/schemas";

type NavItem = { href: string; label: string; roles: string[] };
const MERCHANT = ["merchant_admin", "merchant_developer", "merchant_viewer"];
const NAV: NavItem[] = [
  { href: "/merchant", label: "Overview", roles: MERCHANT },
  { href: "/merchant/payments", label: "Payments", roles: MERCHANT },
  { href: "/merchant/settlements", label: "Settlements", roles: ["merchant_admin", "merchant_viewer"] },
  { href: "/merchant/disputes", label: "Disputes", roles: MERCHANT },
  { href: "/merchant/developers", label: "Developers", roles: ["merchant_admin", "merchant_developer"] },
  { href: "/ops", label: "Switch monitor", roles: ["ops_analyst", "operator", "risk_analyst"] },
  { href: "/ops/recon", label: "Reconciliation", roles: ["ops_analyst", "approver", "operator"] },
  { href: "/ops/ledger", label: "Ledger", roles: ["ops_analyst", "approver", "operator"] },
  { href: "/ops/risk", label: "Risk", roles: ["risk_analyst", "approver", "operator"] },
  { href: "/ops/aml", label: "AML alerts", roles: ["ops_analyst", "risk_analyst", "approver"] },
  { href: "/ops/approvals", label: "Approvals", roles: ["approver", "ops_analyst", "risk_analyst", "operator"] },
  { href: "/ops/audit", label: "Audit log", roles: ["approver", "operator"] },
  { href: "/ops/chaos", label: "Chaos control", roles: ["operator"] },
];

export function AppShell({ children }: { children: ReactNode }) {
  const session = useSession();
  const pathname = usePathname();
  const router = useRouter();
  const queryClient = useQueryClient();
  if (session.isPending) return <Loading />;
  if (session.isError || !session.data) {
    if (typeof window !== "undefined") router.replace(`/login?next=${encodeURIComponent(pathname)}`);
    return <Loading />;
  }
  const roles = session.data.roles;
  const items = NAV.filter((item) => hasAny(roles, item.roles));

  async function logout() {
    await api("/auth/logout", Anything, { method: "POST" }).catch(() => undefined);
    queryClient.clear();
    router.replace("/login");
  }

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <nav aria-label="Console" className="border-b border-zinc-200 bg-white md:w-56 md:border-b-0 md:border-r dark:border-zinc-800 dark:bg-zinc-900">
        <div className="px-4 py-4">
          <Link href="/" className="text-lg font-semibold">Tally</Link>
          <p className="mt-1 truncate text-xs text-zinc-600 dark:text-zinc-400" title={session.data.email}>
            {session.data.email}
          </p>
          <p className="text-xs text-zinc-600 dark:text-zinc-400">{roles.join(", ").replaceAll("_", " ")}</p>
        </div>
        <ul className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col">
          {items.map((item) => {
            const active = pathname === item.href || (item.href.split("/").length > 2 && pathname.startsWith(`${item.href}/`));
            return (
              <li key={item.href}>
                <Link
                  href={item.href}
                  aria-current={active ? "page" : undefined}
                  className={cx(
                    "block whitespace-nowrap rounded-md px-3 py-2 text-sm",
                    active ? "bg-indigo-50 font-medium text-indigo-800 dark:bg-zinc-800 dark:text-indigo-200" : "hover:bg-zinc-100 dark:hover:bg-zinc-800",
                  )}
                >
                  {item.label}
                </Link>
              </li>
            );
          })}
        </ul>
        <div className="px-4 pb-4">
          <Button variant="secondary" onClick={logout}>Sign out</Button>
        </div>
      </nav>
      <main id="main" className="flex-1 px-4 py-6 md:px-8">
        {children}
      </main>
    </div>
  );
}
