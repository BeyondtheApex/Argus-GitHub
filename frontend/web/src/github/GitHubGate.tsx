import { useEffect, useState, type ReactNode } from 'react';
import { command, connect, disconnect, hostConnection, hostPending, readState, refreshPending, subscribeHost } from './transport';

type Project = { id: string; objective: string; status: string; goal_revision: number; runtime_sha: string };
type Intake = { number: number; title: string; body: string; content_hash: string };

function savedRepository(): string {
  try { return localStorage.getItem('argus-repository') || import.meta.env.VITE_GITHUB_REPOSITORY || ''; }
  catch { return import.meta.env.VITE_GITHUB_REPOSITORY || ''; }
}

export default function GitHubGate({ children }: { children: ReactNode }) {
  const [repository, setRepository] = useState(savedRepository);
  const [token, setToken] = useState('');
  const [objective, setObjective] = useState('');
  const [project, setProject] = useState<Project | null>(null);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [intake, setIntake] = useState<Intake[]>([]);
  useEffect(() => subscribeHost(() => setRevision(value => value + 1)), []);
  const host = hostConnection();
  useEffect(() => {
    if (!host) return;
    let stopped = false;
    const refresh = () => Promise.all([readState<Project>('host/project.json', true), refreshPending(),
      readState<Record<string, Intake>>('unapproved/index.json', true).then(rows => { if (!stopped) setIntake(Object.values(rows)); }).catch(reason => {
        if (!String(reason).includes('404')) throw reason;
      })])
      .then(([value]) => { if (!stopped) setProject(value); })
      .catch(reason => { if (!stopped && !String(reason).includes('404')) setError(String(reason)); });
    void refresh();
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 15_000);
    return () => { stopped = true; clearInterval(timer); };
  }, [host]);

  async function login() {
    setBusy(true); setError('');
    try {
      await connect(repository.trim(), token);
      try { localStorage.setItem('argus-repository', repository.trim()); } catch { /* optional preference */ }
      setToken('');
    }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  async function control(kind: string, payload = {}) {
    setBusy(true); setError('');
    try { await command(kind, payload); setNotice('GitHub 已接受请求，正在等待 Action 更新仓库状态。'); }
    catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }
  const jobs = hostPending().filter(row => row.state !== 'completed');
  if (!host) return <main className="min-h-screen bg-paper text-ink grid place-items-center p-8">
    <section className="w-full max-w-lg space-y-5 rounded-xl border border-line p-7 shadow-sm">
      <h1 className="text-2xl font-semibold">Argus · GitHub</h1>
      <p className="text-sm">仓库就是项目的长期身份、记忆和工作记录。连接后进入原生 Argus 工作台。</p>
      <label className="block text-sm">GitHub 仓库<input className="mt-2 w-full rounded border p-2 text-black" value={repository} onChange={event => setRepository(event.target.value)} placeholder="owner/repository" /></label>
      <label className="block text-sm">Fine-grained PAT（仅保存在本页内存）<input type="password" autoComplete="off" className="mt-2 w-full rounded border p-2 text-black" value={token} onChange={event => setToken(event.target.value)} /></label>
      <p className="text-xs">读取公开项目可留空。操作需要该仓库的 Actions 写入权限；附件上传还需要 Contents 写入权限。页面关闭后需要重新授权。Device Flow 尚未启用。</p>
      <a className="text-sm underline" href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noreferrer">在 GitHub 创建仅限此仓库的 Token</a>
      <button className="block rounded bg-ink px-4 py-2 text-paper disabled:opacity-50" disabled={busy || !repository} onClick={() => void login()}>连接仓库</button>
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    </section>
  </main>;
  return <div data-host-revision={revision}>
    <div className="flex flex-wrap items-center gap-3 border-b border-line bg-paper px-4 py-2 text-xs text-ink">
      <strong>{host.repository}</strong><span>{project ? `${project.status} · 目标 v${project.goal_revision}` : '尚未初始化'}</span>
      <a className="underline" href={`https://github.com/${host.repository}/actions`} target="_blank" rel="noreferrer">Actions</a>
      <a className="underline" href={`https://github.com/${host.repository}/pulls`} target="_blank" rel="noreferrer">交付与审核</a>
      {host.canWrite && project && <><button disabled={busy} onClick={() => void control(project.status === 'active' ? 'pause' : 'resume')}>{project.status === 'active' ? '暂停' : '恢复'}</button></>}
      <span className="ml-auto">{host.login}</span><button onClick={() => { disconnect(); setProject(null); }}>断开授权</button>
    </div>
    {(notice || error || jobs.length > 0) && <div className="border-b px-4 py-2 text-xs" role="status">
      {error || notice}{jobs.map(job => <div key={job.id}>{job.path} · {job.state}{job.error && ` · ${job.error}`} {job.url && <a className="underline" href={job.url} target="_blank" rel="noreferrer">查看这次运行</a>}</div>)}
    </div>}
    {project && <details className="border-b border-line bg-paper p-4 text-sm text-ink">
      <summary>仓库长期目标与待审核请求（{intake.length}）</summary>
      <p className="my-3 whitespace-pre-wrap">{project.objective}</p>
      <p className="text-xs">用户请求作为输入交给原生主控评估，不会直接覆盖这个目标。只有维护者明确修改目标才创建新版本。</p>
      {intake.map(row => <article className="my-3 rounded border border-line p-3" key={row.number}>
        <a className="underline" target="_blank" rel="noreferrer" href={`https://github.com/${host.repository}/issues/${row.number}`}>#{row.number} {row.title}</a>
        <pre className="my-2 max-h-64 overflow-auto whitespace-pre-wrap font-sans">{row.body}</pre>
        <p className="break-all text-xs">内容版本：{row.content_hash}</p>
        {host.canWrite && <button className="mt-2 rounded border px-3 py-1" disabled={busy} onClick={() => void control('approve-issue', { number: row.number, content_hash: row.content_hash })}>批准这份内容进入主控流程</button>}
      </article>)}
    </details>}
    {project ? children : <section className="mx-auto max-w-xl space-y-4 p-8">
      <h1 className="text-xl font-semibold">建立这个仓库的长期目标</h1>
      <p className="text-sm">先在仓库 Secrets 配置模型凭据，并检查 .argus-host/config.json。目标会成为版本化的权威状态。</p>
      <textarea className="min-h-40 w-full rounded border p-3 text-black" value={objective} onChange={event => setObjective(event.target.value)} placeholder="项目要持续实现什么？完成条件和不能突破的边界是什么？" />
      <div className="flex gap-3"><button disabled={!host.canWrite || busy || !objective.trim()} onClick={() => void control('initialize', { objective, open_ended: true, start: false })} className="rounded border px-4 py-2 disabled:opacity-50">建立项目，暂不运行</button>
      <button disabled={!host.canWrite || busy || !objective.trim()} onClick={() => void control('initialize', { objective, open_ended: true })} className="rounded bg-ink px-4 py-2 text-paper disabled:opacity-50">初始化并启动</button></div>
    </section>}
  </div>;
}
