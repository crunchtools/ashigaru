# Review

A pull request from an outside contributor gets one first-pass comment written for the maintainers, and everything waiting on a person is listed in one inbox issue.

## Why it matters

Ashigaru fixes what maintainers file. Contributions from everyone else are the ones that go unseen: a pull request can sit for months, and its fix can be written a second time by someone who never saw it. Ashigaru will not merge a stranger's code, but it can read it and say what it found, and it can keep a list.

## How it works

**Choosing.** Each hour `scripts/sweep.py` finds the open pull requests whose author is a person without write access. One is due a review when it is not a draft and Ashigaru's last review comment was written for a different head commit, or does not exist. The oldest five go to `review.yml`.

**Reviewing.** `review.yml` has two jobs.

1. *Read the pull request* runs the model with a read-only token and the tools `Read,Glob,Grep`. It checks out the branch the pull request targets and is given the pull request as text: title, description, list of files and diff. The fork is never checked out and nothing from it runs. The agent returns a summary, a recommendation (`merge`, `changes`, `close`), whether the base branch already has the change, and short lists of blockers and concerns.
2. *Post the review* runs no model. It holds an app token that can write to pull requests and nothing else. It adds what the API says (does it merge, how many checks passed and failed) and creates the comment, or edits the earlier one.

The comment carries a hidden marker with the reviewed commit, which is how the sweep knows not to review it again until the contributor pushes. If the agent fails, a comment saying so is posted with the same marker, so a pull request that cannot be reviewed is not retried every hour.

Nothing is labeled, approved, merged or closed. The recommendation is an opinion for a maintainer.

**The inbox.** The sweep keeps one open issue in the Ashigaru repository, labeled `ashigaru-inbox` and titled "Waiting on a maintainer". It lists:

- pull requests from outside contributors, each with Ashigaru's recommendation;
- issues from outside contributors, each with its type and state labels;
- pull requests labeled `needs-human`, where Ashigaru stopped after five rounds;
- a count of `needs-info` issues per repository.

The body has no timestamps and names people without an `@`, so it is edited only when the list changes and never notifies anyone.

## Example

The comment on a pull request:

```
**Ashigaru first-pass review**

Recommendation: **close**

Replaces the fixed vector width with one read from the embedding model. The
default branch made the same change in src/store.py since this was opened.

The base branch already contains this change or an equivalent one.

Merges cleanly: no, it conflicts with the base branch. Checks: 3 passed, 0 failed. Reviewed at `a1b2c3d`.
```

## Related

- [Sweep](sweep.md): what runs each hour
- [Triage](triage.md): how outside issues are handled
- [Authority and limits](authority.md)
