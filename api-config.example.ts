/**
 * Copy into the Next.js app as `src/lib/api-config.ts` - it is referenced by
 * `openapi-ts.config.ts` (`runtimeConfigPath`) and is where the generated
 * fetch client picks up its base URL and auth headers.
 */
import type { CreateClientConfig } from './lib/api/client.gen';

export const createClientConfig: CreateClientConfig = (config) => ({
  ...config,
  baseUrl: process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000',
  headers: {
    ...config?.headers,
    // The ingestion routes are cron-only; a browser client never needs this.
    // Server-side callers can inject it here from a server-only env var.
    ...(process.env.CRON_TOKEN ? { 'X-Cron-Token': process.env.CRON_TOKEN } : {}),
  },
});
