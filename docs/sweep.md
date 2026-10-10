# Sweep

Sweep queues old issues for triage, a few each night.

## Why it matters

Triage runs when an issue is opened, so issues that predate enrollment would never be looked at. Queueing all of them at once would start dozens of agent runs and spend a subscription's rate limit in an hour. The sweep's limit is the budget.

## How it works

`sweep.yml` runs on a schedule in the Ashigaru repository only. `scripts/sweep.py`:

1. lists the organization's public, non-archived repositories and keeps those that carry `.github/workflows/ashigaru.yml`;
2. reads each one's hundred oldest open issues and keeps those with none of Ashigaru's state labels;
3. walks them oldest first across all repositories, skips authors without write access, and adds `ready-for-triage` to the first `limit` (default 5, never more than 20).

Before queueing anything new it looks for Ashigaru's own open pull requests that conflict with the default branch, and sends each one's issue back through [code](code.md) by removing and re-adding `ready-to-code`. The publish job then rebuilds the branch on the current default branch. Rebuilds come out of the same `limit`, first, and a pull request labeled `needs-human` is never rebuilt.

The label is added with the app token, so it starts each repository's own workflow. From there the issue follows the normal path through [triage](triage.md).

## Example

Run by hand to see what would be queued:

```bash
gh workflow run sweep.yml -R crunchtools/ashigaru -f limit=10 -f dry_run=true
```

## Related

- [Triage](triage.md)
- [Authority and limits](authority.md)
