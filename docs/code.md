# Code

Code turns a `ready-to-code` issue into a pull request that closes it, with auto-merge on.

## Why it matters

A coding agent that can also push is one prompt injection away from pushing something nobody asked for. Here the agent can only edit files on a runner that holds nothing worth stealing, and a separate job with no model in it does the pushing.

## How it works

The fleet's `ci.yml` runs it on the `issues` event, in two of its jobs on two runners.

**Code agent** (sdlc's `ai` job: read-only token, tools `Read,Edit,Write,Glob,Grep`):

1. The guard checks the event, then the issue and its maintainer comments are assembled into the prompt.
2. The agent edits the checkout and returns `status`, `summary` and `reason`.
3. The change leaves the job as a patch file and a JSON result. Nothing else does.

**Open pull request** (sdlc's `write` job: app token, no model): `scripts/publish.py` applies the patch on a fresh checkout, commits, pushes `ashigaru/issue-N`, opens the pull request with `Closes #N` and runs `gh pr merge --auto --squash`.

The publish job refuses, labels the issue `needs-human` and says why when the agent reported no fix, when the patch is empty, or when the applied patch touches what CI runs or what the gates check. That is judged after `git apply --index`, on what git staged against HEAD, so no spelling of the patch hides a path: the `.github/`, `.sdlc/`, `.semgrep/` and `.gourmand-exceptions.d/` directories, `.specify/memory/constitution.md`, the root `AGENTS.md`, `CLAUDE.md` and `.gemini/styleguide.md`, any file named `gatehouse.toml`, `gatehouse-findings.toml`, `.gatehouse-ignore`, `gourmand.toml`, `gourmand-exceptions.toml`, `gourmand-patterns.toml`, `gourmand-audit-ledger.toml`, `sdlc.toml`, `lychee.toml`, `.semgrepignore`, `.hadolint.y*ml`, `.markdownlint*`, `.gitignore`, `.ignore` or `.gitattributes`, a symlink onto any of them or out of the tree, and a change to the parsed `tool.sdlc` table of `pyproject.toml` (or a pyproject that no longer parses). A fix round that touches them hands the pull request to a person instead of pushing. If a pull request is already open for the branch and still merges, it does nothing. If the open pull request conflicts with the default branch, the job first rebases the branch's own commits, fix rounds included, onto the default branch and pushes them, keeping the description, review threads and round count. Only when that rebase conflicts does it rebuild the branch from the new patch: it force-pushes, rewrites the title and description to match, and posts a rebuild comment, after which the round count starts again.

No repository code runs next to the Claude token, so the repository's pre-commit hooks do not run before the pull request opens: its required checks are the arbiter, and [fix](fix.md) handles what they report.

The model (`sonnet`), the turn limit (80) and the tools are sdlc's, in `sdlc_jobs/ai.py`, and the same in every repository.

## Related

- [Triage](triage.md): how an issue becomes `ready-to-code`
- [Fix](fix.md): what happens after the pull request opens
- [Authority and limits](authority.md)
