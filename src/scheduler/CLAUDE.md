# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See also the [root CLAUDE.md](../../CLAUDE.md) for project-wide architecture, build commands, and conventions.

## What This Component Does

The Scheduler is BunkerWeb's central orchestrator — the "brain" of the system. It:

1. Saves configuration to the database on startup
2. Runs plugin jobs (download blocklists, renew certificates, etc.) on schedules (`once`, `minute`, `hour`, `day`, `week`)
3. Generates NGINX configs via the Configurator/Templator pipeline
4. Distributes configs, plugins, caches, and custom configs to BunkerWeb instances via their API
5. Monitors instance health and re-sends config to instances that come back online
6. Handles failover: backs up known-good configs and restores them when a reload fails
7. Polls the database for changes (plugins, configs, instances) and triggers reload cycles

## File Overview

| File | Purpose |
|---|---|
| `main.py` (~1400 lines) | Entry point. Contains the main event loop, signal handlers, config generation orchestration, healthcheck logic, failover backup/restore, and change detection polling |
| `JobScheduler.py` (~520 lines) | `JobScheduler` class extending `ApiCaller`. Discovers jobs from `plugin.json` files, validates them, dynamically imports and executes job modules with caching, manages the `schedule` library, and handles NGINX reloads after jobs that request it |
| `entrypoint.sh` | Docker entrypoint. Runs Alembic database migrations, detects integration mode, then launches `main.py` |
| `Dockerfile` | Multi-stage build: installs Python deps (including certbot + DNS plugins), copies shared code, sets up data directories with correct permissions |
| `requirements.in` | Direct dependencies — notably `schedule`, `certbot` + many DNS plugins, `cryptography`, `maxminddb`, `pydantic` |

## Architecture Details

### Main Loop Flow

```
Startup → save_config → wait for DB init → restore caches → check custom configs/plugins
    → generate NGINX config → send to instances → reload → run once-jobs
    → enter polling loop:
        sleep(1) → run_pending (scheduled jobs) → check DB metadata for changes
        → if changes detected: set NEED_RELOAD, break inner loop
        → regenerate affected artifacts → send to instances → reload → failover if needed
```

### Key Global State in main.py

- `SCHEDULER` (JobScheduler): the singleton orchestrator, holds DB connection, API list, environment
- `APPLYING_CHANGES` / `BACKING_UP_FAILOVER` (Events): coordination flags preventing concurrent operations
- `SCHEDULER_LOCK` (Lock): protects `SCHEDULER.apis` list mutations
- `SCHEDULER_TASKS_EXECUTOR` (ThreadPoolExecutor, 4 workers): runs config sending, plugin checks, and healthchecks in parallel
- Flags: `FIRST_START`, `CONFIG_NEED_GENERATION`, `RUN_JOBS_ONCE`, `NEED_RELOAD`, `PLUGINS_NEED_GENERATION`, etc.

### JobScheduler Internals

- Discovers jobs by globbing `plugin.json` files from three directories: core plugins, external plugins, pro plugins
- Validates job definitions against compiled regexes (name, file patterns) and allowed `every` values
- Executes jobs by dynamically importing Python modules (`spec_from_file_location` + `exec_module`) with a module cache to prevent memory leaks
- Job return codes: `1` = success + needs NGINX reload, `0` = success no reload, `>=2` or `<0` = failure
- Uses `ThreadPoolExecutor` (up to `min(8, effective_cpu_count() * 4)` workers) for parallel job execution
- `run_once()`: runs all "once" jobs, respects async flag per job
- `run_pending()`: runs scheduled jobs that are due, sends cache and reloads NGINX if any job requested it
- `reload()`: clears module cache, resets environment, re-discovers jobs, runs once-jobs, re-schedules periodic jobs

### Signal Handling

- `SIGTERM`/`SIGINT`: waits up to 30s for `APPLYING_CHANGES` to clear, then cleanly shuts down
- `SIGHUP`: triggers `save_config.py` to persist current env vars to the database (used by Linux integration)

### Healthcheck System

- Runs every `HEALTHCHECK_INTERVAL` seconds (default 30)
- Checks each instance via `GET /health`
- If an instance is "loading", sends full config (custom configs, plugins, pro plugins, NGINX confs, cache) and reloads it
- Updates instance status in DB (`up`, `down`, `failover`) and maintains the `SCHEDULER.apis` list

### Failover Mechanism

- After a successful reload, copies config/custom_configs/cache to `/var/tmp/bunkerweb/failover/` and caches it to DB
- On reload failure, restores from failover path and attempts reload with last-known-good config
- Failover state recorded in DB metadata

### Config Generation Pipeline

The scheduler doesn't generate configs directly — it shells out to:
- `gen/save_config.py`: validates and persists settings to DB
- `gen/main.py`: renders NGINX config files from Jinja2 templates

Both are invoked as subprocesses with a restricted environment (PATH, PYTHONPATH, LOG_LEVEL, DATABASE_URI, TZ, CUSTOM_CONF_* vars only).

### Change Detection

The inner polling loop checks `db.get_metadata()` every 1s (3s if read-only) for these flags:
- `pro_plugins_changed` / `external_plugins_changed`: regenerate plugins, re-run jobs, regenerate config
- `custom_configs_changed`: regenerate custom configs files from DB
- `plugins_config_changed`: regenerate NGINX config, re-run changed plugin jobs
- `instances_changed`: refresh API list, regenerate everything

Changes are tracked with timestamps to avoid reprocessing in read-only mode.

## Development Notes

### Running Locally

The scheduler requires the full BunkerWeb filesystem layout (`/usr/share/bunkerweb/`, `/var/cache/bunkerweb/`, etc.) and running BunkerWeb instances. Use the Docker dev compose:

```bash
docker compose -f misc/dev/docker-compose.ui.api.yml up -d
```

### Dependencies on Shared Code

The scheduler imports from several shared packages via `sys.path` manipulation (not pip installs):
- `src/common/utils/` → `common_utils`, `logger`, `jobs`, `ApiCaller`
- `src/common/db/` → `Database`
- `src/common/api/` → `API`
- `src/common/gen/` → `Configurator`, `Templator` (invoked as subprocesses)

### Testing Considerations

- No unit tests exist for the scheduler; testing is integration-level via `tests/main.py`
- The scheduler is tightly coupled to the database and filesystem — changes should be tested with the full Docker stack
- Job modules are imported dynamically; errors in job files surface as exceptions in `__job_wrapper`

### Common Pitfalls

- `os.environ` is mutated globally by `JobScheduler.env` setter — this affects all threads
- The `SCHEDULER.apis` list is modified from multiple threads (healthcheck, main loop, send_file_to_bunkerweb); protected by `SCHEDULER_LOCK` in some places but not all
- Module caching in `JobScheduler.__module_cache` uses file paths as keys; if a plugin is replaced at the same path, `importlib_reload` is attempted first
- The polling loop catches all `BaseException` with a 5-error threshold before calling `stop(1)`
