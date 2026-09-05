# OPNsense MCP

A complete, generic [Model Context Protocol](https://modelcontextprotocol.io)
server that exposes the [OPNsense](https://opnsense.org) REST API to AI agents
(opencode / Claude / etc.) and to scripts. One server instance talks to **one**
firewall; run a second instance to manage a second box.

Built on `FastMCP` (the `mcp` SDK) and `requests`, matching the other custom
lab MCPs (aruba, virsh).

## Why it exists

OPNsense's management is a web UI plus a REST API under
`/api/<module>/<controller>/<command>`. This MCP wraps that API so an agent can
read state, and safely add/delete firewall rules, VLANs, NAT entries, services,
backups, and diagnostics — without a browser or console. A generic
`opnsense_api` escape hatch means *any* documented endpoint is reachable even if
there is no first-class tool for it.

## Features

- **~430 first-class tools** grouped by area:
  - system: status, dashboard, version info, reboot, halt, firmware
    (check/status/upgrade/audit/changelog/log, plugin sync, package
    install/remove/lock/details/license), tunables
  - interfaces: list, config, names, statistics, reload, VLANs (list/add/delete),
    LAGG (list/add/delete), logical-interface assignment CRUD (26.7
    `interfaces/assignment`), interface settings, DHCPv6
  - firewall: rules (list/search/get/add/set/delete/toggle, per-rule stats,
    apply), aliases (CRUD + live pf-table `alias_util` add/list/delete/flush/
    find_references), groups (CRUD)
  - NAT: port-forward (d_NAT) add/list/delete, 1:1 NAT, outbound (source) NAT
  - routing: static gateways (CRUD + status), static routes (CRUD + toggle),
    routing settings
  - DHCP/DNS: dnsmasq leases/status/config (hosts, domains, options, ranges,
    tags); unbound (DNS) full settings, forwarders, host overrides, host
    aliases, ACLs, blocklists, DNSBL, stats
  - services: list, start, stop, restart
  - backup: list, providers, download
  - diagnostics: routes, ARP, pf states, pf statistics, memory, system time,
    ping jobs (set/start/stop/remove/search), portprobe, traceroute,
    interface statistics
  - auth: user search, web password reset, NTP server set (web-session),
    ntpd status, privilege CRUD
  - monitoring: monit (config, services, alerts, tests, status, reconfigure)
  - logging: syslog (config, destinations, status)
  - traffic shaping: shaper config, pipes, queues, rules, statistics
  - certificates: trusted CA search/add/delete (raw dump), certificate search/
    add/delete (raw dump), CRL search, trust settings
  - VPN: WireGuard (general settings, server/client CRUD, key pairs, status,
    reconfigure), IPSec (connections, pools, key pairs, PSKs, leases, phase1/
    phase2 sessions, swanctl, status, reconfigure), OpenVPN (instances,
    static keys, sessions, reconfigure)
  - IDS/IPS: settings, rule sets, installed rules, user rules, policies,
    alert logs, status, reconfigure
- **`opnsense_api`** — call any `/api/...` endpoint (GET/POST) directly.
- **`opnsense_ping`** — one-call health check (confirms auth + reachability).
- Every tool returns a normalized `{ok, status, data, error?}` object.

## Install

```sh
cd opnsense-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configure

The server reads these environment variables (per instance):

| Variable            | Meaning                                  | Default |
|---------------------|------------------------------------------|---------|
| `OPNSENSE_HOST`     | firewall IP/hostname (**required**)      | —       |
| `OPNSENSE_KEY`      | API key → HTTP Basic username (**required**) | —   |
| `OPNSENSE_SECRET`   | API secret → HTTP Basic password (**required**) | — |
| `OPNSENSE_SCHEME`   | `http` or `https`                        | `http`  |
| `OPNSENSE_PORT`     | port                                     | `80`/`443` |
| `OPNSENSE_VERIFY_SSL` | TLS verify (`true`/`false`)            | `false` |
| `OPNSENSE_TIMEOUT`  | request timeout seconds                  | `60`    |

Create an API credential on the firewall: **System → Access → API Credentials**,
then put the key/secret somewhere private. In this lab they live in
`secrets/` (mode 700) and are loaded by the launch wrappers — never hard-code
them.

## Wire into opencode

`opencode.json` (one entry per firewall):

```json
"mcp": {
  "opnsense-roshi":   { "type": "local", "command": ["/path/to/run-opnsense-roshi.sh"] },
  "opnsense-picolo":  { "type": "local", "command": ["/path/to/run-opnsense-picolo.sh"] }
}
```

The bundled wrappers (`run-opnsense-roshi.sh`, `run-opnsense-picolo.sh`) show the
pattern: `export` the env vars (reading the key/secret from a private file), then
`exec .venv/bin/python server.py`. Edit the host + secret file path per box.
Restart opencode so the new MCP servers load.

## Verify

```sh
.venv/bin/python - <<'PY'
# spawn server.py with the env set, then call opnsense_ping
PY
```

A successful `opnsense_ping` returns `{"ok": true, "status": 200, ...}`.

## Safety

Firewall writes are real. Prefer the read tools first, stage changes, and use
`opnsense_apply_firewall` only when you intend to push staged rule changes to
the live firewall. Keep the box's API key/secret private.

The web-session tools (`opnsense_set_timeservers`, `opnsense_set_user_password`)
log in through the web UI because some settings (notably `system.timeservers`)
have no REST endpoint. They take the web password as a **file path** argument
(`password_file`) and never accept it inline — keep those files private.

## API reference

The authoritative endpoint list (module/controller/command) is the official
OPNsense API reference: https://docs.opnsense.org/development/api.html — consult
it (and the per-module pages) when a first-class tool is missing; reach the
endpoint via `opnsense_api`.

## AI Attribution

This work was generated by a human-guided AI assistant. The model is
Qwen3.8-27B-Q4 (pooled/qwen3.8-27b-q4), hosted locally on two NVIDIA
GeForce GTX 1080 Ti GPUs (drivers 580.159.03 and 580.173.02).
No cloud APIs were used.
