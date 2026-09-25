import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import {
  createSelfHostedCheckRequest,
  readSelfHostedCheckResult,
  readSelfHostedConfig,
  validateSelfHostedConfig,
  writeSelfHostedConfig,
} from '../src/lib/selfHostedConfig.js';

test('self-hosted settings reject URLs and model IDs the engine cannot use', () => {
  const model = 'fixture-model';
  for (const baseUrl of [
    'http://remote.example/v1',
    'https://user:key@remote.example/v1',
    'https://remote.example/v1?key=private',
    'https://remote.example/v1#fragment',
    'https://remote.example/chat/completions',
  ]) {
    assert.throws(() => validateSelfHostedConfig({ baseUrl, model }), { status: 422 });
  }
  assert.throws(() => validateSelfHostedConfig({ baseUrl: 'https://remote.example/v1', model: 'bad\tmodel' }), {
    status: 422,
  });
  assert.deepEqual(validateSelfHostedConfig({ baseUrl: 'http://127.0.0.1:9000/v1', model }), {
    baseUrl: 'http://127.0.0.1:9000/v1',
    model,
  });
});

test('connection checks bind results to their request and persist only public settings', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'self-hosted-config-'));
  try {
    const configPath = join(directory, 'config.json');
    const requestPath = join(directory, 'request.json');
    const resultPath = join(directory, 'result.json');
    const expected = { baseUrl: 'https://remote.example/v1', model: 'fixture-model' };
    await writeSelfHostedConfig({ ...expected, credential: 'must-not-be-persisted' }, { path: configPath });
    assert.deepEqual(await readSelfHostedConfig({ path: configPath }), expected);
    assert.ok(!(await readFile(configPath, 'utf8')).includes('must-not-be-persisted'));
    const request = await createSelfHostedCheckRequest({ configPath, requestPath });
    await writeFile(resultPath, JSON.stringify({ id: 'older-request', status: 'passed' }));
    assert.equal((await readSelfHostedCheckResult(request.id, { resultPath })).status, 'pending');
    await writeFile(resultPath, JSON.stringify({ id: request.id, status: 'passed' }));
    assert.equal((await readSelfHostedCheckResult(request.id, { resultPath })).status, 'passed');
    assert.deepEqual(JSON.parse(await readFile(requestPath, 'utf8')).baseUrl, expected.baseUrl);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});
