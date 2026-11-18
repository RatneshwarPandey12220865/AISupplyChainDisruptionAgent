/** @type {import('next').NextConfig} */
// NEXT_DIST_DIR lets a throwaway build (e.g. a local e2e run) use its own folder without clobbering a running server's.
const nextConfig = { output: "standalone", distDir: process.env.NEXT_DIST_DIR || ".next" };
export default nextConfig;
