# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

BunkerWeb's Web UI — a Flask application for managing the BunkerWeb WAF. Server-rendered with Jinja2 templates and vanilla JS (no SPA framework, no bundler). Runs on Gunicorn with threaded workers.

## Development

### Run the full dev stack (UI + API + scheduler + BunkerWeb + MariaDB)

```bash
docker compose -f misc/dev/docker-compose.ui.api.yml up -d
# UI: http://localhost:7000 (admin / P@ssw0rd)
# API: http://localhost:8888 (admin / P@ssw0rd)
```

The dev compose mounts `src/ui/app/`, `src/ui/utils/`, and `src/ui/main.py` as read-only volumes. Code changes require a container restart (`docker compose restart bw-ui`), not a rebuild.

### Rebuild after dependency or Dockerfile changes

```bash
docker compose -f misc/dev/docker-compose.ui.api.yml up -d --build bw-ui
```

### Linting & formatting

```bash
pre-commit run --all-files          # All hooks
black src/ui/                       # Python (160 char lines)
flake8 src/ui/                      # Python lint
prettier --write "src/ui/**/*.{js,css,html,json}"  # Frontend
```

No ESLint or Stylelint — only Prettier for JS/CSS. No unit test suite; testing is integration-only via Docker (see root CLAUDE.md).

## Architecture

### Entry point: `main.py`

`DynamicFlask` — custom Flask subclass that supports dynamic blueprint reloading with priority (pro > external > core plugins). Registers 20 core blueprints, configures middleware stack, and sets up plugin hooks.

### Dependency injection: `app/dependencies.py`

Four global singletons used by all routes:

| Object | Type | Purpose |
|--------|------|---------|
| `DB` | `UIDatabase` | SQLAlchemy ORM wrapper (extends `common/db/Database.py`) |
| `DATA` | `UIData` | JSON file at `/var/tmp/bunkerweb/ui_data.json` — shared transient state (reload flags, flash messages) |
| `BW_CONFIG` | `Config` | Settings & config builder with validation |
| `BW_INSTANCES_UTILS` | `InstancesUtils` | Instance metrics, Redis access |

Also provides `CONFIG_TASKS_EXECUTOR` (ThreadPoolExecutor, 4 workers) for non-blocking config tasks.

### Routes: `app/routes/`

20 Flask Blueprints. Common patterns:

- Import singletons from `app.dependencies`
- `@login_required` on all authenticated routes
- `verify_data_in_form()` for POST validation (from `app/routes/utils.py`)
- `handle_error()` for error flash + redirect
- Long-running operations submit to `CONFIG_TASKS_EXECUTOR`, set `DATA["RELOADING"] = True`, redirect to loading page
- `@cors_required` decorator on JSON/streaming endpoints (requires `Sec-Fetch-Mode: cors` or `X-Requested-With: XMLHttpRequest`)

### Async task flow (plugins, services, configs)

```
Route → DATA["RELOADING"] = True → CONFIG_TASKS_EXECUTOR.submit(task)
  → redirect to loading page → loading page polls DATA → task sets DATA["RELOADING"] = False
  → loading page redirects to final page with flash messages from DATA["TO_FLASH"]
```

### Models: `app/models/`

| File | Purpose |
|------|---------|
| `ui_database.py` | `UIDatabase` — UI-specific DB methods (users, sessions, roles, permissions) |
| `models.py` | `UiUsers` (extends SQLAlchemy `Users` + Flask-Login `UserMixin`), `AnonymousUser` |
| `config.py` | `Config` — configuration builder and validator |
| `instance.py` | `InstancesUtils` — instance aggregation, Redis operations |
| `ui_data.py` | `UIData` — JSON file store for cross-process state |
| `biscuit.py` | Biscuit token auth (RBAC middleware) |
| `totp.py` | TOTP/2FA management |
| `reverse_proxied.py` | WSGI middleware for X-Forwarded-* headers |
| `template.py` | `get_ui_templates()` — template wizard functionality |
| `safe_session_cache.py` | `SafeFileSystemCache` — secure JSON-serialized session storage fallback |

### Authentication

- Flask-Login with `session_protection = "strong"` (validates IP + User-Agent per request)
- Sessions stored in Redis (if available) or `SafeFileSystemCache`
- Session cookie: `__Host-bw_ui_session` (Secure, HttpOnly, SameSite=Lax)
- Biscuit tokens for RBAC (role mapped to read/write operations)
- TOTP 2FA support with recovery codes
- CSRF via Flask-WTF on all POST requests

### Plugin hook system

Plugins can extend the UI by providing `ui/hooks.py` and `ui/blueprints/`. Hook types: `before_request`, `after_request`, `teardown_request`, `context_processor`, `scripts`, `styles`. Hooks are deduplicated by `(module, qualname)`. Higher-priority plugin blueprints override lower-priority ones with the same name.

### Frontend

- **JS**: Vanilla JavaScript + jQuery. Page-specific scripts in `app/static/js/pages/`. No modules or bundling.
- **CSS**: Bootstrap 5.3.3 + custom CSS variables in `overrides.css`. Dark mode via `data-bs-theme` attribute.
- **i18n**: i18next with 17 language JSON files in `app/static/locales/`. Elements use `data-i18n` attributes.
- **Libs**: Vendored in `app/static/libs/` (DataTables, Ace editor, ApexCharts, Leaflet, Flatpickr, etc.)
- **Minification**: Only during Docker build (UglifyJS for JS, cssnano for CSS). Skip with `SKIP_MINIFY=yes` build arg.

### Gunicorn: `utils/gunicorn.conf.py`

`gthread` worker class (multi-threaded). Workers = CPU count - 1, threads = workers * 2. Port 7000. TLS support via env vars. `post_fork()` hook disposes inherited DB connections.

## Key utilities

- `app/utils.py`: `flash()` (enhanced with i18n), `get_multiples()` (parse numbered settings like `REVERSE_PROXY_URL_1`), `get_blacklisted_settings()`, `is_editable_method()`, password hashing (bcrypt, rounds=13)
- `app/routes/utils.py`: `verify_data_in_form()`, `handle_error()`, `wait_applying()` (polls DB metadata until scheduler is idle), `extract_file_setting_names()`

## Important env vars for the UI

| Variable | Default | Purpose |
|----------|---------|---------|
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | — | Initial admin credentials |
| `DATABASE_URI` | sqlite | SQLAlchemy connection string |
| `FLASK_SECRET` | generated | Session signing key |
| `SESSION_LIFETIME_HOURS` | 12 | Session duration |
| `MAX_WORKERS` / `MAX_THREADS` | auto | Gunicorn workers/threads |
| `UI_MAX_CONTENT_LENGTH` | 50MB | Max upload size |
| `UI_SSL_ENABLED` / `UI_SSL_CERTFILE` / `UI_SSL_KEYFILE` | — | TLS config |
| `DEBUG` | — | Flask debug mode |
