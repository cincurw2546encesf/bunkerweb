# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See also the [root CLAUDE.md](../../CLAUDE.md) for project-wide architecture, build commands, and conventions.

## What This Is

The BunkerWeb API — a FastAPI control plane for BunkerWeb (open-source WAF). Manages configuration, instances, plugins, bans, and scheduler artifacts. Lives at `src/api/` within the larger BunkerWeb monorepo.

## Architecture

### Startup Flow

1. `entrypoint.sh` launches Gunicorn with a custom Uvicorn worker (`utils/worker.py`)
2. `utils/gunicorn.conf.py` `on_starting()` hook runs pre-fork: initializes Biscuit EdDSA keys, creates/updates API user in DB, builds ACL cache file, bootstraps permissions from JSON
3. `app/main.py` `create_app()` builds the FastAPI app: mounts middleware (IP whitelist, rate limiter, rate-limit headers), includes the core router, registers error handlers
4. `app/routers/core.py` assembles all sub-routers and conditionally exposes `/auth` only if API users exist in the database

### Request Lifecycle

```
Request → IP Whitelist → Rate Limiter → Auth Guard → Router Handler → Error Normalization → Response
```

All errors normalize to `{"status": "error", "message": "..."}`.

### Three-Tier Authentication (`app/auth/guard.py`)

The `BiscuitWithAdminBearer` guard runs on every request (except `/health`, `/ping`, `/auth/*`, docs endpoints):

1. **HTTP Basic** — if credentials provided, must match an admin API user (bcrypt, 30s cache)
2. **Bearer API_TOKEN** — if `API_TOKEN` env var matches the bearer token, grants full admin access
3. **Biscuit Token** — EdDSA-signed authorization tokens with embedded facts and fine-grained ACL permissions. Issued at `POST /auth`. Verified in `app/auth/biscuit.py` with freshness + IP binding + per-resource permission checks

Skipped paths: `/health`, `/ping`, `/auth/*`, `/docs*`, `/redoc*`, `/openapi.json`, `/openapi.yaml`

### Routers (`app/routers/`)

| Router | Prefix | Purpose |
|--------|--------|---------|
| `core.py` | `/` | Hub — assembles all routers, provides `/ping` and `/health` |
| `auth.py` | `/auth` | Login, Biscuit token issuance |
| `instances.py` | `/instances` | Instance CRUD (sub-routes include `/{hostname}/*`), broadcast reload/stop/ping |
| `services.py` | `/services` | Service config CRUD, convert, export |
| `global_settings.py` | `/global_settings` + `/global_config` | Read/update global settings. `PUT /global_settings/config` updates config, `POST /global_settings/validate` validates without applying |
| `configs.py` | `/configs` | Custom NGINX config management (upload, CRUD) |
| `bans.py` | `/bans` | Ban/unban IPs across instances |
| `plugins.py` | `/plugins` | Plugin list, upload (tar.gz/zip), delete. `GET /plugins?with_data=true` returns base64-encoded plugin data |
| `cache.py` | `/cache` | Job cache file inspection/purge |
| `jobs.py` | `/jobs` | List jobs, trigger execution |
| `users.py` | `/users` | API user CRUD (create, list, update, delete) |
| `system.py` | `/system` | System info, version, reload, restart |
| `templates.py` | `/templates` | Server template CRUD |
| `metadata.py` | `/metadata` | Plugin/setting metadata lookups |

### Configuration Loading (`app/config.py`)

`ApiConfig` (Pydantic `YamlBaseSettings`) loads from multiple sources in precedence order:
1. Environment variables (highest priority)
2. Docker secrets (`/run/secrets`)
3. YAML file (`/etc/bunkerweb/api.yml`)
4. Env file (`/etc/bunkerweb/api.env`)
5. Defaults

Boolean settings accept `yes/no/true/false/1/0/on/off`.

### Shared Dependencies (from `src/common/`)

The API imports from the monorepo's shared modules via `sys.path` manipulation (container paths under `/usr/share/bunkerweb/`):

- `Database` (`src/common/db/Database.py`) — SQLAlchemy ORM wrapper, pooled connections
- `API`, `ApiCaller` (`src/common/api/`) — HTTP clients for communicating with BunkerWeb NGINX instances
- `common_utils` (`src/common/utils/common_utils.py`) — Docker secrets, hashing, version info
- `logger` (`src/common/utils/logger.py`) — Unified logging with syslog support
- `model` (`src/common/db/model.py`) — SQLAlchemy models including `API_users`, `API_permissions`

API-specific models: `app/models/api_database.py` (APIDatabase wraps API user/permission queries).

### Key Singletons (`app/utils.py`)

- `get_db()` — shared `Database` instance for BunkerWeb config/settings
- `get_api_db()` — shared `APIDatabase` instance for API users/permissions
- Both are lazy-initialized, pooled, and closed on app shutdown via the lifespan handler

### Rate Limiting (`app/rate_limit.py`)

Complex engine supporting multiple syntax formats (`10/hour`, `100r/m` NGINX-style), path patterns with wildcards/regex, method filtering, Redis/Redis Sentinel storage, and IP exemptions. Configured via `API_RATE_LIMIT_*` env vars.

## Development

### Running Locally

```bash
# Full stack with API (recommended)
docker compose -f misc/dev/docker-compose.ui.api.yml up -d
```

The dev compose mounts `src/api/app/` as a read-only volume — restart the container to pick up code changes.

Dev credentials: API `admin`/`P@ssw0rd`, DB `bunkerweb`/`secret`.

### Dependencies

```bash
pip install -r src/api/requirements.txt  # compiled from requirements.in
```

Key packages: `fastapi==0.135.1`, `uvicorn==0.41.0`, `gunicorn==25.1.0`, `biscuit-python==0.4.0`, `bcrypt`, `slowapi==0.1.9`, `pydantic==2.12.5`, `pydantic_settings==2.13.1`

### Linting & Formatting

```bash
# From repo root
pre-commit run --all-files

# Or individually
black --line-length 160 src/api/    # Python formatting
flake8 --max-line-length=160 --ignore=E266,E402,E501,E722,W503 src/api/
```

### Testing

No unit tests in `src/api/`. Integration tests run against live Docker environments from the repo root:

```bash
python3 tests/main.py docker
```

### Key File Paths (Container)

| Path | Purpose |
|------|---------|
| `/var/lib/bunkerweb/.api_biscuit_public_key` | Biscuit public key |
| `/var/lib/bunkerweb/.api_biscuit_private_key` | Biscuit private key |
| `/var/lib/bunkerweb/api_acl.json` | ACL cache (generated at startup) |
| `/var/lib/bunkerweb/api_acl_bootstrap.json` | Optional ACL bootstrap file |
| `/var/tmp/bunkerweb/api.healthy` | Health file (created when ready) |
| `/etc/bunkerweb/api.yml` | YAML config file |

## Important Patterns

- **Dependency injection**: FastAPI `Depends()` for DB access (`app/deps.py`), auth guard, API clients
- **Pydantic schemas**: All request/response models in `app/schemas.py` with validators
- **Config types**: Custom configs accept both hyphen and underscore variants (`server-http`/`server_http`), normalized internally to underscores
- **Instance broadcasting**: Routers call BunkerWeb NGINX instances via `ApiCaller` for operations like reload/stop/ban
- **Plugin changes**: Service/config mutations mark plugins as changed in DB, triggering the Scheduler to regenerate NGINX configs
- **`core.py` is NOT the app entry point**: It's the router hub. The actual FastAPI app is created in `main.py`
- **OpenAPI YAML**: Custom `GET /openapi.yaml` endpoint exports the OpenAPI spec as YAML (in addition to the standard JSON `/openapi.json`)
