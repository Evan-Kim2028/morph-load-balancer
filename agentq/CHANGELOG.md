# Changelog

## v0.2.0 (2026-11-14)

- install.sh and uninstall.sh only touch launchd services rendered from their own checkout (`--force` overrides).
- `examples/pi-models.json` shows the pi provider entry for the local proxy.
- Worker shims refuse full test suites, full gates, whole-repo pyright, pkill and killall.
- grep, rg and find refuse broad roots. Refusals are logged to `guard_blocks.tsv`; `AGENTQ_GUARD=0` turns the guards off.
- Sleep pacing: a refused sleep still pauses, so a polling loop cannot spin. The per-job sleep budget is 30 min (`AGENTQ_SLEEP_BUDGET_S`, default 1800). Whole-second sleeps up to 120 s take a fast path.
- A `BLOCKED:` laptop guard ends the run with a BLOCKED verdict instead of a retry loop.
- Memory cap polls only the job's process group, every 2-5 s, without awk.
- Pi workers start with `--no-skills`. Set `AGENTQ_PI_SKILLS=1` to opt out.
- Relaunches are paced.
- The filler ignores jobs held by local gates, so real jobs waiting on a local gate no longer keep filler at zero.
- Urgent and normal jobs split 70/30, so urgent work cannot starve normal jobs.
- Proxy: client deaths and upstream failures are attributed separately. Aborted prompts are charged, split into uncached input and cache reads.
- Proxy default `target_ceiling` is 0.98 (was 1.5).
- Filler default `target_billed_per_min` is 9,400,000 (was 8,000,000). The filler steps in below 8.46M (`low_frac` 0.9) and stops near 9.12M (`high_frac` 0.97).
- Proxy default `total` budget uses a rolling 60 s window, set explicitly. A top-level `window_mode` no longer reaches `total`; set `budgets.total.window_mode` to change it.
- Dash: summaries stay warm while idle, and stale answers refetch on return.
- Dash: the long caption under the capacity charts is removed.

## v0.1.0

- First public release: queue and broker, Morph proxy, filler, dashboard, worker guards, memcap, housekeeping, install and uninstall scripts.
