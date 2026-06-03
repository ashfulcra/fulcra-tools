/**
 * Spawn ONE guest sandbox from the fhv-hermes-demo snapshot.
 *
 * Either:
 *   - Baseline: inject the operator's capped OPENROUTER_API_KEY directly.
 *   - Gateway: if LITELLM_URL + LITELLM_MASTER_KEY are set, mint a per-sandbox
 *     virtual key via LiteLLM admin and inject (BASE_URL, virtual key) instead.
 *
 * Prints the guest's preview URL (port 8080 → Caddy → Hermes chat).
 */
import './fetch-shim.js';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Sandbox } from '@vercel/sandbox';
import { loadSettings } from './config.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SNAPSHOT_FILE = join(__dirname, '..', '.snapshot.json');

// Per-sandbox max lifetime. Vercel Pro caps at 5 h (18_000_000 ms); Hobby caps at
// 45 min. We use the Pro cap so a single sandbox can host a full afternoon's chat.
const TIMEOUT_MS = 5 * 60 * 60 * 1000;

// Spawn modes. Sandbox lifetime is capped at TIMEOUT_MS above on either plan,
// but the LiteLLM virtual-key TTL can outlive the sandbox (unused key just sits
// idle and self-expires; no harm). Budget is the hard cap on what a sandbox can
// spend through the gateway, AND the cap on leak damage if someone exfiltrates
// the virtual key.
const MODES = {
	friendly:  { budgetUsd: 25, keyTtlSeconds: 12 * 60 * 60 }, // 12h TTL / $25 — trusted testers
	marketing: { budgetUsd: 15, keyTtlSeconds:  6 * 60 * 60 }, // 6h TTL / $15 — public link
};

function parseArgs(argv) {
	const args = argv.slice(2);
	let label = null;
	let mode = 'friendly';
	for (let i = 0; i < args.length; i++) {
		if (args[i] === '--mode') { mode = args[++i]; continue; }
		if (args[i].startsWith('--mode=')) { mode = args[i].slice(7); continue; }
		if (!label && !args[i].startsWith('--')) label = args[i];
	}
	if (!label) {
		console.error('Usage: npm run spawn -- <guest-label> [--mode friendly|marketing]');
		process.exit(1);
	}
	if (!MODES[mode]) {
		console.error(`Unknown --mode "${mode}". Valid: ${Object.keys(MODES).join(', ')}`);
		process.exit(1);
	}
	return { label, mode };
}

async function mintVirtualKey({ url, masterKey, label, mode, budgetUsd, keyTtlSeconds }) {
	const res = await fetch(`${url.replace(/\/$/, '')}/key/generate`, {
		method: 'POST',
		headers: { 'Authorization': `Bearer ${masterKey}`, 'Content-Type': 'application/json' },
		body: JSON.stringify({
			max_budget: budgetUsd,
			duration: `${keyTtlSeconds}s`,
			key_alias: `fhv-${mode}-${label}-${Date.now()}`,
			metadata: { mode, label },
		}),
	});
	if (!res.ok) throw new Error(`LiteLLM /key/generate failed: ${res.status} ${await res.text()}`);
	const data = await res.json();
	return data.key; // sk-...
}

async function main() {
	const { label, mode } = parseArgs(process.argv);
	const { budgetUsd, keyTtlSeconds } = MODES[mode];
	if (!existsSync(SNAPSHOT_FILE)) {
		console.error(`No ${SNAPSHOT_FILE}. Run \`npm run build\` first to bake the snapshot.`);
		process.exit(1);
	}
	const { snapshotId } = JSON.parse(readFileSync(SNAPSHOT_FILE, 'utf8'));
	const s = loadSettings();

	console.log(`mode: ${mode}  (key budget $${budgetUsd}, key TTL ${(keyTtlSeconds / 3600).toFixed(1)}h)`);

	// Pick the key the sandbox will carry.
	let injectedKey = s.openrouter.apiKey;
	let injectedBaseUrl = '';
	if (s.litellm.enabled) {
		console.log('Minting per-sandbox virtual key via LiteLLM...');
		injectedKey = await mintVirtualKey({
			url: s.litellm.url,
			masterKey: s.litellm.masterKey,
			label, mode, budgetUsd, keyTtlSeconds,
		});
		injectedBaseUrl = s.litellm.url;
		console.log('  virtual key minted (auto-expires after TTL; revoke on teardown is a TODO)');
	} else {
		console.log('  (LiteLLM not configured — injecting capped operator key directly)');
	}

	console.log(`Spawning sandbox for '${label}' from snapshot ${snapshotId.slice(0, 12)}...`);
	// Only include LITELLM_URL when actually using the gateway — Vercel may reject empty env values.
	const env = { OPENROUTER_MODEL: s.openrouter.model };
	if (injectedBaseUrl) env.LITELLM_URL = injectedBaseUrl;
	let sandbox;
	try {
		sandbox = await Sandbox.create({
			token: s.vercel.token,
			teamId: s.vercel.teamId,
			projectId: s.vercel.projectId,
			timeout: TIMEOUT_MS,
			source: { type: 'snapshot', snapshotId },
			ports: [8080],
			tags: { fhv: 'guest', guest: label, mode },
			env,
		});
	} catch (e) {
		console.error('Sandbox.create failed:');
		console.error('  status:', e?.response?.status);
		console.error('  message:', e?.message);
		console.error('  json:', JSON.stringify(e?.json, null, 2));
		console.error('  text:', (e?.text || '').slice(0, 800));
		throw e;
	}
	console.log(`  sandbox: ${sandbox.name}`);

	// Write the key into ~/.hermes/.env. Pass via env on runCommand so it never
	// appears in the command string or the process list.
	await sandbox.runCommand({
		cmd: 'bash',
		args: [
			'-lc',
			'mkdir -p $HOME/.hermes && printf "OPENROUTER_API_KEY=%s\\n" "$OPENROUTER_API_KEY" >> $HOME/.hermes/.env',
		],
		env: { OPENROUTER_API_KEY: injectedKey },
	});

	// Boot the chat detached — start-chat.sh runs forever; we return immediately
	// with the URL to hand to the guest.
	await sandbox.runCommand({
		cmd: 'bash',
		args: ['-lc', '$HOME/fhv-assets/start-chat.sh > /tmp/start-chat.log 2>&1'],
		detached: true,
	});

	// domain(port) already returns the full https://... URL — don't add a prefix.
	const url = sandbox.domain(8080);
	console.log(`\nGuest '${label}' is ready.`);
	console.log(`  sandbox: ${sandbox.name}`);
	console.log(`  PRESS PLAY (send this link): ${url}`);
	console.log(`  (chat takes ~15-20s to come up on first load)`);
}

main().catch((err) => {
	console.error('FAIL:', err?.message || err);
	process.exit(1);
});
