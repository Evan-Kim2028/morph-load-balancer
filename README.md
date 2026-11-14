# agentq: a Morph load balancer for a swarm of pi workers

agentq runs many pi coding agents on DeepSeek through Morph's API from one laptop, and keeps
them inside the account's per-minute rate limits. You submit prompt files to a local queue.
A broker starts workers as capacity allows. A local proxy meters every request against
Morph's budgets, and a dashboard shows the queue, the budgets and the laptop's load.

## Get started in 5 minutes

You need macOS, Python 3.11 or newer, git, a Morph API key, and the pi CLI.

1. Clone the repository and install the services.

   ```bash
   git clone <this-repo-url> ~/work/morph-load-balancer
   cd ~/work/morph-load-balancer
   ./install.sh
   ```

   The script checks Python, git and `pi`. It creates the state folder
   (`~/.local/state/agentq`), writes a default `broker.json`, asks for the Morph key if the
   Keychain has none (the key is typed hidden and is not printed), and loads four launchd
   services: the broker, the proxy, the dashboard and the housekeeping job. It is safe to
   run again.

2. Point pi at the proxy. Merge `examples/pi-models.json` into pi's model file
   (`~/.pi/agent/models.json`; create it if it does not exist). It adds a provider named `morph`
   whose base URL is the local proxy. The `apiKey` there is a placeholder: the proxy replaces it
   with the real Morph key from the Keychain. The `X-Agentq-Job` header tags each request with
   its job so the dashboard can show tokens per job. The default worker model is
   `morph/morph-dsv41flash-1017` (see Configuration, `models`).

3. Submit a first job. Use any folder that is a git checkout for `--workspace`.

   ```bash
   printf 'List the files in this repository and say what the project does. End with a line that starts with VERDICT: PASS.\n' > /tmp/first.md
   bin/agentq submit /tmp/first.md --workspace ~/work/some-repo
   bin/agentq wait <job-id>
   bin/agentq result <job-id> --print
   ```

4. Open the dashboard at <http://127.0.0.1:18791/>. It shows the running and queued jobs, the
   budgets on the proxy, and the laptop headroom.

If a job waits, `bin/agentq ls` shows why in the detail column, and `bin/agentq status` shows
the daemon, the queue depth and the proxy.

## What it does

- **Queue.** `bin/agentq submit` copies the prompt into the state folder and records a job in
  a SQLite database (`$AGENTQ_HOME/agentq.db`). A job has a kind (`read`, `write` or
  `review`), a tier (`pi-deepseek-flash` by default, `pi-kimi` for review), a resource class
  (`light`, `tests`, `ssh`), a lane and a run label. Run labels are generated from the job
  id unless you pass `--label`. A label is at most 80 characters of letters, digits, underscore, dot,
  plus or hyphen.
- **Broker.** The broker daemon ticks every few seconds. It reconciles running jobs against
  their processes and run folders, then launches queued jobs that pass every admission check.
  Write jobs get their own git worktree under `auto_worktree.root`, unless you pass
  `--in-place`.
- **Lanes and shares.** Each job is in the `urgent`, `normal` or `backlog` lane. While normal
  jobs of a tier are waiting, urgent jobs may hold at most 70% of that tier's running slots
  (`adaptive.urgent_share`). Backlog jobs run only when the proxy has allowance to spare.
- **Proxy.** `agentq/proxy.py` listens on `127.0.0.1:18790` and forwards to Morph. Each
  request is admitted only when every budget it touches has room under the learned target.
  Otherwise it waits in the lane queue. Budgets are per minute:
  - total billed tokens, 10,000,000;
  - DeepSeek output tokens, 70,000, with a reserve of 210,000;
  - DeepSeek uncached input tokens, 400,000, with a reserve of 1,200,000;
  - Kimi output tokens, 6,162, with a reserve of 18,486;
  - Kimi uncached input tokens, 24,290, with a reserve of 72,870;
  - requests, 1,000.

  The reserve is a burst allowance. Urgent and normal work may spend it past the per-minute
  allowance and it refills over 30 minutes. Backlog work waits at the allowance and never
  spends the reserve. Upstream 429 responses are retried inside the proxy and do not reach
  pi. A 429 names a budget, and that budget's reserve is set to zero.
- **Adaptive targets.** The proxy learns each budget's real limit from the 429 bodies and
  keeps it in `proxy_state.json`. Its target starts at 98% of the limit. A 429 cuts the target
  by 3%, and the target climbs back in small steps while requests succeed. It stays between
  60% and 150% of the learned limit.
- **Filler.** When real work cannot use the DeepSeek allowance, the filler tops the pool up
  with low-priority audit and review jobs (`agentq/fill.py`, `agentq/filler.py`). It adds
  workers only while the billed rate is below the low mark, and only while every gate is open:
  the proxy is answering, no DeepSeek request is blocked at the proxy, no 429 came in the last
  two minutes, and the laptop is within the filler bars. It never kills a running worker. The
  fill controller is off by default (`fill.enabled`). Filler audit targets come from
  `backlog-seed.json`.
- **Pacing.** Before a DeepSeek launch, the broker estimates the uncached input the launch will
  send in its first request and holds the launch if the minute's uncached budget would be
  exceeded (`pacing`).
- **Laptop headroom.** Before a `tests` or `light` job starts, the broker reads free memory,
  CPU busy fraction and swap. If a bar is breached, the job waits and `detail` names the bar.
  Thresholds are in `laptop`.
- **Slots.** `ssh` and `pytest` slots are laptop-wide counting semaphores that PATH shims take
  before they run. Their ceilings are `slots.ssh` (8) and `slots.tests` (16 for pytest).
- **Worker launcher.** `agentq/run_worker.py` starts one worker detached from the broker. It
  resolves the tier to a model and starts `agentq/launch_pi.py`, which runs
  `pi -p --mode json` with the prompt as the system brief, writes the report and the stream
  under the run folder, and classifies quota exhaustion (`QUOTA-<label>`) separately from a
  transient 429 that pi retried.
- **Housekeeping.** `agentq housekeeping` runs three things. It removes the worktree of each
  finished job that is clean and at least four hours old (`git worktree remove`, never
  `--force`). It deletes stream files of runs that finished more than seven days ago. It rotates
  logs above 10 MB. The launchd job runs it every two hours with `--apply`. Without `--apply`
  it only reports.
- **Dashboard.** `agentq/dash.py` serves one page and a JSON summary on `127.0.0.1:18791`.
  It reads the database, the proxy status and the laptop gauges.

## Guards

Workers run with the agentq PATH shims in front of the real commands, so a worker cannot run
the expensive or destructive commands by accident. Allowed `grep`, `rg`, `find` and short
`sleep` calls are decided in plain sh and never start Python. A refused command exits with status 2, prints one
`BLOCKED:` line on stderr, and appends a line to `$AGENTQ_HOME/guard_blocks.tsv`.

| Command | Refused when |
|---|---|
| `pytest` | no test target, a directory target, or more than 25 test files. Allowed: explicit test files. |
| `ci-local.sh`, `validate.sh` | always, through `uv run`, `python3` or a bare name. These are whole-repo gates. |
| `select_pytests.py --all` | always |
| `pre-commit` | with `--all-files` or `-a` |
| `pyright` | with no file arguments |
| `sleep` | one sleep longer than 120 seconds, or a job's sleeps adding up past `AGENTQ_SLEEP_BUDGET_S` (default 1800). A refused sleep still waits up to 10 seconds, so a polling loop cannot spin. |
| `grep` | recursive (`-r`, `-R`, `--recursive`, `-d recurse`) over a git checkout root or a broad folder. Use `rg` or `git grep`, or grep a subfolder. |
| `rg`, `find` | the search root is `/`, `/Users`, `/System`, `/Library`, `/private`, `/Volumes`, `$HOME`, `$HOME/Documents`, `$HOME/Documents/projects`, `$HOME/Documents/worktrees` or one of its children, or a folder listed in `$AGENTQ_HOME/guard_broad_roots` |
| `pkill`, `killall` | always. On macOS `pkill -f "x" -P 1` reads `1` as a second pattern and signals every process with a 1 in its command line. Kill by PID from a bracket-guarded pattern: `for p in $(pgrep -f '[m]y-pattern'); do kill $p; done` |
| `ssh` | none refused. A run to a host in `shims.ssh_gated_hosts` first takes an `ssh` slot; any other ssh run runs unchanged. |

Test runs over the guard are wrapped in `scripts/memcap.sh 8192`, a memory cap for macOS, which
kills the process group past the cap and exits 137. `AGENTQ_SHIMS=0` turns the shims off for a
process tree. `AGENTQ_GUARD=0` turns every refusal off.

The guard list is in `agentq/shims/dispatch.py`. Add a rule there and a test in
`tests/test_agentq_shims.py`.

## Setup

### Requirements

- macOS for the launchd services. Linux is covered under "Run without launchd".
- Python 3.11 or newer. The installer checks; `python3 --version` on a fresh Mac may still show 3.9.
- git.
- The pi coding agent CLI, which provides `pi`. The launcher uses `pi -p --mode json`. Install it
  with `npm install -g @mariozechner/pi-coding-agent` or from the project's instructions, and check
  that `command -v pi` prints a path.
- A Morph API key with access to the DeepSeek and Kimi models.

### The Morph key

The proxy reads the key from the macOS Keychain, with the service name `morph-api-key` and your
login name as the account. `install.sh` stores it for you when it is missing. To store it yourself:

```bash
security add-generic-password -a "$USER" -s morph-api-key -w
```

Leave off the value after `-w`. macOS then asks for it at a hidden prompt, so the key never
appears in your shell history or the process list. To use another key for one run, set
`AGENTQ_UPSTREAM_KEY` in the environment of the proxy. The proxy never logs or prints the key, and
the error messages name the source, not the value.

### Services

`install.sh` renders the templates in `agentq/launchd/` into `~/Library/LaunchAgents/`. The
placeholders are `__HOME__`, `__PYTHON__`, `__REPO__` and `__AGENTQ_HOME__`. The labels are:

| Label | Runs | Port |
|---|---|---|
| `com.agentq.broker` | `python -m agentq.cli daemon` | none |
| `com.agentq.proxy` | `agentq/proxy.py` | 127.0.0.1:18790 |
| `com.agentq.dash` | `agentq/dash.py` | 127.0.0.1:18791 |
| `com.agentq.housekeeping` | `python -m agentq.cli housekeeping --apply`, every 2 hours | none |

To change a template, edit it in the repository and run `./install.sh` again.

### Run without launchd

On Linux, `install.sh` writes the state folder and `broker.json` and then exits. Run the
services yourself, for example in `tmux`, with the same environment:

```bash
export AGENTQ_HOME=~/.local/state/agentq
python3 -m agentq.cli daemon &                  # from the repository root
python3 agentq/proxy.py &
python3 agentq/dash.py --port 18791 &
```

The Keychain is macOS-only. On Linux, set `AGENTQ_UPSTREAM_KEY` for the proxy instead.

## Configuration

### `broker.json`

The file lives in the state folder. Every key is optional: a missing key takes the default
below, and a value of the wrong type is ignored. `install.sh` writes the defaults. The table
gives each key's default and meaning.

| Key | Default | Meaning |
|---|---|---|
| `slots.light` | 40 | ceiling on running `light` jobs (no test runs or ssh) |
| `slots.tests` | 6 | ceiling on running `tests` jobs; also the ceiling on pytest slots for shims (16 when unset) |
| `slots.ssh` | 8 | ceiling on running `ssh` jobs and on ssh slots taken by shims |
| `tiers` | `pi-kimi` 2, `pi-deepseek-flash` 48 | ceiling on running jobs per tier |
| `models` | `pi-deepseek-flash` → `morph/morph-dsv41flash-1017`, `pi-kimi` → `morph/morph-kimik31` | pi model for each tier. Must match a provider and model in pi's configuration. |
| `adaptive.enabled` | true | when false, the fixed ceilings are the only limit |
| `adaptive.max_waiting_requests` | 8 | a Morph job waits while the proxy queue for its budgets is longer than this |
| `adaptive.urgent_share` | 0.7 | share of a tier's running slots urgent jobs may hold while normal jobs wait |
| `adaptive.tests_floor` | 4 | fewest `tests` jobs the adaptive limit lets run, even under pressure |
| `adaptive.gauge_ttl_s` | 15 | how long a memory reading is reused |
| `laptop.tests_max_cpu_busy` | 0.80 | `tests` jobs wait above this CPU busy fraction |
| `laptop.tests_min_free_pct` | 35 | `tests` jobs wait below this free-memory percentage |
| `laptop.light_max_cpu_busy` | 0.92 | `light` jobs wait above this CPU busy fraction |
| `laptop.light_min_free_pct` | 20 | `light` jobs wait below this free-memory percentage |
| `laptop.max_swap_frac` | 0.90 | all jobs wait above this swap-used fraction |
| `laptop.fill_max_cpu_busy` | 0.75 | filler jobs start only below this CPU busy fraction |
| `laptop.fill_min_free_pct` | 20 | filler jobs start only above this free-memory percentage |
| `laptop.fill_max_swap_frac` | 0.85 | filler jobs start only below this swap-used fraction |
| `laptop.sample_s` | 10 | how often the laptop gauges are sampled |
| `laptop.ewma_s` | 60 | smoothing time constant for CPU readings |
| `auto_worktree.enabled` | true | write jobs get their own worktree unless `--in-place` |
| `auto_worktree.root` | `~/agentq-worktrees` | folder for those worktrees |
| `fill.enabled` | false | run the fill controller |
| `fill.window_minutes` | 2 | minutes averaged for the DeepSeek billed rate |
| `fill.target_billed_per_min` | 8,000,000 | billed tokens per minute the controller aims for |
| `fill.max_workers` | 64 | most DeepSeek workers the controller will start |
| `fill.low_frac` | 0.90 | below this fraction of the target, the controller adds workers |
| `fill.high_frac` | 0.97 | above this fraction, growth stops |
| `fill.step_workers` | 4 | workers added per step |
| `fill.step_interval_s` | 120 | seconds between steps |
| `fill.gate_429_window_s` | 120 | a DeepSeek 429 this recent blocks filling |
| `fill.reserve_floor_frac` | 0.20 | the controller does not fill while the reserve is below this fraction |
| `pacing.enabled` | true | hold DeepSeek launches that would exceed the uncached input budget |
| `pacing.uncached_limit` | 400,000 | uncached input tokens per minute the pacing plans against |
| `pacing.uncached_frac` | 0.80 | fraction of that limit the pacing will plan to use |
| `pacing.first_request_uncached` | 40,000 | estimate for a launch's first request, until it is measured |
| `pacing.first_request_grace_s` | 120 | while the proxy answers, a launch with no recorded request counts against the budget only this long after it started |
| `pacing.measure_first_request` | true | measure first requests from recent jobs |
| `pacing.measure_percentile` | 75 | percentile of measured first requests used as the estimate |
| `pacing.min_measured_jobs` | 20 | measurements needed before the measured estimate is used |
| `pacing.measure_window_s` | 86400 | how far back the measurements look |
| `proxy_heal.enabled` | true | restart the proxy with `launchctl kickstart` when it stays unhealthy and the restart is corroborated |
| `proxy_heal.fail_s` | 90 | how long the proxy must refuse, reset or time out, without a break, before a restart is considered |
| `proxy_heal.min_interval_s` | 600 | least time between two restarts |
| `proxy_heal.probe_s` | 10 | probe interval while the proxy is unhealthy |
| `proxy_heal.corroborate_lag_ms` | 2000 | a restart also needs the TCP port to refuse, or the last proxy status to show event-loop lag of at least this many milliseconds |
| `tiers_thinking` | `{}` | per tier, the pi thinking level: `off`, `minimal`, `low`, `medium`, `high`, `xhigh` or `max` |
| `shims.ssh_gated_hosts` | `[]` | host patterns (shell globs) whose ssh runs take an ssh slot. Empty means no host is gated. |

### `proxy.json`

Optional. Read from the state folder over the proxy defaults. Its keys include `port` (18790),
`upstream` (`https://api.morphllm.com/v1`), `budgets` (the table in "What it does", merged key
by key), `inflight` (a limiter on DeepSeek requests in flight, in `shadow` mode by default),
`give_up_s` (240), and `log_max_bytes` and `log_backups` for the proxy's own log. Read the defaults
in `DEFAULT_CONFIG` in `agentq/proxy.py` for the rest.

### `backlog-seed.json`

Optional. Read by `python3 -m agentq.backlog_seed`, which creates the standing review templates
once. Each key is a template name with its `workspace` and `report_dir`. The shipped example is
`example-review`, which reads recent commits of one checkout. A `filler` key holds the repos and
modules the filler audits (see the docstring of `agentq/filler.py` for the shape).

### Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `AGENTQ_HOME` | all | state folder (default `~/.local/state/agentq`) |
| `AGENTQ_PROXY_PORT` | proxy | port (default 18790) |
| `AGENTQ_UPSTREAM` | proxy | Morph base URL |
| `AGENTQ_UPSTREAM_KEY` | proxy | Morph key, instead of the Keychain |
| `AGENTQ_PYTHON` | `bin/agentq`, `install.sh` | Python to run with (3.11+) |
| `AGENTQ_PI_MODEL` | launcher | default pi model for a direct launch |
| `AGENTQ_SHIMS` | launcher | `0` to skip the PATH shims |
| `AGENTQ_GUARD` | shims | `0` to let refused commands through |
| `AGENTQ_MEMCAP` | shims | path to `memcap.sh` (default: the one in `scripts/`) |
| `AGENTQ_MEMCAP_RULES` | `memcap.sh` | optional shell file sourced before the cap runs |
| `AGENTQ_XDIST_CAP` | pytest plugin | most xdist workers a pytest run may use (default 2) |
| `AGENTQ_THINKING` | launcher | thinking level for this run (set by the broker from `tiers_thinking`) |
| `AGENTQ_RESUME_SESSION` | launcher | a dead run's pi session file to continue |

## Command reference

| Command | Does |
|---|---|
| `bin/agentq submit <prompt> [--kind read\|write\|review] [--urgent\|--backlog] [--workspace DIR] [--in-place] [--tier T] [--label L] [--res light\|tests\|ssh] [--max-runtime S]` | queue a job; prints its id |
| `bin/agentq wait <id>... [--mine] [--timeout S]` | block until the jobs finish; exit 3 on timeout |
| `bin/agentq ls` | list jobs with state, lane, tier, label and detail |
| `bin/agentq result <id> [--print]` | path of the report, or the report itself with `--print` |
| `bin/agentq cancel <id>` | drop a queued job, or stop a running one |
| `bin/agentq escalate [<id>\|--mine]` | move jobs to the urgent lane |
| `bin/agentq status` | daemon heartbeat, queue depth, running slots, proxy |
| `bin/agentq slots` | ssh and pytest slots held, with holder pid and label |
| `bin/agentq housekeeping [--apply] [--min-age-h H] [--runs-older-than 7d]` | worktree reaper, stream gc and log rotation |
| `bin/agentq gc [--runs-older-than 7d] [--apply]` | stream files of finished runs only |
| `bin/agentq backlog add\|ls\|rm\|pause\|resume` | recurring jobs |
| `bin/agentq thinking report` | verdicts and output by thinking level |
| `bin/agentq inflight report` | throughput by DeepSeek in-flight limiter arm |

Every command ends with one `ANSWER:` line. Exit codes: 0 ok, 1 a negative fact (unknown job,
failed job), 3 timeout or the store unreachable.

## Troubleshooting

**Rate limits (429s).** The proxy retries a 429 inside itself and pi does not see it. Check
`bin/agentq status` and the proxy panel of the dashboard. A budget whose target has been cut
shows the learned limit next to its live use. Nothing needs fixing unless jobs stop completing.
If a worker's report says `QUOTA-<label>`, the account is out of credit: the run stopped and is
marked BLOCKED. A transient 429 that pi retried is only counted as `rate_limit_retries` in the
launcher's summary line.

**Laptop overload.** A `tests` or `light` job that waits reports the bar it is held by in its
detail column, for example free memory below 35%. Close heavy applications or wait; the job
starts when the reading clears. If `slots` shows every pytest slot held, a run is waiting for a
slot. Check `bin/agentq slots` for the holder's label.

**A job waits forever in `queued`.** Check `bin/agentq status`. If the daemon heartbeat is old,
restart the broker: `launchctl kickstart -k gui/$(id -u)/com.agentq.broker`. If the proxy is
down, the broker's proxy heal may restart it (see `proxy_heal`). You can also restart it by hand:
`launchctl kickstart -k gui/$(id -u)/com.agentq.proxy`.

**Dashboard stale.** The page shows a "stale" notice when its JSON summary stops updating. The
dashboard runs on its own launchd job. Restart it with
`launchctl kickstart -k gui/$(id -u)/com.agentq.dash`. Its log is `~/.local/state/agentq/logs/dash.log`.

**Logs.** The launchd services write to `~/.local/state/agentq/logs/`: `broker.log`, `proxy.log`,
`dash.log` and `housekeeping.log`. The proxy also keeps its own rotating log, `$AGENTQ_HOME/logs/proxy.err.log`.
Each worker writes its stream, report and stderr into its run folder under `$AGENTQ_HOME/jobs/<id>/`.

**A command is refused with BLOCKED.** That is the guard working. Read the line: it names the
rule. Run the explicit test files for your change instead of the full suite.

## Uninstall

```bash
./uninstall.sh            # stops the four services and removes their launchd files
./uninstall.sh --purge    # also deletes the state folder (jobs, logs, broker.json)
security delete-generic-password -a "$USER" -s morph-api-key   # optional: remove the key
```

The repository and any worktrees under `auto_worktree.root` stay. Remove worktrees with
`git worktree remove <path>` from their main checkout.

## Tests

The tests use the standard library only.

```bash
python3 -m unittest discover          # from the repository root
scripts/memcap.sh 4096 python3 -m unittest discover   # the same, under a 4 GB memory cap
python3 -m unittest tests.test_agentq_proxy            # one module
```

They cover the queue and the broker's admission, the proxy's budgets and reserves, the dashboard's
summary, the PATH shims and guards, the pytest plugin, the launcher's argument handling, the reaper
and the CLI. The proxy tests run against a local fake upstream, so no test calls Morph. One test runs
`pi --help` when pi is installed and is skipped otherwise.

## Repository layout

```
agentq/                 the package
  cli.py                the agentq command (bin/agentq runs it)
  db.py                 schema, migrations, job and event writes
  broker.py             the daemon: reconcile, admit, launch
  run_worker.py         starts one worker for the broker
  launch_pi.py          runs one pi worker and writes its report
  proxy.py              the Morph governor (HTTP proxy)
  headroom.py           laptop memory, CPU and swap gauges
  slots.py              ssh and pytest slots
  fill.py, filler.py    the fill controller, filler audits and their labels
  backlog.py, backlog_seed.py  recurring jobs and their seed
  pr_source.py          merged PRs for the filler, through the gh CLI
  gc.py, reaper.py      stream gc, worktree reaper, log rotation
  inflight.py, thinking_report.py  reports for the in-flight limiter and thinking levels
  dash.py               the dashboard
  logfilter.py          base64 and delta filter for pi's event stream
  shims/                PATH shims and the guard dispatcher
  pyplugin/             pytest plugin that takes the pytest slot
  backlog_prompts/      prompt templates for backlog and filler jobs
  launchd/              launchd templates
bin/agentq              command line entry point
scripts/memcap.sh       memory cap for macOS
templates/              worker rules to paste into worker prompts
install.sh, uninstall.sh
broker.example.json     the defaults that install.sh writes
tests/                  unittest suite
```
