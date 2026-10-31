# Release

Release turns merged work into a version: a release pull request written from the changelog, then the tag and the GitHub Release.

## Why it matters

A fix that merges and is never released helps nobody who installs from a tag, a registry or a package index. In a repository people visit daily someone cuts the release. In one nobody has opened for a month, the fix sits on the default branch until someone remembers. Ashigaru fixes bugs in exactly those repositories, so it has to be able to ship them too.

How much to trust it differs per repository, so it is set per repository and is off by default.

## How it works

**Turning it on.** Add a topic to the repository. Only its admins can; the app cannot.

| Topic | Effect |
|-------|--------|
| none | nothing is released; unreleased work is still listed in the record |
| `ashigaru-release-pr` | Ashigaru opens the release pull request and a maintainer merges it |
| `ashigaru-release-auto` | GitHub merges the release pull request when the required checks pass |
| `ashigaru-release-max-patch`, `-minor`, `-major` | the largest bump `auto` merges without a person; `minor` when absent |

With both mode topics set, `pr` wins. With several limits set, the lowest wins.

**What the repository needs.** A `CHANGELOG.md` in [Keep a Changelog](https://keepachangelog.com/) form: a `## [Unreleased]` heading, and below it a `## [X.Y.Z]` heading for the version already released, whose tag `vX.Y.Z` exists. Changes add their entries under `[Unreleased]`; the code agent's pull requests already do where the repository's instructions ask for it.

**When a release is due.** Each hour the `release` job in `sweep.yml` runs `scripts/release.py` over the enrolled repositories. One is due when:

- `[Unreleased]` has at least one entry;
- no pull request from an `ashigaru/` branch is open there, so a batch of fixes lands in one release;
- the newest commit on the default branch is more than 20 minutes old.

**The version.** Read from the unreleased entries:

| Entries contain | Bump |
|-----------------|------|
| the word `BREAKING`, or a `### Removed` section | major |
| `### Added`, `### Changed` or `### Deprecated` | minor |
| anything else | patch |

Below 1.0.0 a breaking change bumps the minor.

**The release pull request** comes from `ashigaru/release-X.Y.Z`, titled `chore: release X.Y.Z`, with the changelog entry as its body. Its one commit:

- moves the unreleased entries under `## [X.Y.Z] - date`, leaves `[Unreleased]` empty, and updates the compare links at the foot if the changelog has them;
- replaces the old version with the new one in tracked files, but only on a line that carries the word `version` on its own (`version = "1.2.3"`, `"version": "1.2.3"`, `__version__`, a `version=` label; not `minimum_version` or `python-version`), or in a file that holds nothing else, such as `VERSION`. In a lock file only the version under the project's own name moves. A dependency that happens to share the number stays as it is.

Files under a dot directory (`.github/`, `.specify/`) are never edited. If the old version is stated nowhere outside the changelog, nothing is opened and the record says so.

In `auto` mode, with the bump at or under the limit, auto-merge is turned on. Otherwise the pull request waits, says why in its body, and is listed in the [inbox](review.md). The [fix agent](fix.md) does not work on release pull requests. One that conflicts with the default branch is closed and a fresh one opens on a later sweep.

**Publishing.** When the changelog's current version has no tag, and a release pull request for that version, written by Ashigaru, has merged into the default branch, the sweep creates the tag `vX.Y.Z` at the merge commit and publishes the GitHub Release with the changelog entry as its notes. This holds in both modes: merging the release pull request is the approval. The release is created with the app token, so workflows that run on `release: published` start.

A version with no tag and no such pull request is not Ashigaru's to publish. It is listed as refused until someone tags it.

**The record.** One issue in the Ashigaru repository, labeled `ashigaru-releases`, rewritten when it changes:

- releases published in the last seven days in any enrolled repository, whoever cut them;
- release pull requests that are open, and whether each merges on its own. One with auto-merge on whose head has a failed check says "Blocked:" and names the check: auto-merge waits on it forever, and no other release of that repository opens meanwhile;
- repositories with unreleased entries and no release pull request, including those with releases off;
- repositories with releases on that could not be released, and why.

**Credentials.** The job first mints a token that reads code and pull requests and writes issues, and plans with it. Only if something is due does it mint a second token that can push and publish, limited to the repositories that need it this hour. No model runs and nothing from the repository is executed.

## Example

Turn on automatic patch and minor releases for one repository:

```bash
gh repo edit crunchtools/mcp-pcloud --add-topic ashigaru-release-auto
```

See what the next sweep would do, without doing it:

```bash
gh workflow run sweep.yml -R crunchtools/ashigaru -f dry_run=true
```

## Related

- [Sweep](sweep.md)
- [Authority and limits](authority.md)
- [Enrollment](enrollment.md)
