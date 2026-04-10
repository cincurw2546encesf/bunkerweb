# BunkerWeb + Authentik (Forward Auth, Domain Level)

This example protects two demo applications (`app1` and `app2`) behind a single [Authentik](https://goauthentik.io/) instance using the [Forward auth (domain level)](https://goauthentik.io/docs/providers/proxy/forward_auth) mode. [BunkerWeb](https://www.bunkerweb.io) sits in front of everything as the reverse proxy and Web Application Firewall, calls the Authentik outpost on each request via `auth_request`, and redirects unauthenticated users to the Authentik sign-in flow.

The Authentik stack (`server`, `worker`, `postgresql`) tracks the [upstream docker-compose reference](https://docs.goauthentik.io/install-config/install/docker-compose) for `2026.2+` and no longer needs Redis.

See the [BunkerWeb documentation](https://docs.bunkerweb.io) for the full configuration reference.
