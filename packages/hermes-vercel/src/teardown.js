/**
 * List or delete guest sandboxes (tagged fhv=guest at spawn).
 */
import './fetch-shim.js';
import { Sandbox } from '@vercel/sandbox';
import { loadSettings } from './config.js';

async function* allSandboxes(creds) {
	const paginator = await Sandbox.list({ ...creds });
	for await (const page of paginator.pages()) {
		for (const sb of page.sandboxes) yield sb;
	}
}

async function main() {
	const args = process.argv.slice(2);
	const mode = args[0];
	if (!['--list', '--delete', '--all'].includes(mode)) {
		console.error('Usage: npm run teardown -- [--list | --delete <name> | --all]');
		process.exit(1);
	}
	const s = loadSettings();
	const creds = { token: s.vercel.token, teamId: s.vercel.teamId, projectId: s.vercel.projectId };

	if (mode === '--list') {
		let n = 0;
		for await (const sb of allSandboxes(creds)) {
			if (sb.tags?.fhv !== 'guest') continue;
			console.log(`${sb.name}  guest=${sb.tags.guest ?? '?'}  status=${sb.status}`);
			n++;
		}
		console.log(`${n} guest sandbox(es).`);
		return;
	}

	if (mode === '--delete') {
		const name = args[1];
		if (!name) { console.error('--delete needs a sandbox name'); process.exit(1); }
		const sb = await Sandbox.get({ ...creds, name });
		await sb.stop();
		console.log(`Stopped ${name}`);
		return;
	}

	if (mode === '--all') {
		let n = 0;
		for await (const sb of allSandboxes(creds)) {
			if (sb.tags?.fhv !== 'guest') continue;
			if (sb.status === 'stopped' || sb.status === 'aborted') continue;
			try {
				const handle = await Sandbox.get({ ...creds, name: sb.name });
				await handle.stop();
				console.log(`Stopped ${sb.name}`);
				n++;
			} catch (e) {
				console.log(`Failed to stop ${sb.name}: ${e?.message || e}`);
			}
		}
		console.log(`Stopped ${n} guest sandbox(es).`);
	}
}

main().catch((err) => {
	console.error('FAIL:', err?.message || err);
	process.exit(1);
});
