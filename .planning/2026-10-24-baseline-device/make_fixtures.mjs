import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { generateKeyPairSync, createHash, sign } from 'node:crypto';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = JSON.parse(fs.readFileSync(path.join(here, 'candidate-source.json')));
const app = path.join(source.snapshot, 'android-app');
const { canonicalArtifactMessage } = await import(pathToFileURL(path.join(app, 'src/update/trust.ts')));
const pair = generateKeyPairSync('rsa', { modulusLength: 2048 });
const pub = pair.publicKey.export({ format: 'jwk' });
const data = { source_commit: source.commit, source_tree: source.tree,
  key: { key_id: 'review-device-only', algorithm: 'rsa-sha256', n: pub.n, e: pub.e }, targets: {} };
for (const kind of ['good', 'bad', 'timeout']) {
  const body = Buffer.from(JSON.stringify({ workflow: 'review',
    workflow_def: { workflow_id: 'review', version: '1.0.0', nodes: kind === 'timeout'
      ? [{ node_id: 'first', tool: 'hang', max_retries: 2 }, { node_id: 'after', tool: 'after', deps: ['first'] }]
      : [{ node_id: 'time', tool: 'local_time', args: { label: kind } }] },
    health_probe: { input: {}, expect_output_contains: kind === 'bad' ? 'MUST_NOT_MATCH_07466FB' : kind === 'good' ? 'good' : '' },
  }));
  const target = { release_id: 'review_' + kind, version: kind === 'good' ? 'v1.0.0' : kind === 'bad' ? 'v2.0.0' : 'v3.0.0',
    artifact_digest: '', signed_by: 'review-device-only', signature_ok: true,
    rollback_target: kind === 'good' ? null : 'review_good', min_app_version: '0.1.0', channel: 'review',
    bundle_kind: 'workflow', delivery_mode: 'capability_slot', gray_percent: 100,
    artifact_url: 'http://127.0.0.1:8334/' + kind + '.bundle', artifact_size: body.length,
    artifact_sha256: createHash('sha256').update(body).digest('hex'), artifact_signature: '',
    signing_key_id: data.key.key_id, signature_schema: 'evoagent.release.signing/1',
    required_tool_contracts: [], required_permissions: [] };
  target.artifact_digest = 'sha256:' + target.artifact_sha256;
  target.artifact_signature = sign('sha256', Buffer.from(canonicalArtifactMessage({ schema: target.signature_schema,
    release_id: target.release_id, version: target.version, artifact_sha256: target.artifact_sha256,
    artifact_size: target.artifact_size, signing_key_id: target.signing_key_id, signature: '' })), pair.privateKey).toString('hex');
  data.targets[kind] = target;
  fs.writeFileSync(path.join(here, kind + '.bundle'), body);
}
// No private key is written to disk. These public fixtures are scoped to this harness.
fs.writeFileSync(path.join(here, 'device-fixtures.json'), JSON.stringify(data, null, 2));
fs.copyFileSync(path.join(here, 'device-fixtures.json'), path.join(app, 'device-review-fixtures.json'));
fs.copyFileSync(path.join(here, 'deviceReview.tsx'), path.join(app, 'deviceReview.tsx'));
console.log(JSON.stringify({ source_commit: source.commit, fixture_key_id: data.key.key_id, private_key_saved: false }));
