/**
 * Load + validate operator credentials/config from .env. Fail fast with a clear
 * message instead of opaque SDK auth errors several calls deep.
 */
import 'dotenv/config';

const DEFAULT_MODEL = 'anthropic/claude-sonnet-4.5';

export function loadSettings({ requireLitellm = false } = {}) {
  const required = ['VERCEL_TOKEN', 'VERCEL_TEAM_ID', 'VERCEL_PROJECT_ID', 'OPENROUTER_API_KEY'];
  if (requireLitellm) required.push('LITELLM_URL', 'LITELLM_MASTER_KEY');
  const missing = required.filter((k) => !process.env[k]);
  if (missing.length) {
    throw new Error(`Missing required env var(s): ${missing.join(', ')} (set them in .env)`);
  }
  return {
    vercel: {
      token: process.env.VERCEL_TOKEN,
      teamId: process.env.VERCEL_TEAM_ID,
      projectId: process.env.VERCEL_PROJECT_ID,
    },
    openrouter: {
      apiKey: process.env.OPENROUTER_API_KEY,
      model: process.env.OPENROUTER_MODEL || DEFAULT_MODEL,
    },
    litellm: {
      url: process.env.LITELLM_URL || null,
      masterKey: process.env.LITELLM_MASTER_KEY || null,
      enabled: Boolean(process.env.LITELLM_URL && process.env.LITELLM_MASTER_KEY),
    },
  };
}
