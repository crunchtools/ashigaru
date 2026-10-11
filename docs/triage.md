# Triage

Triage reads a new issue against the code and gives it one state label. It is the gate that decides whether an issue is fixed automatically or waits for a person.

## Why it matters

Issues filed in passing pile up unlabeled, and nobody goes back to sort them. Sorting is cheap for a model that can read the code, and a wrong sort is cheap to correct as long as the default is cautious: when in doubt, the issue goes to a human.

## How it works

`triage.yml` has two jobs. `Classify` runs the agent with a read-only token and the tools `Read`, `Glob` and `Grep`; it returns a verdict as structured output. Images in the issue are downloaded for it beforehand and listed in the prompt, so a screenshot counts as evidence; the agent itself has no network. `Apply verdict` runs no model: `scripts/triage_apply.py` turns the verdict into a label and a comment.

| Verdict | Label | What happens next |
|---------|-------|-------------------|
| `bug`, `documentation` or `performance`, confidence at or above `min_confidence` | `ready-to-code` | The caller chains [code](code.md) in the same run |
| `feature`, or any category below the threshold, or a malformed verdict | `triaged` | Waits. Add `ready-to-code` to have it fixed |
| `question`, `unclear`, or `needs_info` | `needs-info` | Waits. Answer, then add `ready-for-triage` |

The matching type label (`bug`, `documentation`, `performance`, `enhancement` for a feature, `question`) is added, and created when the repository lacks it.

Triage runs when an issue is opened, and when `ready-for-triage` is added.

**Issues from outside.** An issue from an account without write access is triaged the same way, with one difference: a verdict that would have been `ready-to-code` becomes `triaged`, with a comment saying Ashigaru can fix it once a maintainer adds `ready-to-code`. The triage agent can only read, so classifying a stranger's report is safe; writing code from it is a maintainer's decision. An issue written by a bot without write access is not read. See [authority](authority.md).

## Example

```yaml
triage:
  uses: crunchtools/ashigaru/.github/workflows/triage.yml@v2.5.0
  with:
    auto_code_categories: "bug,documentation"   # keep performance work for a person
    min_confidence: "0.8"
  secrets:
    CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
```

## Related

- [Code](code.md): what `ready-to-code` starts
- [Sweep](sweep.md): how old issues reach triage
- [Enrollment](enrollment.md): all inputs
