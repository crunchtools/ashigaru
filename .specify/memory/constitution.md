# ashigaru Constitution

> **Version:** 2.6.0
> **Ratified:** 2026-10-10
> **Status:** Active
> **Inherits:** [crunchtools/constitution](https://github.com/crunchtools/constitution) v1.24.0
> **Profile:** Workflow Automation

This file holds what is specific to ashigaru. The fleet rules and the Workflow
Automation profile apply at the inherited version and are checked against this
repo's files by `constitution.yml`. They are not restated here.

## Purpose

Reusable workflows that triage a repository's issues and fix the bug-class
ones: open the pull request, answer the review, and leave the merge to GitHub's
required checks. Feature work is labeled and left for a maintainer.

## Authority Split

- **Agent jobs** (`Classify`, `Code agent`, `Fix agent`, `Read the pull
  request`) run Claude Code with the job token at read-only permissions and
  `persist-credentials: false`. Their only other secret is the Claude token,
  plus the reviewer key inside the pre-commit hook step of `Code agent`. Tools
  default to file tools; the two agents that read text from outside
  contributors, triage and review, cannot edit. No agent's file tools may read
  `/proc` or `/sys`, where the process environment would give the token back.
- **Writing jobs** (`Apply verdict`, `Open pull request`, `Push and answer
  findings`, `Queue untriaged issues`, `Post the review`) run no model. The two
  publishing jobs hold the Ashigaru app token, minted per job with contents,
  issues and pull-requests write and nothing else; the sweep mints it with
  issues write, pull-requests read and contents read; the review publisher
  mints it with pull-requests write alone, since a comment is all it leaves.
- What crosses from an agent job to a writing job is schema-validated
  structured output and a patch file. The writing job applies the patch on a
  fresh runner and refuses one that touches `.github/`.
- No job merges. The app has no Workflows permission and is not an
  organization admin.

## Configured Limits

| Limit | Value | Test |
|-------|-------|------|
| Switch `ASHIGARU_ENABLED` on the first job of every workflow | must be `true` | `test_the_switch_gates_the_first_job_and_every_job_has_a_timeout` |
| Turn limit and tool list on every agent step | 40 or 80 turns | `test_every_agent_step_has_a_turn_limit_and_a_tool_list` |
| Job timeouts | 5 to 75 minutes | same as the switch |
| Fix rounds per pull request | 5, then `needs-human` | `test_round_cap_hands_off_instead_of_fixing` |
| Issues queued for triage per sweep | 20 at most | `test_no_run_queues_more_than_the_hard_maximum` |
| Fixes started by the sweep per hour | 5 by default, less those opened in the past hour | `test_the_hourly_budget_is_what_the_past_hour_left` |
| Authors without write access | triaged and reviewed by agents that can only read; never reach the code agent unless someone with write access adds `ready-to-code` | `tests/test_guard.py`, `test_an_agent_that_reads_outside_text_cannot_edit` |
| Outside pull requests reviewed per sweep | 5 | `test_no_sweep_reviews_more_than_its_share_and_the_oldest_go_first` |
| File tools on `/proc` and `/sys` | denied in every agent step | `test_no_agent_can_read_its_own_environment` |

## Public Interface

Label names (`ready-for-triage`, `ready-to-code`, `triaged`, `needs-info`,
`needs-human`), workflow inputs and secrets, the `ashigaru/` branch prefix, the
round marker comment and the job names are what consumers and branch rules
depend on. Changing one is a MAJOR release.

## Release Coupling

A called workflow cannot name its own ref. Callers use the major tag (`v2`) and
each reusable workflow fetches the scripts at that same literal tag;
`tests/test_workflows.py` holds the workflows, the example and this repo's
caller to it. Publishing a release moves the major tag, so a release reaches
every enrolled repo at once and is undone by moving the tag back.
