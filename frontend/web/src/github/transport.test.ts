import { afterEach, describe, expect, it, vi } from 'vitest';
import { webcrypto } from 'node:crypto';

afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); vi.resetModules(); vi.useRealTimers(); });

async function setup() {
  vi.stubEnv('VITE_ARGUS_HOST', 'github');
  vi.stubGlobal('crypto', webcrypto);
  vi.stubGlobal('document', { hidden: false });
  const localStorage = { setItem: vi.fn(), getItem: vi.fn() };
  vi.stubGlobal('localStorage', localStorage);
  const values = new Map<string, string>();
  const sessionStorage = { setItem: vi.fn((key: string, value: string) => values.set(key, value)), getItem: vi.fn((key: string) => values.get(key) ?? null) };
  vi.stubGlobal('sessionStorage', sessionStorage);
  const fetch = vi.fn(async (url: string, _init?: RequestInit) => {
    if (url.endsWith('/user')) return Response.json({ login: 'maintainer' });
    if (url.endsWith('/repos/owner/repo')) return Response.json({ default_branch: 'github-host', permissions: { push: true } });
    if (url.includes('/dispatches')) return new Response(null, { status: 204 });
    if (url.includes('/contents/views/')) return Response.json({ status: 200, content_type: 'application/json', body_b64: btoa('{"snapshot":true}') });
    if (url.includes('/contents/api/')) return Response.json({ status: 200, content_type: 'application/json', body_b64: btoa('{"completed":true}') });
    return Response.json({ ok: true });
  });
  vi.stubGlobal('fetch', fetch);
  const transport = await import('./transport');
  await transport.connect('owner/repo', 'test-only-secret');
  return { transport, fetch, localStorage, sessionStorage };
}

describe('GitHub transport', () => {
  it('keeps the credential in memory and sends it only to GitHub API calls', async () => {
    const { transport, fetch, localStorage } = await setup();
    await transport.hostFetch('/api/meta');
    expect(localStorage.setItem).not.toHaveBeenCalled();
    expect(fetch.mock.calls.every(([url]) => url.startsWith('https://api.github.com/'))).toBe(true);
    const init = fetch.mock.calls.at(-1)![1]!;
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer test-only-secret');
    expect(new Headers(init.headers).get('Accept')).toBe('application/vnd.github.raw+json');
    transport.disconnect();
    expect(transport.hostConnection()).toBeUndefined();
  });

  it('does not report completed when GitHub merely accepts a dispatch', async () => {
    vi.useFakeTimers();
    const { transport } = await setup();
    let completed = false;
    const result = transport.hostFetch('/api/projects/p/identity', { method: 'POST', body: '{"text":"identity"}' })
      .then(response => { completed = true; return response; });
    await vi.advanceTimersByTimeAsync(1);
    expect(completed).toBe(false);
    expect(transport.hostPending()[0].state).toBe('accepted');
    await vi.advanceTimersByTimeAsync(8000);
    expect(await (await result).json()).toEqual({ completed: true });
    expect(transport.hostPending()[0].state).toBe('completed');
  });

  it('recovers receipt tracking after reload without persisting credentials or request bodies', async () => {
    vi.useFakeTimers();
    const { transport, sessionStorage } = await setup();
    const request = transport.hostFetch('/api/projects/p/identity', { method: 'POST', body: '{"text":"private instruction"}' }).catch(error => error);
    await vi.advanceTimersByTimeAsync(1);
    const id = transport.hostPending()[0].id;
    const stored = sessionStorage.getItem('argus-pending:owner/repo')!;
    expect(stored).toContain(id);
    expect(stored).not.toContain('test-only-secret');
    expect(stored).not.toContain('private instruction');
    transport.disconnect();
    await vi.advanceTimersByTimeAsync(8000);
    expect(await request).toBeInstanceOf(Error);
    await transport.connect('owner/repo', 'fresh-test-secret');
    expect(transport.hostPending()[0].id).toBe(id);
    await transport.refreshPending();
    expect(transport.hostPending()[0].state).toBe('completed');
  });

  it('tracks control application separately from successful workflow dispatch', async () => {
    const { transport, fetch } = await setup();
    fetch.mockImplementation(async (url: string) => {
      if (url.includes('/dispatches')) return Response.json({ workflow_run_id: 123 });
      if (url.includes('/contents/commands/')) return Response.json({ status: 'applied' });
      return Response.json({});
    });
    await transport.command('pause', {});
    expect(transport.hostPending()[0].state).toBe('accepted');
    expect(transport.hostPending()[0].url).toContain('/actions/runs/123');
    await transport.refreshPending();
    expect(transport.hostPending()[0].state).toBe('completed');
  });

  it('restarts stale history cursors from a real persisted page without dispatching work', async () => {
    const { transport, fetch } = await setup();
    fetch.mockImplementation(async (url: string) => {
      if (url.includes('/contents/views/index.json')) return Response.json({ views: {} });
      return Response.json({ status: 200, content_type: 'application/json',
        body_b64: btoa(JSON.stringify({ events: [{ id: 'retained-evidence' }], history_loading: true })) });
    });
    const response = await transport.hostFetch('/api/projects/p/map-history?after=old%3A500&task_after=old-task');
    expect(await response.json()).toEqual({ events: [{ id: 'retained-evidence' }], history_loading: true, reset_history: true });
    expect(fetch.mock.calls.some(([url]) => url.includes('/dispatches'))).toBe(false);
  });
});
