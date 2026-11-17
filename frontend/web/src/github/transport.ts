/** GitHub is the transport and durable store. Credentials remain in memory. */
export const githubMode = import.meta.env.VITE_ARGUS_HOST === 'github';
type Receipt = { status: number; content_type: string; body_b64: string; path?: string };
export type Connection = { repository: string; branch: string; defaultBranch: string; login: string; canWrite: boolean };
export type Pending = { id: string; path: string; state: 'accepted' | 'waiting' | 'completed' | 'failed'; started: number };
let connection: Connection | undefined;
let credential = '';
const cache = new Map<string, { etag: string; value: unknown; at: number }>();
const pending = new Map<string, Pending>();
const listeners = new Set<() => void>();
const inflight = new Map<string, Promise<Response>>();
const apiRoot = 'https://api.github.com';
const encoder = new TextEncoder();
const decoder = new TextDecoder();

export function subscribeHost(fn: () => void) { listeners.add(fn); return () => { listeners.delete(fn); }; }
function notify() { for (const fn of listeners) fn(); }
export function hostConnection() { return connection; }
export function hostPending() { return [...pending.values()]; }
export function disconnect() { credential = ''; connection = undefined; cache.clear(); notify(); }

async function github(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (!headers.has('Accept')) headers.set('Accept', 'application/vnd.github+json');
  headers.set('X-GitHub-Api-Version', '2022-11-28');
  if (credential) headers.set('Authorization', `Bearer ${credential}`);
  if (init.body) headers.set('Content-Type', 'application/json');
  const response = await fetch(apiRoot + path, { ...init, headers });
  if (response.status !== 304 && !response.ok) {
    const body = await response.json().catch(() => ({})) as { message?: string };
    throw new Error(`GitHub ${response.status}: ${body.message || response.statusText}`);
  }
  return response;
}

export async function connect(repository: string, token: string): Promise<Connection> {
  if (!/^[\w.-]+\/[\w.-]+$/.test(repository)) throw new Error('Use owner/repository.');
  credential = token.trim();
  try {
    const repo = await (await github(`/repos/${repository}`)).json();
    const user = credential ? await (await github('/user')).json() : { login: 'public reader' };
    connection = { repository, branch: 'argus-state', defaultBranch: repo.default_branch,
      login: user.login, canWrite: Boolean(repo.permissions?.push) };
    cache.clear();
    notify();
    return connection;
  } catch (error) { credential = ''; throw error; }
}

function current(): Connection {
  if (!connection) throw new Error('Connect this browser to a GitHub repository first.');
  return connection;
}

export async function readState<T>(path: string, fresh = false): Promise<T> {
  const host = current();
  const key = host.repository + ':' + path;
  const previous = cache.get(key);
  if (!fresh && previous && Date.now() - previous.at < 10_000) return previous.value as T;
  const response = await github(`/repos/${host.repository}/contents/${path}?ref=${host.branch}`, {
    headers: { Accept: 'application/vnd.github.raw+json', ...(previous?.etag ? { 'If-None-Match': previous.etag } : {}) },
  });
  if (response.status === 304 && previous) { previous.at = Date.now(); return previous.value as T; }
  const value = await response.json();
  // Some GitHub media negotiations return a Contents object.
  const decoded = value.encoding === 'base64' && value.content
    ? JSON.parse(decoder.decode(Uint8Array.from(atob(value.content.replace(/\s/g, '')), c => c.charCodeAt(0)))) : value;
  cache.set(key, { etag: response.headers.get('etag') || '', value: decoded, at: Date.now() });
  return decoded as T;
}

export async function command(kind: string, payload: unknown): Promise<void> {
  const host = current();
  if (!host.canWrite || !credential) throw new Error('This action requires a repository maintainer token.');
  await github(`/repos/${host.repository}/actions/workflows/argus-intake.yml/dispatches`, {
    method: 'POST', body: JSON.stringify({ ref: host.defaultBranch,
      inputs: { command: kind, payload: JSON.stringify(payload) } }),
  });
}

function normalize(path: string): string {
  const url = new URL(path, 'https://argus.invalid');
  url.searchParams.delete('prewarm');
  url.searchParams.sort();
  return url.pathname + (url.searchParams.size ? '?' + url.searchParams.toString() : '');
}
async function hash(path: string) {
  return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(normalize(path)))))
    .map(x => x.toString(16).padStart(2, '0')).join('');
}
function responseFrom(receipt: Receipt) {
  return new Response(Uint8Array.from(atob(receipt.body_b64), char => char.charCodeAt(0)), {
    status: receipt.status, headers: { 'Content-Type': receipt.content_type },
  });
}
const pause = (ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms));

async function enqueue(path: string, init: RequestInit): Promise<Response> {
  const method = (init.method || 'GET').toUpperCase();
  const id = 'ui-' + crypto.randomUUID();
  let body: unknown = undefined;
  let multipart: unknown = undefined;
  let upload_id: string | undefined;
  if (init.body instanceof FormData) {
    const parts: { name: string; filename: string; type: string; path: string }[] = [];
    const blobs: { path: string; mode: string; type: string; sha: string }[] = [];
    const host = current();
    for (const [name, value] of init.body.entries()) {
      if (!(value instanceof File)) throw new Error('This upload accepts files only.');
      if (value.size > 40 * 1024 * 1024) throw new Error('A repository attachment may be at most 40 MiB. Use a configured external artifact store for larger datasets.');
      const bytes = new Uint8Array(await value.arrayBuffer());
      let binary = '';
      for (let offset = 0; offset < bytes.length; offset += 8192)
        binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
      const blob = await (await github(`/repos/${host.repository}/git/blobs`, {
        method: 'POST', body: JSON.stringify({ encoding: 'base64', content: btoa(binary) }),
      })).json();
      const file = `${parts.length}.bin`;
      blobs.push({ path: `uploads/${id}/${file}`, mode: '100644', type: 'blob', sha: blob.sha });
      parts.push({ name, filename: value.name, type: value.type, path: file });
    }
    for (let attempt = 0; ; attempt++) {
      const ref = await (await github(`/repos/${host.repository}/git/ref/heads/${host.branch}`)).json();
      const parent = await (await github(`/repos/${host.repository}/git/commits/${ref.object.sha}`)).json();
      const tree = await (await github(`/repos/${host.repository}/git/trees`, {
        method: 'POST', body: JSON.stringify({ base_tree: parent.tree.sha, tree: blobs }),
      })).json();
      const commit = await (await github(`/repos/${host.repository}/git/commits`, {
        method: 'POST', body: JSON.stringify({ message: `Stage authenticated attachment ${id}`, tree: tree.sha, parents: [ref.object.sha] }),
      })).json();
      try {
        await github(`/repos/${host.repository}/git/refs/heads/${host.branch}`, {
          method: 'PATCH', body: JSON.stringify({ sha: commit.sha, force: false }),
        });
        break;
      } catch (error) {
        if (attempt >= 4 || !/GitHub (409|422):/.test(String(error))) throw error;
      }
    }
    multipart = parts;
    upload_id = id;
  } else if (typeof init.body === 'string') body = JSON.parse(init.body);
  const payload = { request_id: id, method, path: normalize(path), body, multipart, upload_id };
  if (encoder.encode(JSON.stringify(payload)).length > 60_000) throw new Error('Request is too large for Actions dispatch; attach the content in the repository.');
  await command('api', payload);
  pending.set(id, { id, path, state: 'accepted', started: Date.now() });
  notify();
  for (;;) {
    // Cancelling a subscription does not cancel an already accepted command.
    if (init.signal?.aborted) throw new DOMException(`Request ${id} remains queued in GitHub`, 'AbortError');
    await pause(document.hidden ? 30_000 : 8_000);
    try {
      const receipt = await readState<Receipt>(`api/${id}.json`, true);
      pending.set(id, { ...pending.get(id)!, state: receipt.status < 400 ? 'completed' : 'failed' });
      cache.clear(); notify();
      return responseFrom(receipt);
    } catch (error) {
      if (!(error instanceof Error) || !error.message.startsWith('GitHub 404:')) throw error;
      pending.set(id, { ...pending.get(id)!, state: 'waiting' }); notify();
      // Retain the receipt id for reopening after a long Actions queue.
      if (Date.now() - pending.get(id)!.started > 90 * 60_000)
        throw new Error(`Still waiting for ${id}. Check Actions; the request has not been reported as complete.`);
    }
  }
}

export async function hostFetch(path: string, init: RequestInit = {}): Promise<Response> {
  if (!githubMode || !path.startsWith('/api/')) return fetch(path, init);
  const method = (init.method || 'GET').toUpperCase();
  if (method !== 'GET') return enqueue(path, init);
  const key = await hash(path);
  try {
    const receipt = await readState<Receipt>(`views/${key}.json`);
    if (receipt.status < 500) return responseFrom(receipt);
  } catch (error) {
    if (!(error instanceof Error) || !error.message.startsWith('GitHub 404:')) throw error;
  }
  if (!current().canWrite) throw new Error('This view has not been generated yet. A maintainer can request it through Actions.');
  if (!inflight.has(key)) inflight.set(key, enqueue(path, init).finally(() => inflight.delete(key)));
  return (await inflight.get(key)!).clone();
}

export function hostStream<T>(sid: string, onEvent: (event: T) => void, onOpen?: () => void): () => void {
  let closed = false;
  let timer: ReturnType<typeof setTimeout>;
  const seen = new Set<string>();
  const poll = async () => {
    try {
      const response = await hostFetch(`/api/projects/${encodeURIComponent(sid)}/events?limit=500&view=ui`);
      const body = await response.json() as { events?: T[] };
      for (const event of body.events || []) {
        const key = JSON.stringify(event);
        if (!seen.has(key)) { seen.add(key); onEvent(event); }
      }
      if (seen.size > 2000) { const recent = [...seen].slice(-1000); seen.clear(); recent.forEach(key => seen.add(key)); }
      onOpen?.();
    } finally { if (!closed) timer = setTimeout(poll, document.hidden ? 60_000 : 15_000); }
  };
  void poll().catch(() => {});
  return () => { closed = true; clearTimeout(timer); };
}
