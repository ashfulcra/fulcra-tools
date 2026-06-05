import './fetch-shim.js';
import { Sandbox } from '@vercel/sandbox';
import { loadSettings } from './config.js';

const s = loadSettings();
const sb = await Sandbox.get({
	token: s.vercel.token, teamId: s.vercel.teamId, projectId: s.vercel.projectId,
	name: process.argv[2],
});
console.log('sandbox:', sb.name, 'status:', sb.status);

async function sh(label, cmd) {
	const r = await sb.runCommand('bash', ['-lc', cmd]);
	const out = (await r.stdout()).slice(0, 1500);
	const err = (await r.stderr()).slice(0, 800);
	console.log(`\n--- ${label} (exit ${r.exitCode}) ---`);
	if (out) console.log(out);
	if (err) console.log('STDERR:', err);
}

await sh('processes', 'ps -ef | grep -E "caddy|hermes|start-chat" | grep -v grep || echo none');
await sh('start-chat.log', 'cat /tmp/start-chat.log 2>&1 || echo no-file');
await sh('dash.log', 'cat /tmp/dash.log 2>&1 || echo no-file');
await sh('skill-fetch.log', 'cat /tmp/skill-fetch.log 2>&1 || echo no-file');
await sh('listening ports', 'ss -ltnp 2>/dev/null || netstat -lnt 2>/dev/null || echo none');
