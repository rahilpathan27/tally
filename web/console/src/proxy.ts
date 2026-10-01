import { NextRequest, NextResponse } from "next/server";

// Per-request CSP nonce plus security headers for every page (API rewrites excluded).
export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const isDev = process.env.NODE_ENV === "development";
  const vault = process.env.NEXT_PUBLIC_VAULT_URL ?? "http://127.0.0.1:8002";
  // In AWS, hashed /_next/static assets are served from a CloudFront origin (no user data).
  const assets = process.env.NEXT_PUBLIC_ASSET_ORIGIN ? ` ${process.env.NEXT_PUBLIC_ASSET_ORIGIN}` : "";
  const csp = [
    "default-src 'self'",
    `script-src 'self'${assets} 'nonce-${nonce}' 'strict-dynamic'${isDev ? " 'unsafe-eval'" : ""}`,
    `style-src 'self'${assets}${isDev ? " 'unsafe-inline'" : ` 'nonce-${nonce}'`}`,
    `img-src 'self'${assets} blob: data:`,
    `font-src 'self'${assets}`,
    `connect-src 'self' ${vault}${isDev ? " ws:" : ""}`,
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", csp);
  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("Content-Security-Policy", csp);
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("Referrer-Policy", "no-referrer");
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set("Permissions-Policy", "camera=(), microphone=(), geolocation=()");
  return response;
}

export const config = {
  matcher: [
    {
      source: "/((?!auth|bff|api|_next/static|_next/image|favicon.ico).*)",
      missing: [{ type: "header", key: "next-router-prefetch" }],
    },
  ],
};
