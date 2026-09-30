import Link from "next/link";

export default function Home() {
  const links = [
    { href: "/login", title: "Console", body: "Merchant dashboard and ops/risk console (sign in)." },
    { href: "/checkout", title: "Hosted checkout", body: "Pay the demo store by test card or UPI." },
    { href: "/payer", title: "Payer phone", body: "Receive simulated OTPs for step-up challenges." },
  ];
  return (
    <main id="main" className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-3xl font-semibold tracking-tight">Tally</h1>
      <p className="mt-2 text-zinc-600 dark:text-zinc-400">
        A payments platform simulation. No real money, cards or personal data.
      </p>
      <ul className="mt-8 grid gap-4 sm:grid-cols-3">
        {links.map((link) => (
          <li key={link.href}>
            <Link
              href={link.href}
              className="block h-full rounded-lg border border-zinc-200 bg-white p-4 hover:border-indigo-400 focus-visible:ring-2 focus-visible:ring-indigo-500 dark:border-zinc-700 dark:bg-zinc-900"
            >
              <span className="font-semibold">{link.title}</span>
              <span className="mt-1 block text-sm text-zinc-600 dark:text-zinc-400">{link.body}</span>
            </Link>
          </li>
        ))}
      </ul>
    </main>
  );
}
