export type ActivationResumeStep = {
  key: string;
  done?: boolean;
  running?: boolean;
  failed?: boolean;
};

export type ActivationResumeSnapshot = {
  steps?: ActivationResumeStep[];
  smoke?: null | {
    job_id?: string | null;
    state?: string | null;
    error_code?: string;
    error_message?: string;
  };
  smoke_job_completed?: boolean;
  max_runtime_minutes?: number;
  runtime_limit_minutes?: number;
  runtime_limit_seconds?: number;
  max_runtime_seconds?: number;
  runtime_seconds?: number;
  [key: string]: unknown;
};

const ACTIVE_SMOKE_STATES = new Set([
  'pending', 'queued', 'validating', 'waiting_for_capacity', 'allocating', 'preparing',
  'loading', 'running', 'uploading', 'cleaning_up', 'cleanup_pending',
]);
const COMPLETED_SMOKE_STATES = new Set(['completed', 'succeeded', 'success']);
const FAILED_SMOKE_STATES = new Set(['failed', 'cancelled', 'canceled', 'error']);

export function getActivationStep(
  snapshot: ActivationResumeSnapshot | null | undefined,
  key: string,
): ActivationResumeStep | undefined {
  return snapshot?.steps?.find((step) => step.key === key);
}

function smokeState(snapshot: ActivationResumeSnapshot | null | undefined): string {
  return String(snapshot?.smoke?.state || '').toLowerCase();
}

function smokeJobId(snapshot: ActivationResumeSnapshot | null | undefined): string {
  return String(snapshot?.smoke?.job_id || '');
}

export function isSmokePolling(snapshot: ActivationResumeSnapshot | null | undefined): boolean {
  return Boolean(smokeJobId(snapshot)) && isSmokeActiveState(smokeState(snapshot));
}

export function isSmokeActiveState(state: string | null | undefined): boolean {
  return ACTIVE_SMOKE_STATES.has(String(state || '').toLowerCase());
}

function transientStatusError(error: unknown): boolean {
  if (error instanceof TypeError || (error instanceof Error && error.name === 'AbortError')) return true;
  if (!error || typeof error !== 'object') return false;
  const status = (error as { status?: unknown }).status;
  return typeof status === 'number' && (status === 0 || status === 408 || status === 429 || status >= 500);
}

/** Retry only read-only status requests. Never retry the paid smoke submission. */
export async function readSmokeStatusWithRetry<T>(
  read: () => Promise<T>,
  isCurrent: () => boolean,
  onRetry: (attempt: number) => void,
  wait: (ms: number) => Promise<void>,
): Promise<T | null> {
  const maxAttempts = 4;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    if (!isCurrent()) return null;
    try {
      return await read();
    } catch (error) {
      if (!isCurrent()) return null;
      if (!transientStatusError(error)) throw error;
      if (attempt === maxAttempts) {
        throw new Error('暂时无法读取冒烟状态，任务可能仍在运行。请稍后刷新，不要重复启动。');
      }
      onRetry(attempt);
      await wait(Math.min(3000 * 2 ** (attempt - 1), 12000));
    }
  }
  return null;
}

export function isSmokeCompleted(snapshot: ActivationResumeSnapshot | null | undefined): boolean {
  return Boolean(snapshot?.smoke_job_completed) || COMPLETED_SMOKE_STATES.has(smokeState(snapshot));
}

/** A job that can be resumed or accepted; failed jobs may be retried as a new smoke. */
export function hasExistingSmoke(snapshot: ActivationResumeSnapshot | null | undefined): boolean {
  const jobId = smokeJobId(snapshot);
  if (!jobId || FAILED_SMOKE_STATES.has(smokeState(snapshot))) return false;
  return true;
}

export function shouldCreateSmoke(snapshot: ActivationResumeSnapshot | null | undefined): boolean {
  return !hasExistingSmoke(snapshot);
}

export function shouldAcceptSmoke(snapshot: ActivationResumeSnapshot | null | undefined): boolean {
  return hasExistingSmoke(snapshot)
    && isSmokeCompleted(snapshot)
    && getActivationStep(snapshot, 'accept_smoke')?.done !== true;
}

export function terminalSmokeJobId(snapshot: ActivationResumeSnapshot | null | undefined): string | null {
  const jobId = smokeJobId(snapshot);
  const state = smokeState(snapshot);
  return jobId && (COMPLETED_SMOKE_STATES.has(state) || FAILED_SMOKE_STATES.has(state)) ? jobId : null;
}

/** Prefer the server's limit; keep this tolerant while the API field is coordinated. */
export function getSmokeRuntimeMinutes(snapshot: ActivationResumeSnapshot | null | undefined): number | null {
  const source = snapshot || {};
  const minutes = [source.max_runtime_minutes, source.runtime_limit_minutes]
    .find((value) => typeof value === 'number' && Number.isFinite(value) && value > 0);
  if (typeof minutes === 'number') return minutes;

  const seconds = [source.runtime_limit_seconds, source.max_runtime_seconds, source.runtime_seconds]
    .find((value) => typeof value === 'number' && Number.isFinite(value) && value > 0);
  return typeof seconds === 'number' ? seconds / 60 : null;
}

export function appendActivationLog(log: string[], line: string, maxEntries = 40): string[] {
  if (log[log.length - 1] === line) return log;
  const next = [...log, line];
  return next.length > maxEntries ? next.slice(next.length - maxEntries) : next;
}

export type ActivationInputSpec = {
  key: string;
  label?: string;
  description?: string;
  type?: string;
  required?: boolean;
};

export function missingActivationInputs(inputs: ActivationInputSpec[], values: Record<string, string>): string[] {
  return inputs
    .filter((input) => input.required && !values[input.key])
    .map((input) => input.label || input.key);
}

export function activationSmokeParams(inputs: ActivationInputSpec[], values: Record<string, string>): { params: Record<string, string | number>; assetIds: string[] } {
  const params: Record<string, string | number> = {};
  const assetIds: string[] = [];
  for (const input of inputs) {
    const value = values[input.key];
    if (!value) continue;
    if (input.type === 'number' || input.type === 'integer') {
      params[input.key] = Number(value);
    } else {
      params[input.key] = value;
      if (!assetIds.includes(value)) assetIds.push(value);
    }
  }
  return { params, assetIds };
}

export type ActivationWatch = {
  workflowId: string;
  projectId: string;
  jobId: string;
  state: string;
  maxRuntimeMinutes: number | null;
};

const WATCH_KEY = 'studio-activation-watch';

export function readActivationWatch(): Record<string, ActivationWatch> {
  try {
    const raw = sessionStorage.getItem(WATCH_KEY);
    const parsed = raw ? JSON.parse(raw) as unknown : {};
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return parsed as Record<string, ActivationWatch>;
  } catch {
    return {};
  }
}

export function writeActivationWatch(value: Record<string, ActivationWatch>): void {
  try { sessionStorage.setItem(WATCH_KEY, JSON.stringify(value)); } catch { /* storage can be unavailable */ }
}

export function smokeIdempotencyKey(
  workflowId: string, projectId: string, assetIds: string[], failedJobId?: string | null,
): string {
  // A failed attempt needs a new key, while retries of an uncertain request
  // following that same failure must keep using the same key.
  const signature = `${workflowId}:${projectId}:${[...assetIds].sort().join(',')}${failedJobId ? `:retry:${failedJobId}` : ''}`;
  const storageKey = `studio-smoke-idem:${signature}`;
  try {
    const existing = sessionStorage.getItem(storageKey);
    if (existing) return existing;
    const created = `smoke-${workflowId}-${typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : Date.now()}`;
    sessionStorage.setItem(storageKey, created);
    return created;
  } catch {
    return `smoke-${workflowId}-${Date.now()}`;
  }
}
