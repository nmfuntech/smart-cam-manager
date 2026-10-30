#!/usr/bin/env bash
# Deploy the current commit from this machine to the Windows mini PC.
#
#   make deploy                 # deploy the current branch
#   make deploy REF=main        # deploy a specific branch/tag/commit
#   make deploy SKIP_TESTS=1    # skip local + remote test runs (emergencies only)
#   make deploy-status          # show what the mini PC is running
#
# Flow: clean tree check -> lint + tests here -> git push -> upload the latest
# scripts/windows/update.ps1 -> run it over SSH (it fetches, re-syncs deps only
# when the lock changed, runs the tests in a separate worktree, restarts the
# service and rolls back automatically if /health does not come back).
set -euo pipefail

HOST="${MINIPC_HOST:-blackframe-minipc}"
REPO_DIR="${MINIPC_REPO_DIR:-C:\\Users\\nikom\\smart-cam-manager}"
REMOTE_SCRIPT="bf-update.ps1"
cd "$(dirname "$0")/.."

ssh_q() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$@" 2> >(grep -v '^\*\*' >&2); }

if [[ "${1:-}" == "status" ]]; then
  ssh_q "cd /d $REPO_DIR && git log -1 --format=\"%h %ci %s\" && git status -sb --untracked-files=no && sc query BLACKFRAME | findstr STATE && curl -s -m 5 http://127.0.0.1:8000/health"
  exit 0
fi

REF="${REF:-$(git rev-parse --abbrev-ref HEAD)}"
if [[ "$REF" == "HEAD" ]]; then
  echo "Detached HEAD: pass REF=<branch|tag|sha>" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "Working tree has uncommitted changes: commit them first." >&2
  exit 1
fi

if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  echo "==> lint + tests (local)"
  poetry run ruff check .
  poetry run python -m pytest -q -p no:cacheprovider
fi

echo "==> push $REF"
if git show-ref --verify --quiet "refs/heads/$REF"; then
  git push --quiet origin "$REF"
fi

echo "==> upload updater to $HOST"
scp -q -o BatchMode=yes scripts/windows/update.ps1 "$HOST:$REMOTE_SCRIPT" 2> >(grep -v '^\*\*' >&2)

ARGS=(-Ref "$REF" -RepoDir "$REPO_DIR")
[[ "${SKIP_TESTS:-0}" == "1" ]] && ARGS+=(-SkipTests)

echo "==> remote update"
set +e
ssh_q "powershell -NoProfile -ExecutionPolicy Bypass -File $REMOTE_SCRIPT ${ARGS[*]}"
code=$?
set -e
case $code in
  0) echo "==> deploy OK" ;;
  2) echo "==> deploy aborted: local edits on the mini PC (nothing changed)" ;;
  3) echo "==> deploy aborted before restart (tests/deps failed): mini PC unchanged" ;;
  4) echo "==> new version unhealthy: automatic rollback performed, see logs\\deploy.log" ;;
  *) echo "==> deploy failed (exit $code)" ;;
esac
exit $code
