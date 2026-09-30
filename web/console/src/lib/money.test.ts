import { describe, expect, it } from "vitest";
import { formatMoney, minorToDecimalString, parseMoneyInput } from "./money";

describe("money", () => {
  it("renders minor units exactly", () => {
    expect(minorToDecimalString(123450)).toBe("1234.50");
    expect(minorToDecimalString(5)).toBe("0.05");
    expect(minorToDecimalString(-7)).toBe("-0.07");
    expect(minorToDecimalString(1500, "JPY")).toBe("1500");
    expect(minorToDecimalString(1234, "KWD")).toBe("1.234");
    expect(minorToDecimalString(Number.MAX_SAFE_INTEGER)).toBe("90071992547409.91");
  });

  it("formats with Intl without float drift", () => {
    expect(formatMoney(123450)).toBe("₹1,234.50");
    expect(formatMoney(Number.MAX_SAFE_INTEGER)).toBe("₹9,00,71,99,25,47,409.91");
    expect(formatMoney(10 + 20)).toBe("₹0.30");
  });

  it("parses input to integer minor units", () => {
    expect(parseMoneyInput("1,234.5")).toEqual({ ok: true, minor: 123450 });
    expect(parseMoneyInput("0.1")).toEqual({ ok: true, minor: 10 });
    expect(parseMoneyInput("₹ 99")).toEqual({ ok: true, minor: 9900 });
    expect(parseMoneyInput("1.005").ok).toBe(false);
    expect(parseMoneyInput("-5").ok).toBe(false);
    expect(parseMoneyInput("0").ok).toBe(false);
    expect(parseMoneyInput("1e3").ok).toBe(false);
    expect(parseMoneyInput("99999999999999999").ok).toBe(false);
  });
});
