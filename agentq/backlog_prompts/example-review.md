Scope: the workspace {workspace}. This is a read-only review of the recent changes in this checkout.
Do not edit, commit, push, or open pull requests. Do not run the full test suite.

Steps:
1. Run `git -C {workspace} log --since=7.days --oneline` and read the commits that changed code.
2. For each changed file, look for correctness defects: wrong results, unhandled errors, and races.
3. Write the report to {report_dir}/example-review-<UTC timestamp>.md. Create the folder if it is missing.
   Use one bullet per finding: `- **high** \`path:line\` - title - why: one sentence`.
4. End the report with `VERDICT: PASS` when the whole range was read, or `VERDICT: PARTIAL` and what was not read.
