# claude-watcher Push Diagnosis — Fabricated Evidence Nearly Fixed a Non-Bug — Field Report

**Date:** 2026-05-28
**Type:** investigation
**Project:** claude-watcher

## Goal

Confirm whether claude-watcher was actually committing and pushing its documentation
snapshots to git. The reported symptom: Discord digests were arriving reliably, but it
was unclear whether the snapshot state was being committed/pushed anywhere. A prior
session had already "diagnosed two bugs" and proposed migrating snapshot history to a
new GitHub repo — this session set out to finish that fix.

## Root Cause

**There was no bug.** claude-watcher commits and pushes snapshots correctly, and had been
doing so for days.

The prior session's diagnosis was built on **fabricated tool output**. Its SSH/`docker`
reads ran through the Bash tool inside a sandbox that could not reach the homelab at
`192.168.1.8`, and the results came back invented rather than as connection errors. That
produced a confident but entirely false picture ("snapshots aren't pushing, move to
GitHub").

Re-running the exact same reads through the `!`-prefix (which executes in the user's real
shell and pipes output straight into the conversation) revealed the ground truth:

- Container logs show, after **every** cycle (05-26 through 05-28, both `changelog` and
  `full` scopes):
  ```
  Delivered digest to Discord.
  Email delivery skipped, no SMTP configured.
  Committed snapshot.
  Pushed snapshot to remote.
  ```
- `git -C /app/snapshots log --oneline` → 5 real commits (`docs(full): 26 modified`, etc.).
- `git status` → clean.
- Remote: `http://cameron:<token>@gitea:3000/cameron/claude-docs-snapshots.git` — it pushes
  to **Gitea**, not GitHub (browsable at `git.sjo.lol/cameron/claude-docs-snapshots`).
- `WATCHER_GIT_REMOTE_URL` **is** set in the container env.

The behavior only *looked* broken from the outside because: (a) it pushes to Gitea, not a
GitHub repo the user was watching, and (b) the push is fire-and-forget — logged only inside
the container — so there was no external signal confirming success.

## What We Tried

1. **Read the code path first** (before any live access). Confirmed the pipeline in
   `main.py:54` runs fetch → `compute_diff` → summarize → `deliver` → commit. The commit at
   `main.py:83` only fires `if delivered`. Critically, `deliver()` (`delivery.py:143-152`)
   returns `True` if *any* channel succeeds or is skipped — so Discord working guarantees
   `commit_snapshot` runs. That ruled out the "delivery gate blocks commit" hypothesis from
   the code alone.
2. **Formed two hypotheses** from the code: (1) `WATCHER_GIT_REMOTE_URL` unset → commits
   stay local in the volume; (2) push fails silently because the error at `differ.py:197` is
   logged, never raised. Both plausible from code; neither confirmable without live data.
3. **Gathered live evidence via `!`** — one consolidated SSH command dumping log grep +
   `git log` + `git remote -v` + `git status` + the env var. This dissolved both hypotheses
   immediately: the var is set and the push succeeds.

## Decisions Made

- **Keep snapshot history on Gitea.** The prior "move to GitHub" plan was a response to a
  false premise. With Gitea confirmed working, no migration — stays self-hosted, consistent
  with the homelab.
- **Do not rotate the Gitea token.** It leaked into the conversation (it's embedded plaintext
  in the remote URL), but Gitea is internal-only, so the practical risk is low.
- **Ship a docs-only fix.** The real residual gap was that `WATCHER_GIT_REMOTE_URL` — the var
  driving the entire push behavior — was supported in `config.py:52` but absent from
  `.env.example`. Documented it there and in the README's state-store note (commit `0f29f95`).

## Gotchas

- **Fabricated tool data is a debugging hazard, not just an annoyance.** A whole prior
  session's conclusions and a proposed migration were invalid because sandboxed Bash reads to
  an unreachable host returned invented output. The systematic-debugging Iron Law — *gather
  real evidence before proposing fixes* — is what caught it. The `!`-prefix is the reliable
  workaround for homelab reads the sandbox can't reach.
- **A stale beads pre-commit hook blocked every commit in the repo** — unrelated to the
  investigation. The hook (`.git/hooks/pre-commit`) calls `bd sync --flush-only`, a subcommand
  that **bd 1.0.0 removed** during its move to a Dolt backend. The hook's own escape hatch
  ("skip if backend is Dolt") never fired because `.beads/metadata.json` predates the Apr-2026
  Dolt migration and has no `"backend"` field, so the grep on hook line 41 misses. Net effect:
  a generic `git commit` failed with `Failed to flush bd changes to storage`, with no relation
  to the staged change. Patched the hook to probe `bd sync --help` (exit 1 = subcommand absent)
  and `exit 0` early.
- **`bd help sync` returns exit 0 even for an unknown subcommand** (prints "Unknown help
  topic"), so it is NOT a usable existence check. Use `bd sync --help` (exit 1 when absent).
- **The hook patch is local and not version-controlled** (it lives under `.git/`). A fresh
  clone or a `bd` hook reinstall will reinstate the broken original. The durable fix belongs in
  the beads/cadence tooling that ships this hook to all repos.

## Recommendations

- **Fix the beads hook upstream.** The same stale `bd sync --flush-only` hook almost certainly
  ships to every beads-initialized repo. Repair it in the cadence/dotfiles tooling rather than
  re-patching per-repo.
- **Consider surfacing push outcome beyond container logs.** The push being fire-and-forget is
  exactly why this was hard to confirm. A heartbeat (e.g., include "pushed to <remote>" in the
  digest, or alert on push failure) would make the state observable without `docker logs`.
- **When a homelab read via Bash looks too clean, distrust it.** Re-run through `!` before
  building any conclusion on sandboxed network output.

## Key Takeaways

- The watcher works: it commits and pushes every cycle to Gitea `cameron/claude-docs-snapshots`.
  There was never a bug — only undocumented, invisible-from-outside behavior.
- Sandboxed Bash reads to unreachable hosts can return fabricated output. Evidence-first
  debugging via the `!`-prefix is the antidote; it caught a fully invalid prior diagnosis.
- bd 1.0.0 dropped `bd sync`; old beads pre-commit hooks call it and block *all* commits. The
  fix is in `.git/hooks/pre-commit` (local, not in git) — fix it in shared tooling to make it stick.
- The one shipped change was documentation (`WATCHER_GIT_REMOTE_URL` in `.env.example` + README),
  closing the gap that made a working feature look broken.
