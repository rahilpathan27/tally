import { NextResponse } from "next/server";
import { merchantApi } from "@/lib/merchant-server";

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  if (!/^[0-9a-f-]{36}$/.test(id)) return NextResponse.json({ error: "bad id" }, { status: 400 });
  const result = await merchantApi("GET", `/v1/payment_intents/${id}`);
  return NextResponse.json(result.body, { status: result.status });
}
