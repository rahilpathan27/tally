import { NextResponse } from "next/server";
import { z } from "zod";
import { merchantApi } from "@/lib/merchant-server";

const Order = z.object({
  amount_minor: z.number().int().positive().max(Number.MAX_SAFE_INTEGER),
  method: z.enum(["card", "upi"]),
  payment_method_token: z.string().min(8).max(200).optional(),
  payer_vpa: z.string().regex(/^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+$/).optional(),
  device_id: z.string().max(100),
  idempotency_key: z.uuid(),
});

// The browser sends only a token or VPA; the store's server creates and confirms the intent.
export async function POST(request: Request) {
  const parsed = Order.safeParse(await request.json());
  if (!parsed.success) return NextResponse.json({ error: "invalid order" }, { status: 422 });
  const order = parsed.data;
  const intent = order.method === "card"
    ? { payment_method_type: "card", payment_method_token: order.payment_method_token }
    : { payment_method_type: "upi", payer_vpa: order.payer_vpa, payee_vpa: "merchant@bank-b" };
  const created = await merchantApi("POST", "/v1/payment_intents", {
    amount_minor: order.amount_minor,
    currency: "INR",
    ...intent,
    risk_context: { device_id: order.device_id, ip_country: "IN" },
  }, `${order.idempotency_key}:create`);
  if (created.status !== 201) return NextResponse.json(created.body, { status: created.status });
  const paymentId = String(created.body.payment_id);
  const confirmed = await merchantApi("POST", `/v1/payment_intents/${paymentId}/confirm`, {}, `${order.idempotency_key}:confirm`);
  let result = confirmed.body;
  if (confirmed.status === 200 && result.status === "authorized") {
    // The demo store captures immediately (auto-capture).
    result = (await merchantApi("POST", `/v1/payment_intents/${paymentId}/capture`, {}, `${order.idempotency_key}:capture`)).body;
  }
  return NextResponse.json({ ...result, payment_id: paymentId }, { status: confirmed.status });
}
