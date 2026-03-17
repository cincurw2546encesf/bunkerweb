# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See also the [root CLAUDE.md](../../CLAUDE.md) for project-wide architecture, build commands, and conventions.

## What This Is

Autoconf is BunkerWeb's dynamic configuration component. It watches container/orchestrator events (Docker, Swarm, Kubernetes) and automatically reconfigures BunkerWeb instances by translating labels/annotations into settings and persisting them to the shared database. The Scheduler then picks up these changes.

## Architecture

### Class Hierarchy

```
Config (Config.py)
  └── Controller (controllers/Controller.py)           # abstract base
        ├── DockerController (controllers/DockerController.py)     # standalone Docker
        ├── SwarmController (controllers/SwarmController.py)       # Docker Swarm (deprecated)
        └── KubernetesController (controllers/KubernetesController.py)  # concrete K8s base
              ├── IngressController (controllers/IngressController.py)   # K8s Ingress API
              └── GatewayController (controllers/GatewayController.py)   # K8s Gateway API
```

`Config` handles settings validation, change detection (`update_needed`), database writes (`apply`), and waiting for the Scheduler to finish (`wait_applying`). `Controller` adds instance/service discovery abstractions and the event loop skeleton. Concrete controllers implement platform-specific discovery and event handling.

### Startup Flow (main.py)

1. Detect mode from env vars (`SWARM_MODE`, `KUBERNETES_MODE`, `KUBERNETES_GATEWAY_MODE`)
2. Instantiate the appropriate controller
3. `controller.wait()` — poll until DB is ready and at least one healthy BunkerWeb instance exists
4. `controller.wait_applying(True)` — block until Scheduler finishes any in-progress config apply
5. `controller.process_events()` — enter the infinite event loop

### Event Processing Pattern

All controllers share a debounce pattern (2-second window):
1. Receive platform event (Docker event stream / K8s watch)
2. Filter: only process events with relevant labels/annotations (and matching namespace if `NAMESPACES` is set)
3. Set `pending_apply = True`, record timestamp
4. Sleep until debounce window passes with no new events
5. Re-discover instances, services, configs from the platform
6. If `update_needed()` detects changes, call `apply()` to write to DB
7. `apply()` calls `_db.save_config()`, `_db.save_custom_configs()`, `_db.update_instances()`, then `_db.checked_changes()` to signal the Scheduler

### Label/Annotation Conventions

- **Docker/Swarm**: Labels prefixed with `bunkerweb.` (e.g., `bunkerweb.SERVER_NAME`, `bunkerweb.INSTANCE`)
- **Kubernetes**: Annotations prefixed with `bunkerweb.io/` (e.g., `bunkerweb.io/INSTANCE`, `bunkerweb.io/CONFIG_TYPE`)
- Custom configs use `bunkerweb.CUSTOM_CONF_<TYPE>_<NAME>` labels (Docker) or ConfigMaps with `bunkerweb.io/CONFIG_TYPE` annotation (K8s)
- Ignore-label filtering: `DOCKER_IGNORE_LABELS`, `SWARM_IGNORE_LABELS`, `KUBERNETES_IGNORE_ANNOTATIONS`

### Kubernetes Controllers

**KubernetesController** (base): Watches Pods, ConfigMaps, Services, Secrets. Handles `get_configs()` returning both extra settings (from `settings`-type ConfigMaps) and custom configs. Manages LoadBalancer IP detection for status patching. Uses threaded watchers with retry logic for `410 Gone` (resource version expired).

**IngressController**: Translates Ingress rules into `REVERSE_PROXY_HOST_N`/`REVERSE_PROXY_URL_N` settings. Handles TLS secrets. Watches Ingress resources additionally. Supports `KUBERNETES_INGRESS_CLASS` filtering.

**GatewayController**: Supports Gateway API with HTTPRoute, GRPCRoute, TLSRoute, TCPRoute, UDPRoute. Auto-detects available CRD versions. Resolves Gateway listeners for hostname/port/protocol/TLS. Translates routes into BunkerWeb service configs. Caches gateways per cycle. Patches Gateway status with LoadBalancer addresses.

### Key Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `SWARM_MODE` | `no` | Enable Swarm controller |
| `KUBERNETES_MODE` | `no` | Enable Kubernetes Ingress controller |
| `KUBERNETES_GATEWAY_MODE` | `no` | Enable Kubernetes Gateway API controller |
| `DOCKER_HOST` | `unix:///var/run/docker.sock` | Docker socket path |
| `WAIT_RETRY_INTERVAL` | `5` | Seconds between readiness retries |
| `NAMESPACES` | (all) | Space-separated namespace filter |
| `USE_KUBERNETES_FQDN` | `yes` | Use Pod FQDN vs IP as hostname |
| `KUBERNETES_DOMAIN_NAME` | `cluster.local` | K8s cluster domain |
| `KUBERNETES_SERVICE_PROTOCOL` | `http` | Protocol for backend service URLs |
| `KUBERNETES_REVERSE_PROXY_SUFFIX_START` | `1` | Starting index for numbered reverse proxy settings |

### Database Interaction

Autoconf uses `Database` from `src/common/db/` (added to sys.path at startup). Key patterns:
- Read-only fallback: `_try_database_readonly()` attempts write, falls back to readonly with retry logic
- Change signaling: After writing config, calls `_db.checked_changes()` which sets metadata flags the Scheduler watches
- Settings validation: `_db.is_valid_setting()` validates each setting extracted from labels/annotations

### Threading Model

- **Docker**: Single-threaded event loop on `client.events()` with internal lock for debounce
- **Swarm**: Two threads — one watching `service` events, one watching `config` events, sharing a lock
- **Kubernetes**: One thread per watcher type (pod, configmap, service, secret, ingress/gateway routes), all sharing `_internal_lock`

## Build & Run

```bash
# Build the autoconf Docker image
docker build -f src/autoconf/Dockerfile -t bunkerweb-autoconf:dev .

# Run in dev with Docker autoconf mode
docker compose -f misc/dev/docker-compose.autoconf.yml up -d

# Install Python deps locally (for IDE support)
pip install -r src/autoconf/requirements.in
```

The Dockerfile is a multi-stage build: builder stage compiles Python deps (including DB requirements), final stage runs as `autoconf` user (UID 101).

## Conventions

- Swarm integration is deprecated (logged at startup)
- Health check: writes `/var/tmp/bunkerweb/autoconf.healthy` when event loop starts, removes on exit
- All controllers use `_first_start` flag to unconditionally apply config on the first event cycle
- `_set_autoconf_load_db()` sets `autoconf_loaded` metadata once after first successful apply
