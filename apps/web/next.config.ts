import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  async rewrites() {
    const origin = process.env.SCILAB_API_ORIGIN;
    return { fallback: origin ? [{ source: "/v1/:path*", destination: `${origin}/v1/:path*` }] : [] };
  },
};

export default nextConfig;
