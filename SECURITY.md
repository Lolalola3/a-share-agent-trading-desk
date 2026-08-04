# Security and privacy

## Never commit

- Brokerage account numbers, screenshots or credentials
- Real positions, costs, cash balances or order history
- API tokens, cookies, proxy credentials or session files
- Codex task IDs, automation IDs, project IDs or local absolute paths
- Generated `state/`, `records/`, `journal/` and strategy review files

## Runtime isolation

Set `A_SHARE_DESK_HOME` to a private directory outside the repository when possible. The included `.gitignore` excludes common runtime paths, but it is not a substitute for reviewing staged files before every push.

## Trading safety

This repository has no broker integration. Keep that boundary when extending it: recommendations should remain human-reviewed, and any future execution adapter should require separate explicit authorization, idempotency, reconciliation and kill-switch controls.

## Reporting

For security issues, open a GitHub security advisory instead of posting secrets in a public issue.
