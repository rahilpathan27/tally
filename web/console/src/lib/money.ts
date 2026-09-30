/**
 * Money is integer minor units end to end. Display goes through a decimal string built from
 * the integer; input is parsed from text to an integer without ever using a JS number for the
 * fractional part. parseFloat is never used on money.
 */

export const MAX_SAFE_MINOR = Number.MAX_SAFE_INTEGER;
const EXPONENT: Record<string, number> = { INR: 2, USD: 2, JPY: 0, KWD: 3 };

export function exponentOf(currency: string): number {
  const exponent = EXPONENT[currency];
  if (exponent === undefined) throw new Error(`unsupported currency ${currency}`);
  return exponent;
}

/** Decimal string for an integer minor amount, e.g. 123450 -> "1234.50". */
export function minorToDecimalString(minor: number | bigint, currency = "INR"): string {
  const value = BigInt(minor);
  if (typeof minor === "number" && !Number.isSafeInteger(minor)) {
    throw new Error("amount must be a safe integer");
  }
  const exponent = exponentOf(currency);
  const negative = value < BigInt(0);
  const digits = (negative ? -value : value).toString().padStart(exponent + 1, "0");
  const whole = exponent ? digits.slice(0, -exponent) : digits;
  const fraction = exponent ? `.${digits.slice(-exponent)}` : "";
  return `${negative ? "-" : ""}${whole}${fraction}`;
}

/** Locale display (₹1,234.50) using Intl on the exact decimal string. */
export function formatMoney(minor: number | bigint, currency = "INR", locale = "en-IN"): string {
  const decimal = minorToDecimalString(minor, currency);
  const exponent = exponentOf(currency);
  const format = new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    minimumFractionDigits: exponent,
    maximumFractionDigits: exponent,
  });
  // Intl accepts decimal strings exactly (no binary float conversion) for this API.
  return format.format(decimal as unknown as number);
}

export type ParseResult = { ok: true; minor: number } | { ok: false; error: string };

/** Parse user input like "1,234.5" to integer minor units, rejecting extra precision. */
export function parseMoneyInput(text: string, currency = "INR"): ParseResult {
  const exponent = exponentOf(currency);
  const cleaned = text.trim().replace(/[,\s₹]/g, "");
  if (!/^\d+(\.\d*)?$/.test(cleaned)) return { ok: false, error: "Enter an amount like 1234.50" };
  const [whole, fraction = ""] = cleaned.split(".");
  if (fraction.length > exponent) {
    return { ok: false, error: `Use at most ${exponent} decimal places` };
  }
  const minor = BigInt(whole) * BigInt(10) ** BigInt(exponent) + BigInt(fraction.padEnd(exponent, "0") || "0");
  if (minor <= BigInt(0)) return { ok: false, error: "Amount must be greater than zero" };
  if (minor > BigInt(MAX_SAFE_MINOR)) return { ok: false, error: "Amount is too large" };
  return { ok: true, minor: Number(minor) };
}
