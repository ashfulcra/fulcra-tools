/**
 * Build the reusable Vercel Sandbox snapshot.
 *
 * Provisions a fresh sandbox, installs the full stack (uv + fulcra-api + Hermes
 * + caddy + skill + asset files), and snapshots it. Writes the snapshot ID to
 * .snapshot.json so spawn.js can find it.
 */
import './fetch-shim.js';
import { writeFileSync, readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Sandbox } from '@vercel/sandbox';
import { loadSettings } from './config.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');
const ASSETS = join(ROOT, 'assets');
const SNAPSHOT_FILE = join(ROOT, '.snapshot.json');

const HERMES_INSTALL =
	'curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh ' +
	'| sudo HERMES_HOME=$HOME/.hermes bash -s -- --skip-browser --skip-setup';

// Caddy v2 static binary (same source as Daytona).
const CADDY_INSTALL =
	'sudo curl -L -o /usr/local/bin/caddy "https://caddyserver.com/api/download?os=linux&arch=amd64" ' +
	'&& sudo chmod +x /usr/local/bin/caddy && caddy version';

// Amazon Linux 2023 — dnf, not apt. procps-ng provides ps/pkill (note the -ng).
const SYSTEM_DEPS = 'sudo dnf install -y git procps-ng socat ca-certificates';

const INSTALL_STEPS = [
	['system deps (git, procps-ng, socat, ca-certificates)', SYSTEM_DEPS],
	['uv', 'curl -LsSf https://astral.sh/uv/install.sh | sh'],
	// --python 3.12 pins via uv-managed Python so we get the latest fulcra-api
	// (AL2023's system Python 3.9 would pull a stale version with broken entry points).
	['fulcra-api CLI (Python 3.12)', '$HOME/.local/bin/uv tool install --python 3.12 fulcra-api'],
	['hermes', HERMES_INSTALL],
	['caddy', CADDY_INSTALL],
	['onboarding skill (fallback copy)',
		'git clone --depth 1 https://github.com/fulcradynamics/agent-skills /tmp/agent-skills ' +
		'&& mkdir -p $HOME/.hermes/skills/fulcra ' +
		'&& cp -r /tmp/agent-skills/skills/fulcra-onboarding $HOME/.hermes/skills/fulcra/fulcra-onboarding ' +
		'&& rm -rf /tmp/agent-skills'],
	// /usr/local/bin/hermes via absolute path — Vercel's bash-lc doesn't reliably
	// have /usr/local/bin on PATH for the vercel-sandbox user.
	['hermes config (provider, model)',
		'/usr/local/bin/hermes config set model.provider openrouter ' +
		'&& /usr/local/bin/hermes config set model.default anthropic/claude-sonnet-4.5'],
	['mkdir asset dir', 'mkdir -p $HOME/fhv-assets'],
];

// $HOME for the vercel-sandbox user is /home/vercel-sandbox/ (NOT /vercel/sandbox/
// — that's the app working dir on Vercel runtimes, not the user home). start-chat.sh
// references $HOME, which expands to /home/vercel-sandbox/ at runtime; so we have to
// upload to the same path.
const ASSET_UPLOADS = [
	{ local: 'hermes/SOUL.md',       remote: '/home/vercel-sandbox/.hermes/SOUL.md',      chmod: null },
	{ local: 'hermes/AGENTS.md',     remote: '/home/vercel-sandbox/.hermes/AGENTS.md',    chmod: null },
	{ local: 'caddy/Caddyfile',      remote: '/home/vercel-sandbox/fhv-assets/Caddyfile', chmod: null },
	{ local: 'hermes/start-chat.sh', remote: '/home/vercel-sandbox/fhv-assets/start-chat.sh', chmod: '755' },
];

async function main() {
	const s = loadSettings();
	console.log('Creating base sandbox (will be snapshotted then stopped)...');
	const sandbox = await Sandbox.create({
		token: s.vercel.token,
		teamId: s.vercel.teamId,
		projectId: s.vercel.projectId,
		timeout: 30 * 60 * 1000, // 30 min — enough to install everything
		runtime: 'node22',
	});
	console.log(`  base sandbox: ${sandbox.name}  region=${sandbox.region}`);

	try {
		console.log('\nInstalling stack:');
		for (const [label, cmd] of INSTALL_STEPS) {
			process.stdout.write(`  - ${label}... `);
			const r = await sandbox.runCommand('bash', ['-lc', cmd]);
			if (r.exitCode !== 0) {
				const err = ((await r.stderr()) || (await r.stdout())).slice(0, 1500);
				console.log('FAIL');
				throw new Error(`step "${label}" failed (exit ${r.exitCode}):\n${err}`);
			}
			console.log('ok');
		}

		console.log('\nUploading assets:');
		for (const { local, remote, chmod } of ASSET_UPLOADS) {
			const data = readFileSync(join(ASSETS, local), 'utf8');
			await sandbox.fs.writeFile(remote, data);
			if (chmod) await sandbox.runCommand('bash', ['-lc', `chmod ${chmod} ${remote}`]);
			console.log(`  - ${local} → ${remote}${chmod ? ` (chmod ${chmod})` : ''}`);
		}

		console.log('\nSnapshotting (takes ~30-60s)...');
		const snap = await sandbox.snapshot();
		console.log(`  snapshot id: ${snap.snapshotId}`);
		console.log(`  status:      ${snap.status}`);
		console.log(`  size:        ${(snap.sizeBytes / 1024 / 1024).toFixed(1)} MB`);

		writeFileSync(SNAPSHOT_FILE, JSON.stringify({
			snapshotId: snap.snapshotId,
			createdAt: new Date().toISOString(),
			builtFromSandbox: sandbox.name,
		}, null, 2) + '\n');
		console.log(`\nWrote ${SNAPSHOT_FILE}. spawn.js will read it.`);
	} finally {
		console.log('\nStopping base sandbox...');
		await sandbox.stop().catch((e) => console.error('  stop failed:', e?.message));
		console.log('done.');
	}
}

main().catch((err) => {
	console.error('FAIL:', err?.message || err);
	process.exit(1);
});
