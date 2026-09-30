import { NextResponse } from "next/server";
import { payerOtp } from "@/lib/merchant-server";

// Stands in for the SMS/app notification a payer would receive. Simulation only.
export async function GET(request: Request) {
  const challenge = new URL(request.url).searchParams.get("challenge_id") ?? "";
  if (!/^[0-9a-f-]{36}$/.test(challenge)) return NextResponse.json({ error: "bad challenge" }, { status: 400 });
  const code = await payerOtp(challenge);
  return code ? NextResponse.json({ code }) : NextResponse.json({ error: "not found" }, { status: 404 });
}
