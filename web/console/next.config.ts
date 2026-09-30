import type { NextConfig } from "next";

// The console talks to the back office same-origin: cookies stay SameSite=Strict and no CORS
// is needed. Only /auth and /bff are forwarded.
const bff = process.env.TALLY_BFF_URL ?? "http://127.0.0.1:8040";

const nextConfig: NextConfig = {
  poweredByHeader: false,
  async rewrites() {
    return [
      { source: "/auth/:path*", destination: `${bff}/auth/:path*` },
      { source: "/bff/:path*", destination: `${bff}/bff/:path*` },
    ];
  },
};

export default nextConfig;
