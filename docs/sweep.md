# Sweep

Sweep runs every hour. It queues old issues for triage and starts the fixes that are waiting, on a budget.

## Why it matters

Triage runs when an issue is opened, so issues that predate enrollment would never be looked at. Triage is a short read-only run, so the backlog can be triaged quickly. A fix is a long run with review rounds, and starting dozens at once would spend a subscription's rate limit in an hour. So the sweep has two limits: issues queued for triage per run, and fixes started per hour.

## How it works

`sweep.yml` runs on a schedule in the Ashigaru repository only. `scripts/sweep.py`:

1. lists the organization's public, non-archived repositories and keeps those that carry `.github/workflows/ashigaru.yml`;
2. reads each one's hundred oldest open issues and keeps those with none of Ashigaru's state labels;
3. walks them oldest first across all repositories, skips authors without write access, and adds `ready-for-triage` to the first `limit` (20 on the schedule, never more than 20).

**Starting fixes.** A newly opened issue that triage marks `ready-to-code` goes to the code agent in the same run. An issue triaged any other way (queued by the sweep, or re-triaged after a question was answered) is labeled `ready-to-code` and waits. Each hour the sweep counts the pull requests Ashigaru opened across the organization in the past hour, subtracts that from `code_per_hour` (default 5), and starts that many fixes by removing and re-adding `ready-to-code` with the app token. It starts, in order:

1. Ashigaru's own open pull requests that conflict with the default branch (the publish job rebuilds the branch; one labeled `needs-human` is never rebuilt);
2. waiting issues, oldest first: open, labeled `ready-to-code`, no `ashigaru/issue-N` pull request, and not touched in the last 20 minutes, so a fix already in flight is not started twice.

A maintainer who adds `ready-to-code` by hand starts the fix at once; it counts against the next hour's budget once its pull request opens.

The label is added with the app token, so it starts each repository's own workflow. From there the issue follows the normal path through [triage](triage.md).

## Example

Run by hand to see what would be queued:

```bash
gh workflow run sweep.yml -R crunchtools/ashigaru -f limit=10 -f dry_run=true
```

## Related

- [Triage](triage.md)
- [Authority and limits](authority.md)
