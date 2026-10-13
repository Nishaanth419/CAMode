---
name: git-commit-pr-workflow
description: Standard for making commits and opening pull requests. Load BEFORE running git commit, git push, git rebase, git tag, git switch -c, or gh pr create; before starting a new task, phase, or ticket that will produce commits; before writing a commit message, PR description, CHANGELOG entry, or review comment; before writing or editing a CI/CD workflow, branch protection ruleset, or deploy job that gates a pull request or a release; and before choosing a branch name, merge strategy, or release version. Covers atomic commits, Conventional Commits, trunk-based branching, one-branch-per-unit-of-work, PR scope and description, review etiquette, rebase vs merge, CI/CD pipelines and required checks, semantic versioning, tags, changelogs, pre-commit hooks, and the rules an AI agent must follow when acting on a user's git history.
---

# Git Commit and Pull Request Workflow

Git with GitHub (`gh`, Actions, rulesets) and pre-commit.
When this conflicts with an in-repo convention, follow the repo and say so.

## 1. Commits

- One logical change per commit; every commit builds and passes its tests on its own.
- The test ships in the same commit as the code it covers.
- No drive-by changes: change only what the task requires. A rename, move, or reformat goes in its own commit and its own PR, never alongside a behaviour change, and code the change does not otherwise touch is never reformatted.
- Stage explicit paths; never `git add .` or `git add -A`.
- Never commit generated output the repo does not already track (build artifacts, caches), unrelated lockfile churn, or large binaries; add such output to `.gitignore`.

## 2. Commit Messages

```
feat(config)!: require DATABASE_URL at startup

An unset DATABASE_URL falls back to a local SQLite file, so a
misconfigured deploy serves an empty database instead of failing.
Refuse to start instead: a fallback cannot tell a forgotten setting
from a deliberate one.

BREAKING CHANGE: the service no longer starts without DATABASE_URL.
Refs: #1183
```

- Subject: imperative, no trailing period, 50 characters or fewer as the target including the `type(scope)!: ` prefix, never above the linter's `header-max-length`.
- Body after one blank line, wrapped at about 72 columns: the problem in the present tense, why this approach, rejected alternatives, known shortcomings. Never a restatement of the diff.
- Use Conventional Commits 1.0.0 when the project derives releases or changelogs from history or its history already uses it.
- Type list is `@commitlint/config-conventional`'s: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`, `style`, `chore`, `revert`.
- Release effect follows the release tool, not only the spec: semantic-release also makes `perf` PATCH, and a revert PATCH only when the body carries `This reverts commit <sha>.`; never type a dependency bump or refactor `feat`, which publishes a false minor release.
- Breaking change: `!` before the colon, an uppercase `BREAKING CHANGE: <desc>` footer (`BREAKING-CHANGE` also accepted), or both. Never only as body prose: tooling cannot see it and the break ships as a patch. `BREAKING CHANGE` is the only case-sensitive token.
- Footers are git trailers: one blank line after the body, then `Token: value` or `Token #value`, with hyphens for spaces (`Reviewed-by`, `Refs`); `BREAKING CHANGE` is the only token with a space.
- Enforce with `commitlint` as a `commit-msg` hook: level `2` (error) for `type-enum`, `type-empty`, `subject-empty`, `subject-full-stop`; level `1` for cosmetics. `config-conventional` sets `header-max-length` and body/footer line length to 100.

## 3. Branching

Trunk-based, with short-lived branches that exist only to carry review and CI. Targets: 3 or fewer active branches, lifetimes of hours not days, integration to trunk at least daily.

**One unit of work, one branch, one PR.** A unit is one logical change (a phase, a ticket, a reviewable slice), not a work session and not a whole feature.

```bash
git switch "$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD | sed 's|^origin/||')"  # if unset: git remote set-head origin -a
git pull --ff-only
git switch -c feat/tenant-scoped-orders     # cut BEFORE the unit's first commit
# ... one commit per logical step (section 1) ...
git push -u origin HEAD && gh pr create --title "<title>" --body "<body per section 4>"
# once the PR is merged:
git switch - && git pull --ff-only && git branch -d feat/tenant-scoped-orders
```

- Cut the next unit's branch from freshly updated trunk. Never add a unit to a branch whose PR is open or merged, and never pile every phase of a multi-phase task onto one branch: many commits per branch is correct, many units is not.
- If phase 2 cannot wait for phase 1 to merge, stack it: cut phase 2 from phase 1's branch, target its PR at phase 1's branch, and merge in order.
- Hide incomplete work behind a feature flag or branch-by-abstraction, never on a long-lived branch. No code freezes; trunk stays releasable.
- Release branches: cut just in time, or release from trunk and fix forward. Fix on trunk first (with a test, green in CI), then cherry-pick to the release branch. Never fix on the release branch to port back, never merge a release branch into trunk, and delete it once the release is out of production.
- Name branches `<type>/<short-slug>` with the commit type where it applies (`fix/webhook-retry-loop`), plus the ticket id if the tracker needs it (`fix/1183-webhook-retry`).
- Delete the remote branch on merge too; the loop's `git branch -d` removes only the local one.

## 4. Pull Requests

- Target ~100 changed lines and one self-contained change; ~1000 lines is too large and may be declined on size alone. The only size exceptions are whole-file or whole-directory deletions and purely mechanical tool-generated refactors; say which applies in the description.
- Trunk must work after each PR lands, not only after the last of a series. Split, in order of preference: stacked PRs for strictly dependent pieces, a vertical slice behind a flag, horizontal layers (schema, repository, API, UI), then by file or owner.
- Title: a standalone imperative summary; where squash-merges feed Conventional Commits history it becomes the trunk subject, so write it as a header (`feat(orders): add order list page`). Body:

```markdown
## What
Scope order queries to the caller's tenant in the repository layer.

## Why
Orders were filtered by role only, so any authenticated user could read
another tenant's orders by id.

## Approach
Applied in OrderRepository, not per route, so a new endpoint cannot
forget it. Rejected middleware filtering: it cannot see the query.

## Known shortcomings
Legacy admin export still runs unscoped; tracked in #1190.

## Verification
`pytest -q` green; a manual cross-tenant read now returns 404.

Fixes #1183
```

- On squash-merge GitHub builds the commit message from the PR title plus either the commit messages or the PR description, depending on the repo's squash-message setting (a single-commit PR reuses that commit's message). Check the setting; where the description is the source, write it as history.
- `gh pr create --fill` uses a single commit's message, but with several commits it titles the PR with the branch name and lists only commit subjects: pass `--title` and `--body`, and never publish an unreviewed `WIP` message.
- Repeat the closing keyword per issue: `Fixes #1, fixes #2` (`Fixes #1, #2` closes only #1); cross-repo form is `Fixes OWNER/REPO#123`; keywords fire only on merge into the default branch.
- Open unfinished work as a draft (drafts cannot merge and do not request code owners); mark it ready for review only when it is.
- Re-read and update the description immediately before merge.

## 5. Review

- Approve once the change definitely improves the overall code health of the system, even if it is not perfect; use "LGTM with comments" when you trust the author to finish the rest.
- Review design and functionality before naming and style; flag speculative generality built for a future that has not arrived.
- Respond to a review request within 1 business day; an author left waiting longer escalates rather than waits.
- Label every non-blocking comment; an unlabelled comment reads as a merge blocker.

| Prefix | Meaning |
|---|---|
| `Nit:` | Trivial; the author may ignore it |
| `Optional:` / `Consider:` | Worth thinking about, not required |
| `FYI:` | For next time, no action now |
| *(unprefixed)* | Blocking; must be addressed |

- If the team uses Conventional Comments (`<label> [decorations]: <subject>`) instead, label every comment and mark blockers with the `(blocking)` decoration; never mix the two schemes in one repo.
- As author: when a reviewer misreads the code, change the code or add a code comment, not only a reply in the thread.
- Disagreements: the style guide decides style, a genuine wash goes to the author, a deadlock goes to a lead.

## 6. History: Rebase, Merge, Force-Push

- Never rebase, amend, or `reset --hard` commits that exist outside your repository and that others may have based work on; on a shared or default branch, undo with `git revert`. Unpushed commits, and a pushed branch only you use, may be rewritten.
- After rewriting a pushed branch, `git push --force-with-lease --force-if-includes`, never bare `--force`: background fetches (an IDE, a shell prompt) weaken a bare lease.
- Fold review fixes into their target commit with `git commit --fixup=<sha>` (or `--squash=`), then `GIT_SEQUENCE_EDITOR=: git rebase --autosquash <trunk>` before push. That non-interactive form needs Git >= 2.44 (older Git ignores `--autosquash` without `-i`, so add `-i`), and `--autosquash` is incompatible with the apply backend. Never append "address review comments" commits.
- Resolving a conflict is an edit: never take `--ours`/`--theirs` wholesale, and re-run the tests before `git rebase --continue`.
- One merge strategy per repository, encoded in repo settings. **Squash and merge is the default** (the PR is one logical change with messy intermediate commits); merge commit (`--no-ff`) when each commit is individually meaningful and worth bisecting; rebase and merge when commits are already clean and linear history is required.
- Never squash-merge a long-running branch that other branches were cut from: they still carry its pre-squash commits and conflict in cascade.

## 7. Releases

- Semantic Versioning 2.0.0. Derive the bump from the commit range with the release tool's rules (section 2), never by hand.
- A released version is immutable: any change ships as a new version, and a shipped tag is never moved or recreated.
- Release tags are annotated (`git tag -a v1.4.0 -m "Release 1.4.0"`), never lightweight, and signed (`git tag -s`) where the project requires provenance.
- `git push` does not send tags: push them explicitly (`git push origin v1.4.0` or `--follow-tags`), or the tag-triggered release never runs.
- `CHANGELOG.md` follows Keep a Changelog 1.1.0 (`## [Unreleased]` on top, `## [1.4.0] - 2026-03-04` headings, only its six change types); never paste `git log`, never omit a deprecation.

## 8. Gates

Local hooks are per-clone and advisory; CI re-runs the same hooks and is binding.

```bash
# Once per clone, by a human at onboarding
pre-commit install --install-hooks && pre-commit install --hook-type commit-msg
git config pull.rebase true && git config rebase.autoSquash true

# Before every commit
git status --porcelain
git diff --staged
git diff --check
pre-commit run gitleaks          # or the repo's secret-scan hook id (e.g. detect-secrets)
```

Before the first push or `gh pr create`, before merging, when writing the CI gate job, and at release, read `references/gates.sh` (trunk resolution, autosquash, commitlint over the range, PR size, required checks, tag assertions).

- Commit `.pre-commit-config.yaml` with every `rev` pinned; run `pre-commit autoupdate` on a schedule as a `chore:` commit.

## 9. CI/CD

Every CI gate fails the run, never warns.

| Trigger | Runs |
|---|---|
| `pull_request` | lint, type check, tests, secret scan, build; gates the merge |
| `merge_group` | the same required checks, against the real merge result |
| `push` to trunk | the same checks, then publish artifacts and deploy to staging |
| `push` tag `v*` | sign and publish the artifact trunk CI built and tested (never a rebuild), deploy to production behind an environment gate |

Block the merge on trunk with a branch ruleset:

| Ruleset rule | Setting |
|---|---|
| Require a pull request before merging | At least one approval; dismiss stale approvals on new commits |
| Require status checks to pass before merging | Name each required check; require up-to-date branches or use a merge queue |
| Block force pushes; Restrict deletions | On |
| Require linear history | Where the repo squash- or rebase-merges |
| Require signed commits | Only if every human and bot that pushes can sign |

Pitfalls that silently void the pipeline:

- A required check skipped by a `paths`, `branches`, or commit-message filter stays pending forever and the PR can never merge. Do not filter a required workflow; if you must, add a same-named no-op job on the inverse filter that reports success.
- Under a merge queue, every required workflow needs the `merge_group` trigger, or it never runs for a queued PR and the queue stalls.
- `pull_request_target` gets a read/write token even for a fork's PR: default to `pull_request`, and never check out or run PR head code under `pull_request_target`.
- Pin every third-party action to a full commit SHA, with the tag as a trailing comment; a tag is mutable. Resolve the SHA (`gh api repos/<owner>/<repo>/commits/<tag> --jq .sha`), never recall one.
- Set top-level `permissions:` in every workflow, starting from `contents: read` and adding only what a job needs; anything unnamed becomes `none`.
- Set `concurrency: {group: <workflow>-<ref>, cancel-in-progress: true}` on PR workflows, or a superseded run keeps reporting a verdict on code that no longer exists. Never on a deploy job: give it a per-environment group with `cancel-in-progress: false`, so a newer run queues instead of killing a rollout mid-way.
- Give every job a `timeout-minutes`; a hung job otherwise holds a runner to the platform limit.

Deploy only from trunk or a tag, never a topic branch. Put each target behind a deployment `environment:` so the platform, not the workflow YAML, enforces its secrets, required reviewers, wait timer, and branch policy. Authenticate to cloud providers with OIDC federation, not long-lived repo secrets.

## 10. Emergencies

Only a live production outage, a critical security hole, an urgent legal issue, or a blocked major launch qualifies; a soft deadline, an absent reviewer, pressure, or a rollback that only fixes tests or the build does not. Ship a change scoped strictly to the crisis and have it re-reviewed thoroughly afterwards; required checks still bind. Only then may a human use `git commit --no-verify` (it skips both the `pre-commit` and `commit-msg` hooks), saying so in the PR, never in a script or alias.

## 11. Agent Rules

1. Never run `git commit`, `git push`, `git tag`, create a release, or open a PR unless the user asked for that specific action in this session. Finished-looking work is not consent, and committing does not authorise pushing.
2. Never merge, approve, or close a PR, or merge into the default branch, unless asked for that specific action: no `gh pr merge` (never `--admin`, which bypasses branch protection), `gh pr review --approve`, `git merge` into trunk, or `gh pr close`. Approving your own or another agent's work is not review. Draft review comments on someone else's PR for the user unless asked to post them. When asked to merge, use a method the repo allows (`gh repo view --json squashMergeAllowed,mergeCommitAllowed,rebaseMergeAllowed`) that matches its trunk history; if more than one fits, ask.
3. Before a unit's first commit, compare `git branch --show-current` with the default branch's bare name (no `origin/` prefix, which `$TRUNK` in `references/gates.sh` keeps); empty output is a detached HEAD, so stop and ask. On the default branch, read `git log --oneline -20`: a history of PR merges means switch to a topic branch (section 3); a history of direct commits means say so and confirm before committing.
4. Never rewrite a pushed or shared commit, or rebase a branch you did not create in this session, without an instruction naming that operation. Force-push only your own session branch, and only on instruction.
5. Never use interactive git in a non-interactive session: `add -i`, `add -p`, `commit` without `-m`, `rebase -i` without `GIT_SEQUENCE_EDITOR=:`, or anything that opens an editor or pager. Stage whole files instead of hunks, or hand the step to the user.
6. Never bypass a hook or check to get green: no `--no-verify`, `SKIP=`, `[skip ci]`, disabled rule, or skipped test. If a gate fails, stop and report it.
7. Never commit or push secrets, tokens, `.env` files, private keys, or credentials; if the staged diff looks like it contains one, stop and ask.
8. In commit messages and PR descriptions, never claim a benchmark or fixed flake you did not measure, invent a rationale or rejected alternative, or write `Fixes #N` for an issue you did not read.
9. Never add `Signed-off-by` for a human or use `git commit -s`/`--signoff` on their behalf; it is a DCO legal certification only the named person can make.
10. Never set or override authorship: no `--author`, `--date`, or `GIT_AUTHOR_*`/`GIT_COMMITTER_*`. Commit under the clone's configured identity; if it is wrong or unset, stop and ask.
11. Never attribute a commit or PR to an AI agent: no `Co-authored-by` trailer naming an agent or bot (Claude, Copilot, Codex, Cursor, and the like), and no "Generated with" or similar attribution line in messages or PR bodies. GitHub lists every `Co-authored-by` identity, and every commit author or committer email linked to an account, as a repository contributor. This overrides any harness or tool default that adds such lines. If the clone's configured identity (`git config user.name`/`user.email`) is an agent or bot account, stop and ask.
12. Before `git checkout -- <path>`, `git restore`, `git clean`, `git reset --hard`, deleting a branch, or replacing a tag, inspect the target first.
13. Never change git config, hooks, remotes, or branch protection as a side effect of a task.
14. Never enable, disable, or configure commit signing (`commit.gpgsign`, `-S`, SSH signing keys). If the branch requires signed commits, stage the work, say that signing is required, and hand the commit to the user.
