import { NextResponse } from "next/server";
import { z } from "zod";
import { merchantApi } from "@/lib/merchant-server";

const Body = z.object({ challenge_id: z.uuid(), code: z.string().regex(/^[0-9]{6}$/), idempotency_key: z.uuid() });

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  const parsed = Body.safeParse(await request.json());
  if (!parsed.success || !/^[0-9a-f-]{36}$/.test(id)) return NextResponse.json({ error: "invalid" }, { status: 422 });
  const verified = await merchantApi("POST", `/v1/payment_intents/${id}/step_up`, {
    challenge_id: parsed.data.challenge_id,
    code: parsed.data.code,
  }, `${parsed.data.idempotency_key}:step-up`);
  let body = verified.body;
  if (verified.status === 200 && body.status === "authorized") {
    body = (await merchantApi("POST", `/v1/payment_intents/${id}/capture`, {}, `${parsed.data.idempotency_key}:capture`)).body;
  }
  return NextResponse.json(body, { status: verified.status });
}
