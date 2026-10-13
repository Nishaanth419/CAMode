# Git workflow gates beyond the per-commit block in ../SKILL.md (Gates section).
# Not a script to run top to bottom: run the block for the stage you are at.

# Resolve the trunk once, then use it everywhere; never hardcode origin/main
git fetch origin --prune                      # stale refs make every gate below measure the wrong base
TRUNK=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD || echo origin/main)  # if unset: git remote set-head origin -a
# Not `git rev-parse --abbrev-ref origin/HEAD`: with origin/HEAD unset it prints "origin/HEAD" AND exits 128, so `|| echo` keeps both lines

# Before the first push / requesting review (unpushed branch)
git --version                                 # non-interactive --autosquash needs git >= 2.44
# Next line rewrites history: only while unpushed. If already pushed, fold fixups only on explicit instruction, then git push --force-with-lease --force-if-includes
GIT_SEQUENCE_EDITOR=: git rebase --autosquash "$TRUNK"   # fold fixup!/squash! commits; on git < 2.44 add -i, or --autosquash is ignored
npx commitlint --from="$TRUNK" --to=HEAD --verbose
git diff --stat "$TRUNK"...HEAD | tail -1     # ~1000+ changed lines: split
git log --oneline "$TRUNK"..HEAD              # no wip/fixup!/squash! subjects left
git branch -r --sort=-committerdate | head    # active-branch audit (target: 3 or fewer, hours old)

# In CI (binding), and before merging
pre-commit run --all-files                    # or --from-ref "$TRUNK" --to-ref HEAD
gh pr view --json isDraft,reviewDecision,statusCheckRollup,body
gh pr checks --required --watch --fail-fast   # block on the real required checks; exits non-zero if any fail

# At release
git describe --tags --exact-match HEAD        # the release commit is actually tagged
# Assertions, not bare greps: `grep -v` exits 1 when every tag is valid, failing a healthy repo in CI.
# `set -e` ignores a `!`-negated command, so the `|| { ...; exit 1; }` is what fails the job.
! git tag -l | grep -qvE '^v?[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$' || { echo 'non-semver tag'; exit 1; }
! git for-each-ref --format='%(refname:short) %(objecttype)' refs/tags | grep -qv ' tag$' || { echo 'lightweight tag'; exit 1; }
