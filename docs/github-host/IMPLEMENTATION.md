# GitHub repository host: implementation and acceptance

This fork retains the upstream runtime. The repository host is an additional
deployment target, not a replacement planner or a smaller research loop.
Device Flow is deferred; the Pages client initially uses a fine-grained PAT.

Deployment, controls, recovery, limits and upgrade policy are documented in
[the operator guide](README.zh-CN.md). Runtime promotion requires a compatible
state-format manifest and explicitly reviewed real-model acceptance evidence;
fixture CI alone cannot approve a claimed non-regression.

## Boundaries

- `argus/github_host`: repository protocol, durable state, versioned snapshots,
  trusted intake, bounded controller execution, remote mission execution,
  reconciliation, projections, upgrades and performance evidence.
- Upstream `LifeSupervisor`, Manager, Planner, SkillLoop, Engineer/Reviewer,
  skills, costs, research-stage guards and failure handling remain the source
  of decision logic. Private integration seams are explicitly version-checked.
- The controller never executes a delegated mission in its Action. A Worker
  runs the original mission machinery in a separate Action. Its result is a
  proposal; the controller validates provenance and merges state by base hash.
- Workers retain the native engineer/reviewer refinement loop inside a bounded
  mission. Independent PR acceptance/review is a separate task/run. Nested
  asynchronous subagent work must use the repository task transport.
- Only trusted, non-model publishing jobs have repository write credentials.
  The model execution job has no repository write token. Work from a fork PR
  is never checked out by an elevated publisher.
- Git stores authoritative state and evidence. Actions artifacts are transport,
  not the sole record. Tokens, provider auth files and machine locks are excluded.
- Source, runtime and goal revisions are separate. Pending tasks retain their
  runtime revision when upstream advances. Results from obsolete goals are held
  for reevaluation, never silently applied.

## Delivery sequence

1. Versioned task/event protocol, Git CAS store, snapshot/merge and security tests.
2. Native Argus controller/worker adapter with durable supervisor continuation.
3. Trusted intake, separate workflows, retries/reconciliation and self-hosted profiles.
4. Pages transport for the existing WebUI, shared commands and read projections.
5. Upstream candidate/rollback workflow, native regression suite, performance gates.
6. Live model scenarios (success, failed research route, recovery, concurrent work),
   then self-maintenance against this repository.

## Acceptance is evidence-based

No feature is declared preserved just because its code is still present.
Record native-vs-host tests for planning, review, memory, no-progress detection,
failure recovery, cost ownership and completion gates. Record context bytes,
model call counts, accepted quality and throughput; measure Actions queue/cold
start overhead separately. Zero orchestration overhead cannot be promised on
ephemeral infrastructure. Idle reconciliations must make zero model calls.
Live model quality/performance acceptance requires explicitly configured model
credentials; fixture results cannot stand in for it.

Keep all changes reviewable in this fork. Do not change the NRC renderer or merge
its infrastructure-probe PR as part of this implementation.
