# Fix

Fix answers the review on an Ashigaru pull request: every finding gets a reply in its thread, and failed checks get fixed.

## Why it matters

A required check that says "every review finding has an answer" stops a pull request until someone answers. For a pull request nobody is watching, that someone has to be the automation, and it has to stop when it is not converging.

## How it works

sdlc's `ai` job runs a round on a `pull_request_target` event, after Gatehouse has reviewed the head in the same job. It acts only when the branch starts with `ashigaru/`, the branch is in this repository, and the open pull request for it was opened by the Ashigaru app. A reply to a finding re-runs only the `gate` job, so a round cannot start the next one by replying.

**Fix agent** (`ai`: read-only token):

1. `scripts/findings.py` waits up to 15 minutes for checks that are still running, then collects inline findings by the reviewer that nobody else has replied to, and failed checks with the tail of their logs. `Gatehouse triage` is not counted as a failure: it is red because findings are unanswered.
2. Nothing to answer ends the run. If the round limit is spent, the run goes straight to the hand-off.
3. The agent edits the branch and returns one disposition per finding: `fixed`, or `not_a_bug` with a reason.

**Push and answer findings** (sdlc's `write` job: app token, no model): `scripts/fix_publish.py` yields, answering nothing, when the branch is no longer at the head the round started from (checked before the patch is applied, before a patchless round answers, and by a lease on the push); otherwise it pushes the patch and replies `fixed in <sha>` or `not a bug: <reason>`. A finding the agent did not account for gets no reply, so the required check stays red.

**Second source: maintainers.** `findings.py` also collects, from accounts with write access (`can_write`), inline comments, review bodies (a `changes-requested` review is findings, not a stop) and pull-request comments nobody has answered, and passes comments added to the linked issue since the last round as context. Each finding carries its `origin` and `author`. A maintainer finding is answered `fixed in <sha>` or with one question, never `not a bug`; `fix_publish.py` refuses to post a dismissal for it, and a question sets `needs-human`. Bots, Ashigaru itself and accounts without write access are never findings, so a reply cannot start the next round and outside contributors stay on the review path. Pickup is at a round boundary; a running job is not interrupted.

Each round leaves a marker comment on the pull request; that is how rounds are counted. Rounds 1 and 2 use `sonnet`, later rounds `opus`. After five rounds, or when a round changes no code while work remains, the pull request gets `needs-human`, auto-merge is turned off, and Ashigaru stops acting on it.

A `not a bug` reply is the agent judging a finding on its own pull request. Nothing independent checks the reason. The round limit and the repository's deterministic checks are the backstops.

## Related

- [Code](code.md): where the pull request comes from
- [Authority and limits](authority.md)
