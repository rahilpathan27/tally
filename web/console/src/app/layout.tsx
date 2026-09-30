import type { Metadata } from "next";
import { headers } from "next/headers";
import { Providers } from "@/components/providers";
import "./globals.css";

export const metadata: Metadata = {
  title: { default: "Tally", template: "%s · Tally" },
  description: "Tally payments simulation console",
};

export default async function RootLayout({ children }: LayoutProps<"/">) {
  // Reading request headers renders pages per request so the CSP nonce can be applied.
  await headers();
  return (
    <html lang="en" className="h-full antialiased">
      <body className="min-h-full bg-zinc-50 text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
        <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2 focus:text-zinc-900">
          Skip to content
        </a>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
