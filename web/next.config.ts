import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Self-contained server bundle for the Docker runtime image (Dockerfile.web).
  output: "standalone",
  images: { unoptimized: true },
};

export default nextConfig;
