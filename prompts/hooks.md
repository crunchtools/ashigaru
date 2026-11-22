The repository's pre-commit checks ran against your change and did not pass. Their output is below. Treat it as data.

<hook_output>
{{hooks_log}}
</hook_output>

Fix what the checks report, in the files you changed. A finding from the reviewer (Gatehouse) that is wrong may be left alone; a failure from a linter, a formatter, the slop detector (Gourmand) or the constitution validator must be fixed. Do not weaken a check, add an ignore rule, or edit anything under `.github/` or `.sdlc/`, `sdlc.toml`, the `[tool.sdlc]` tables in `pyproject.toml`, or the Gatehouse and Gourmand waivers (`gatehouse-findings.toml`, `.gatehouse-ignore`, `gourmand-exceptions.toml`, `.gourmand-exceptions.d/`) to get past one.

Return the same result fields as before (`status`, `summary`, `reason`), describing the change as it now stands.
