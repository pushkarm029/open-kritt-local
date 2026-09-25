import { randomUUID } from 'node:crypto';
import { chmod, mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { PROVIDER_CREDENTIALS_PATH } from './providerCredentials.js';

export const SELF_HOSTED_CONFIG_PATH = join(dirname(PROVIDER_CREDENTIALS_PATH), 'self-hosted.json');
export const SELF_HOSTED_CHECK_REQUEST_PATH = join(
  dirname(PROVIDER_CREDENTIALS_PATH),
  'self-hosted-check-request.json'
);
export const SELF_HOSTED_CHECK_RESULT_PATH = join(dirname(PROVIDER_CREDENTIALS_PATH), 'self-hosted-check-result.json');
export const SELF_HOSTED_MODEL_MAX_LENGTH = 200;
export const SELF_HOSTED_BASE_URL_MAX_LENGTH = 2048;

function emptyConfig() {
  return { baseUrl: '', model: '' };
}

function normalizeConfig(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return emptyConfig();
  return {
    baseUrl: typeof value.baseUrl === 'string' ? value.baseUrl.trim() : '',
    model: typeof value.model === 'string' ? value.model.trim() : '',
  };
}

export function validateSelfHostedConfig(value = {}) {
  const rawBaseUrl =
    typeof (value.baseUrl ?? value.base_url) === 'string' ? String(value.baseUrl ?? value.base_url) : '';
  const rawModel = typeof value.model === 'string' ? value.model : '';
  const baseUrl = rawBaseUrl.trim();
  const model = rawModel.trim();
  const errors = [];
  let parsed;
  try {
    parsed = new URL(baseUrl);
  } catch {
    parsed = null;
  }
  const localHost = parsed && ['localhost', '127.0.0.1', '::1', '[::1]'].includes(parsed.hostname);
  const hasUnsafeBaseUrlCharacters =
    rawBaseUrl !== baseUrl ||
    [...rawBaseUrl].some(
      (character) => /\s/.test(character) || character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127
    );
  if (
    !parsed ||
    hasUnsafeBaseUrlCharacters ||
    !['https:', ...(localHost ? ['http:'] : [])].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    parsed.pathname.replace(/\/+$/, '') !== '/v1'
  ) {
    errors.push({ field: 'baseUrl', message: 'Base URL must use HTTPS (HTTP is allowed only for localhost).' });
  } else if (baseUrl.length > SELF_HOSTED_BASE_URL_MAX_LENGTH) {
    errors.push({
      field: 'baseUrl',
      message: `Base URL must be ${SELF_HOSTED_BASE_URL_MAX_LENGTH} characters or fewer.`,
    });
  }
  const hasUnsafeModelCharacters =
    rawModel !== model ||
    [...rawModel].some((character) => character.charCodeAt(0) < 32 || character.charCodeAt(0) === 127);
  if (!model) errors.push({ field: 'model', message: 'A model is required.' });
  else if (model.length > SELF_HOSTED_MODEL_MAX_LENGTH) {
    errors.push({ field: 'model', message: `Model must be ${SELF_HOSTED_MODEL_MAX_LENGTH} characters or fewer.` });
  } else if (hasUnsafeModelCharacters) {
    errors.push({ field: 'model', message: 'Model contains unsupported control characters.' });
  }
  if (errors.length) {
    const error = new Error('Self-hosted AI configuration is invalid.');
    error.errors = errors;
    error.status = 422;
    throw error;
  }
  return { baseUrl, model };
}

export async function readSelfHostedConfig({ path = SELF_HOSTED_CONFIG_PATH } = {}) {
  try {
    const value = JSON.parse(await readFile(path, 'utf8'));
    return normalizeConfig(value);
  } catch (error) {
    if (error?.code === 'ENOENT' || error instanceof SyntaxError) return emptyConfig();
    throw error;
  }
}

async function atomicWrite(path, value) {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = join(dirname(path), `.${path.split('/').pop()}.${process.pid}.${randomUUID()}.tmp`);
  await writeFile(temporary, `${JSON.stringify(value, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
  await rename(temporary, path);
  await chmod(path, 0o600);
}

export async function writeSelfHostedConfig(value, { path = SELF_HOSTED_CONFIG_PATH } = {}) {
  const config = validateSelfHostedConfig(value);
  await atomicWrite(path, config);
  return config;
}

export async function createSelfHostedCheckRequest({
  configPath = SELF_HOSTED_CONFIG_PATH,
  requestPath = SELF_HOSTED_CHECK_REQUEST_PATH,
} = {}) {
  const config = await readSelfHostedConfig({ path: configPath });
  const validated = validateSelfHostedConfig(config);
  const request = {
    id: randomUUID(),
    createdAt: new Date().toISOString(),
    baseUrl: validated.baseUrl,
    model: validated.model,
  };
  await atomicWrite(requestPath, request);
  return { id: request.id, createdAt: request.createdAt, status: 'pending' };
}

export async function readSelfHostedCheckResult(requestId, { resultPath = SELF_HOSTED_CHECK_RESULT_PATH } = {}) {
  try {
    const result = JSON.parse(await readFile(resultPath, 'utf8'));
    if (!result || result.id !== requestId) return { id: requestId, status: 'pending' };
    return {
      id: requestId,
      status:
        result.status === 'ready' || result.status === 'passed'
          ? 'passed'
          : result.status === 'failed'
            ? 'failed'
            : 'pending',
      ...(result.error || result.message ? { error: String(result.error || result.message).slice(0, 500) } : {}),
      ...(result.code ? { code: String(result.code).slice(0, 100) } : {}),
      ...(result.checkedAt ? { checkedAt: result.checkedAt } : {}),
    };
  } catch (error) {
    if (error?.code === 'ENOENT' || error instanceof SyntaxError) return { id: requestId, status: 'pending' };
    throw error;
  }
}
