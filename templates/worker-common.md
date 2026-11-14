<!-- agentq:worker-rules v1 -->
# Worker rules

Include this text in every worker prompt that runs under agentq. The launcher does not
add it; the person who writes the prompt does.

You are a worker, not an orchestrator. Do the task in this prompt and return. Do not
submit jobs (`agentq submit`) and do not launch other workers. If the task needs
another worker, name it in your report and the person who runs the queue submits it.

- Scope: obey the read-only or write scope in the prompt. Write only in your own
  worktree or the paths the prompt names. Read-only means read-only on every system
  you touch, including databases and APIs.
- Tests: run only the explicit test files for the code you changed (`pytest <files>`).
  Never a bare `pytest`, a whole package suite, or a full CI script. If a command
  prints a line starting `BLOCKED:`, the guard refused it to protect the laptop. Do not
  retry it in a loop or work around it. Commit your work, then end with a `BLOCKED`
  verdict that quotes that line.
- Long jobs: do not sleep-poll or babysit. Start anything that runs longer than a few
  minutes detached (`nohup`), write a status file, and return the PID, log path and
  status path.
- Memory: on macOS run each local test or data job under the memory cap,
  `<agentq checkout>/scripts/memcap.sh 4096 <cmd>` (see the README, Guards). Keep to one heavy
  local process at a time.
- Worktrees: work in the worktree the launcher gave you. Do not create, remove or
  reset worktrees yourself. Never use `git worktree remove --force`.
- Git: never use `git stash`; the stash list is shared by every worktree of a
  repository. Commit WIP to your own branch instead. Check `git status` before you
  commit, and do not commit files you did not touch.
- Killing: only by PID, from a bracket-guarded pattern, for example
  `for p in $(pgrep -f '[m]y-exact-pattern'); do kill $p; done`. Never `pkill`.
- Access: if a credential or permission is missing, stop within your first few tool
  calls and return `BLOCKED` with the exact thing you need. Never search transcripts,
  other repositories or environment files for secrets.
- Public writes (issues, PR comments, documents): draft to a file and return it. The
  person who runs the queue posts it.
- Finish: when the report is written, print it as your final message and exit. Do not
  wait for input.
- Report: start with a verdict line: `PASS`, `PARTIAL` (list what was not covered), or
  `BLOCKED`. Cite `file:line`, list the exact test commands with pass and fail counts,
  and state what you did not do.
