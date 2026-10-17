# Sweep

Sweep runs every hour. It queues old issues for triage and starts the fixes that are waiting, on a budget.

## Why it matters

Triage runs when an issue is opened, so issues that predate enrollment would never be looked at. Triage is a short read-only run, so the backlog can be triaged quickly. A fix is a long run with review rounds, and starting dozens at once would spend a subscription's rate limit in an hour. So the sweep has two limits: issues queued for triage per run, and fixes started per hour.

## How it works

`sweep.yml` runs in the Ashigaru repository only, dispatched hourly by Kagetora's "Ashigaru Sweep Trigger" Hermes job; GitHub's own schedule fired only every four or five hours. `scripts/sweep.py`:

1. lists the organization's public, non-archived repositories and keeps those that carry `.github/workflows/ashigaru.yml`;
2. reads each one's hundred oldest open issues and keeps those a person wrote that carry none of Ashigaru's labels;
3. walks them oldest first across all repositories and adds `ready-for-triage` to the first `limit` (20 by default, never more than 20). Issues from outside contributors are included; the [guard](authority.md) keeps them to triage.

**Re-triage after a triage fix.** An issue Ashigaru already labeled `triaged` or `needs-info` is not queued again, so a verdict the old prompt got wrong stays wrong. A manual run with `retriage_before` set to a date (`YYYY-MM-DD`, UTC) also queues issues whose state label Ashigaru itself set before that date, under the same per-run limit. Run it once per 20 issues until it queues none. A state label a person set is left alone, and so is a type label a person set when the issue is triaged again ([triage](triage.md)).

**Starting fixes.** A newly opened issue that triage marks `ready-to-code` goes to the code agent in the same run. An issue triaged any other way (queued by the sweep, or re-triaged after a question was answered) is labeled `ready-to-code` and waits. Each hour the sweep counts the pull requests Ashigaru opened across the organization in the past hour, subtracts that from `code_per_hour` (default 5), and starts that many fixes by removing and re-adding `ready-to-code` with the app token. It starts, in order:

1. Ashigaru's own open pull requests that conflict with the default branch (the publish job rebuilds the branch; one labeled `needs-human` is never rebuilt);
2. waiting issues, oldest first: open, labeled `ready-to-code`, no `ashigaru/issue-N` pull request, and not touched in the last 20 minutes, so a fix already in flight is not started twice.

A maintainer who adds `ready-to-code` by hand starts the fix at once; it counts against the next hour's budget once its pull request opens.

The label is added with the app token, so it starts each repository's own workflow. From there the issue follows the normal path through [triage](triage.md).

**What waits on a person.** After that the sweep picks up to five outside pull requests for a [first-pass review](review.md) and rewrites the inbox issue when its list changed.

**Releases.** A second job in the same workflow cuts [releases](release.md) in repositories that asked for them, and keeps the releases record.

## Example

Run by hand to see what would be queued:

```bash
gh workflow run sweep.yml -R crunchtools/ashigaru -f limit=10 -f dry_run=true
```

## Related

- [Triage](triage.md)
- [Review](review.md)
- [Release](release.md)
- [Authority and limits](authority.md)
