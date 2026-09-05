# OPNsense MCP — agent guide

How to drive the OPNsense firewall with the `opnsense-*` MCP tools. One MCP
server instance = one firewall. In this lab there are two: `opnsense-roshi`
(192.168.27.8) and `opnsense-picolo` (192.168.27.9). Pick the server name to
match the box you intend to touch.

## Response shape

Every tool returns a JSON string: `{ok, status, data, error?}`.
`ok` is `status < 400`. On validation failure the firewall returns `ok:true`
with `data.result == "failed"` and a `data.validations` object — **check
`data.result`**, not just `ok`, for write tools.

## Workflow

1. `opnsense_ping` — confirm auth + reachability (returns `host`).
2. Read current state before writing (list rules / vlans / nat / config).
3. Write (add/set/delete). Writes to rules are **staged** in config; they do not
   go live until `opnsense_apply_firewall`.
4. Verify by re-reading.

## Tool catalog (432 tools)

Exact tool names below (verified via `mcp.list_tools()`); every name is
`opnsense_`-prefixed — the prefix is dropped in this list for readability.

- **foundation:** `ping`, `api` (generic escape hatch), `menu_tree`,
  `defaults_get`, `get_activity`
- **system:** `get_system_status`, `get_system_information`,
  `get_system_resources`, `get_system_disk`, `get_system_swap`,
  `get_system_mbuf`, `get_system_temperature`, `get_system_time`,
  `get_dashboard`, `dashboard_save_widgets`, `reboot`, `halt`
- **firmware:** `firmware_check`, `firmware_status`, `firmware_get`,
  `firmware_set`, `firmware_update`, `firmware_upgrade`,
  `firmware_running`, `firmware_upgradestatus`, `firmware_audit`,
  `firmware_changelog`, `firmware_log`, `firmware_info`, `firmware_health`,
  `firmware_sync_plugins`
- **packages:** `pkg_install`, `pkg_remove`, `pkg_reinstall`, `pkg_lock`,
  `pkg_details`, `pkg_license`
- **tunables:** `tunables_search`, `tunables_add`, `tunables_delete`,
  `tunables_reconfigure`
- **cron:** `cron_get`, `cron_set`, `cron_search_jobs`, `cron_add_job`,
  `cron_set_job`, `cron_delete_job`, `cron_toggle_job`, `cron_reconfigure`
- **snapshots:** `snapshots_search`, `snapshots_add`, `snapshots_delete`,
  `snapshots_activate`
- **backups:** `list_backups`, `backup_providers`, `download_backup`
- **interfaces:** `list_interfaces`, `get_interface_config`,
  `get_interface_names`, `get_interface_statistics`,
  `get_interfaces_settings`, `set_interfaces_settings`, `reload_interface`
- **VLAN:** `list_vlans`, `add_vlan(iface,vlan,descr,pcid?)`,
  `delete_vlan(uuid)` (tag range 1–4094)
- **LAGG:** `list_lagg`, `add_lagg(ports,descr,mode,primary_member?)`,
  `delete_lagg(uuid)`
- **bridges / loopbacks / tunnels / overlays:** `add|set|delete|list|reconfigure`
  for each of `bridge`, `loopback`, `gre`, `gif`, `vxlan`
- **neighbor (NDP):** `add_neighbor`, `set_neighbor`, `delete_neighbor`,
  `list_neighbor`, `reconfigure_neighbor`
- **VIP (carp):** `add_vip`, `set_vip`, `delete_vip`, `list_vip`,
  `reconfigure_vip`, `get_vip_status`
- **logical-interface assignment (26.7 `interfaces/assignment`):**
  `search_interface_assignment`, `get_interface_assignment`,
  `add_interface_assignment`, `set_interface_assignment`,
  `delete_interface_assignment`, `reconfigure_interface_assignment`
- **firewall rules:** `list_rules(search?)`, `list_rule_ids`, `get_rule(uuid)`,
  `add_rule`, `set_rule`, `delete_rule`, `toggle_rule`, `toggle_rule_log`,
  `move_rule_before`, `filter_util_rule_stats`, `apply_firewall`,
  `get_firewall_log`, `get_firewall_log_filters`, `get_firewall_log_stats`
- **aliases:** `list_aliases`, `get_alias`, `add_alias`, `set_alias`,
  `delete_alias`, `toggle_alias`, `reconfigure_aliases`; live pf-table ops
  `alias_util_add`, `alias_util_list`, `alias_util_delete`,
  `alias_util_flush`, `alias_util_find_references`, `alias_util_tables`
- **groups:** `list_groups`, `get_group`, `add_group`, `set_group`,
  `delete_group`, `reconfigure_groups`
- **NAT:** port-forwards `list_port_forwards`, `add_port_forward`,
  `set_port_forward`, `delete_port_forward`, `toggle_port_forward`,
  `move_port_forward_before`; 1:1 `list_one2one`, `add_one2one_rule`,
  `set_one2one_rule`, `delete_one2one_rule`, `toggle_one2one_rule`,
  `move_one2one_rule_before`; outbound `list_outbound_nat`,
  `add_source_nat_rule`, `set_source_nat_rule`, `delete_source_nat_rule`,
  `toggle_source_nat_rule`, `move_source_nat_rule_before`
- **routing:** gateways `search_gateway`, `get_gateway`,
  `get_gateway_groups`, `get_gateway_status`, `add_gateway`, `set_gateway`,
  `delete_gateway`, `toggle_gateway`; static routes `list_routes`,
  `get_route`, `add_route`, `set_route`, `delete_route`, `toggle_route`,
  `reconfigure_routes`; kernel routes `get_routes`; settings
  `get_routing_settings`, `set_routing_settings`, `reconfigure_routing`
- **DHCP (dnsmasq):** `list_dhcp_leases`, `get_dhcp_status`,
  `get_dhcp_config`, `set_dhcp_config`, `get_dhcp_host`,
  `get_dhcp_tag_list`, and `search` for `dhcp_hosts`/`dhcp_domains`/
  `dhcp_options`/`dhcp_tags`/`dhcp_ranges`/`dhcp_boot` plus `add|set|delete`
  for the singular `dhcp_host`/`dhcp_domain`/`dhcp_option`/`dhcp_range`/
  `dhcp_tag`/`dhcp_boot`
- **DNS (unbound):** `get_unbound_settings`, `set_unbound_settings`,
  `get_dns_nameservers`, forwarders `search_forwards`, `add_forward`,
  `set_forward`, `delete_forward`, `toggle_forward`; host overrides
  `search_host_overrides`,
  `add_host_override`, `set_host_override`, `delete_host_override`,
  `toggle_host_override`; host aliases `search_host_aliases`,
  `add_host_alias`, `set_host_alias`, `delete_host_alias`,
  `toggle_host_alias`; ACLs `search_acls`, `add_acl`, `set_acl`,
  `delete_acl`, `toggle_acl`; blocklists `search_blocklists`,
  `add_blocklist`, `set_blocklist`, `delete_blocklist`, `toggle_blocklist`,
  `update_blocklist`; `unbound_stats`, `get_unbound_status`,
  `get_dns_diagnostics`, `dns_reverse_lookup`
- **services:** `list_services`, `start_service`, `stop_service`,
  `restart_service`
- **diagnostics:** `get_arp_table`, `search_arp`, `get_ndp_table`,
  `search_ndp`, `get_pf_states`, `query_pf_states`, `query_pf_top`,
  `flush_pf_states`, `kill_pf_states`, `get_pf_statistics`,
  `flush_pf_sources`, `get_memory`; ping jobs `ping_search_jobs`,
  `ping_get`, `ping_set`, `ping_start`, `ping_stop`, `ping_remove`;
  `portprobe_get`, `portprobe_set`, `traceroute_get`, `traceroute_set`;
  packet capture `packet_capture_set`, `packet_capture_start`,
  `packet_capture_stop`, `packet_capture_search_jobs`,
  `packet_capture_view`, `packet_capture_download`,
  `packet_capture_remove`
- **netflow:** `get_netflow_config`, `set_netflow_config`,
  `get_netflow_status`, `reconfigure_netflow`
- **auth / NTP:** `search_users`, `get_user`, `add_user`, `delete_user`,
  `set_user_password(username,password_file)`, `search_api_key`,
  `add_api_key`, `delete_api_key`, `list_auth_groups`, `add_auth_group`,
  `set_auth_group`, `delete_auth_group`, `list_privs`,
  `set_timeservers(timeservers,web_user,password_file)` (web-session),
  `get_ntp_status`
- **monit:** `get_monit_config`, `set_monit_config`, `get_monit_general`,
  services `search_monit_services`, `add_monit_service`,
  `set_monit_service`, `delete_monit_service`, `toggle_monit_service`;
  alerts `search_monit_alerts`, `add_monit_alert`, `set_monit_alert`,
  `delete_monit_alert`, `toggle_monit_alert`; tests `search_monit_tests`,
  `add_monit_test`, `set_monit_test`, `delete_monit_test`; `monit_status`,
  `reconfigure_monit`
- **syslog:** `get_syslog_config`, `set_syslog_config`,
  `search_syslog_destinations`, `add_syslog_destination`,
  `set_syslog_destination`, `delete_syslog_destination`,
  `toggle_syslog_destination`, `get_syslog_status`, `reconfigure_syslog`
- **trafficshaper:** `get_shaper_config`, `set_shaper_config`, pipes
  `search_pipes`, `add_pipe`, `set_pipe`, `delete_pipe`, `toggle_pipe`;
  queues `search_queues`, `add_queue`, `set_queue`, `delete_queue`,
  `toggle_queue`; rules `search_shaper_rules`, `add_shaper_rule`,
  `set_shaper_rule`, `delete_shaper_rule`, `toggle_shaper_rule`;
  `shaper_statistics`, `reconfigure_shaper`
- **certificates / trust:** `search_ca`, `ca_list`, `add_ca`, `delete_ca`,
  `ca_raw_dump`, `search_cert`, `add_cert`, `delete_cert`, `cert_raw_dump`,
  `search_crl`, `get_trust_config`, `reconfigure_trust`
- **wireguard:** `get_wireguard_general`, `set_wireguard_general`,
  servers `search_wireguard_servers`, `add_wireguard_server`,
  `set_wireguard_server`, `delete_wireguard_server`,
  `toggle_wireguard_server`, `wireguard_key_pair`; clients
  `search_wireguard_clients`, `add_wireguard_client`,
  `set_wireguard_client`, `delete_wireguard_client`,
  `toggle_wireguard_client`, `wireguard_client_list_servers`,
  `wireguard_client_psk`; `wireguard_status`, `reconfigure_wireguard`
- **ipsec:** `get_ipsec_config`, `set_ipsec_config`, connections
  `search_ipsec_connections`, `add_ipsec_connection`,
  `set_ipsec_connection`, `delete_ipsec_connection`,
  `toggle_ipsec_connection`, `ipsec_is_enabled`; pools `search_ipsec_pools`,
  `add_ipsec_pool`, `set_ipsec_pool`, `delete_ipsec_pool`; key pairs
  `search_ipsec_key_pairs`, `add_ipsec_key_pair`, `set_ipsec_key_pair`,
  `delete_ipsec_key_pair`, `gen_ipsec_key_pair`; PSKs `search_ipsec_psk`,
  `add_ipsec_psk`, `set_ipsec_psk`, `delete_ipsec_psk`; `search_ipsec_leases`,
  `search_ipsec_phase1`, `search_ipsec_phase2`, `ipsec_swanctl`,
  `ipsec_status`, `reconfigure_ipsec`
- **openvpn:** `search_openvpn_instances`, `get_openvpn_instance`,
  `add_openvpn_instance`, `set_openvpn_instance`,
  `delete_openvpn_instance`, `toggle_openvpn_instance`; static keys
  `search_openvpn_static_keys`, `add_openvpn_static_key`,
  `delete_openvpn_static_key`; `search_openvpn_sessions`,
  `openvpn_reconfigure`
- **ids/ips:** `get_ids_config`, `set_ids_config`, `ids_list_rulesets`,
  `ids_get_ruleset_properties`, `ids_search_installed_rules`,
  `ids_search_user_rules`, `ids_add_user_rule`, `ids_set_user_rule`,
  `ids_delete_user_rule`, `ids_search_policies`,
  `ids_search_policy_rules`, `ids_get_alert_logs`, `ids_update_rules`,
  `ids_status`, `reconfigure_ids`
- **hasync (HA):** `hasync_get`, `hasync_set`, `hasync_reconfigure`,
  `hasync_status_services`, `hasync_status_version`,
  `hasync_status_restart`, `get_pfsync_nodes`

## Gotchas (learned the hard way — read these)

- **Firewall/NAT endpoints are camelCase** in the URL: `searchRule`, `addRule`,
  `setRule/{uuid}`, `delRule/{uuid}`, `toggleRule/{uuid}/{0|1}`, `getInterfaceList`.
  Other modules (core, interfaces, dnsmasq) use snake_case (`system_status`,
  `vlan_settings`, `leases/search`).
- **`searchRule` only honors `searchPhrase` via POST body.** The GET form ignores
  it (returns everything). `opnsense_list_rules(search=...)` already POSTs it for
  you. If you use `opnsense_api` to search, send a POST with the body.
- **DHCP is served by dnsmasq, not the `dhcpv4` plugin.** The `dhcpv4/*`
  endpoints 404 on a stock box. Use `dnsmasq/leases/search`,
  `dnsmasq/service/status`, `dnsmasq/settings/get` (the `opnsense_*dhcp*` tools
  wrap these).
- **`addRule` validation:** source/destination net type must match the rule's
  `ipprotocol` (default `inet4`). Mixing IPv4 nets with `inet46` fails with
  `data.validations`. Omit `ipprotocol` for IPv4 rules.
- **No-body POSTs send `{}`** so the request has a `Content-Length`; the
  OPNsense web frontend otherwise answers `411 Length Required`.
- **Model-based `set` endpoints need a doubly-nested body.** Controllers
  extending `ApiMutableModelControllerBase` (e.g. `diagnostics/ping/set`,
  `diagnostics/portprobe/set`, `diagnostics/traceroute/set`) read
  `getPost('<model_name>')` and validate the model, so the body must be
  `{"<model>": {"<item>": {fields...}}}` — for ping:
  `{"ping": {"settings": {"hostname": "1.1.1.1", "fam": "ip",
  "interval": 1}}}`. Flat keys (`{"hostname": ...}`), `settings.*` dotted
  keys, or a single nesting level all fail with
  `validations: {"<model>.settings.<field>": "A value is required."}`.
  Ping jobs are *continuous* (interval-based): `set` → returns `uuid`,
  `start/{uuid}` → `get`/`search_jobs` → `stop/{uuid}` + `remove/{uuid}`.
  There is no `count` field.
- **`add_item` (VLAN/LAGG) needs a single-level model wrapper.** `addBase`
  reads `getPost('<model>')`, so the body is `{"vlan": {…}}` / `{"lagg": {…}}`
  (unlike the doubly-nested model `set` endpoints). The `opnsense_add_vlan` /
  `opnsense_add_lagg` tools already wrap it.
- **LAGG `members` is a comma-separated string, not a JSON array.** The
  `BaseListField` validator runs `explode(",", $data)` and `setNodes` rejects
  arrays with "expected a single value" → HTTP 500.
- **`IntegerField` range failures use the generic message.** `vlan.tag` is
  1–4094; out-of-range fails with `validations: {"vlan.tag":
  "Invalid integer value."}` (MinMaxValidator shares the field's message) —
  it is NOT a type problem.
- **VLAN/LAGG list responses are dicts keyed by uuid**, not rows:
  `data.vlan.vlan` / `data.lagg.lagg` = `{"<uuid>": {…fields…}}`.
- **A new VLAN/LAGG is staged** until you reconfigure: POST
  `interfaces/vlan_settings/reconfigure` (or `lagg_settings/reconfigure`) via
  `opnsense_api`, then assign an IP through the interface config.
- **`system.timeservers` has no REST endpoint** (26.7 verified against source:
  only the legacy `services_ntpd.php` page writes it; cron jobs can only run
  fixed configd actions). Use `opnsense_set_timeservers`, which performs a
  scripted web login (session CSRF: hidden token or `X-CSRFToken` header) and
  submits the NTP form — pool hosts `*.pool.ntp.org` are auto-flagged as
  pools, matching the GUI.
- **Web password reset:** `opnsense_set_user_password` wraps
  `auth/user/set/{uuid}` (send only `{"user":{"password":"<plaintext>"}}` —
  the model hashes it in `setBaseHook`). Never send `scrambled_password`
  (server generates a random pwd and won't return it → lockout). Passwords are
  read from files in `secrets/`, never tool arguments.
- **`opnsense_apply_firewall`** pushes staged firewall rules live. Don't call it
  casually — only when you intend to change live traffic.
- **Search-endpoint naming is per-controller, not global.** Some list
  endpoints are `search_item` (aliases, groups, monit services/alerts/tests,
  syslog destinations, interface assignment, unbound host overrides/aliases,
  ACLs, blocklists, ipsec pools/PSKs/connections, openvpn instances,
  shaper pipes/queues/rules); others plain `search` (privileges
  `auth/priv/search`, users `auth/user/search`, static gateways
  `routing/settings/search_gateway`, static routes
  `routes/routes/searchroute` — lowercase compound); others `search_jobs`
  (ping, cron). When an endpoint 404s, check the controller's action list in
  the core repo (`src/opnsense/mvc/app/controllers/OPNsense/<Module>/Api/`).
- **Model `set`/`add` bodies need the model-name wrapper key.** Every
  `addBase`/`setItem` controller reads `getPost('<model>')`: alias →
  `{"alias": {...}}`, group → `{"group": {...}}`, gateway →
  `{"gateway": {...}}` (model item is `gateway_item` but the wrapper is
  `gateway`), route → `{"route": {...}}`, host override → `{"host": {...}}`,
  syslog destination → `{"destination": {...}}`, monit service →
  `{"service": {...}}`, wireguard server/client → `{"server"/"client": {...}}`,
  ipsec → `{"connection"/"pool"/"key_pair"/"pre_shared_key": {...}}`,
  interface assignment → `{"interface": {...}}`, shaper →
  `{"pipe"/"queue"/"rule": {...}}`. The first-class tools already wrap it.
- **Model field names differ from UI labels — check the model XML before
  writing.** Examples learned live: unbound host override is `domain` +
  `server` (not name/address); syslog destination is `hostname` + `transport`
  (udp4/tcp4/udp6/tcp6/tls4/tls6) + `port`; monit service needs `type`
  (process/file/fifo/filesystem/directory/host/system/custom/network);
  firewall alias `name` is alnum+underscore only (no hyphens, <32 chars);
  static route `gateway` is a **gateway name** (e.g. `LAN`, `WAN_DHCP`), not
  an interface. Validation errors come back as `ok:true` +
  `data.result=="failed"` + `data.validations` keyed by `<wrapper>.<field>`.
- **`firewall/alias_util/*` is path-based, not item-based:**
  `aliases` (table names), `list/<name>`, `add/<name>`, `delete/<name>`,
  `flush/<name>`, `find_references` (POST `{"ip": ...}`). `add`/`delete` take
  the address in the body: `{"address": "10.0.0.1/32"}`. `list` returns live
  pf table entries with rows carrying the `ip` field.
- **Interface assignment (26.7):** `interfaces/assignment` is a full CRUD
  controller (`search_item`, `get_item/<ifname>`, `add_item`,
  `set_item/<ifname>`, `del_item/<ifname>`, `reconfigure`). The item is keyed
  by interface name (`identifier`); `if` is the physical device. Set
  `"lock": "0"` on new items or deletion will refuse ("Interface locked").
  Changes go live on `reconfigure` — test on a spare box.
- **`opnsense_update_blocklist`** takes `uuid` + `domain` + `type`
  (`blocklists`/`allowlists`) — the domain alone is not enough.
- **Monit status** prints "monit.sock does not exist" on stderr when the monit
  service is disabled — that is the *expected* response shape, not an error.

## Anything not covered

Use the escape hatch:
```
opnsense_api(method="GET"|"POST", path="core/system/status",
             params={...}, body={...})
```
`path` is everything after `/api/`. Find module/controller/command names in the
official reference: https://docs.opnsense.org/development/api.html (and the
per-module pages). Prefer that over guessing.

## Safety

You can take a firewall down or drop traffic. Read before you write; stage
changes; apply deliberately; keep the box's API key/secret private (they live in
`secrets/`, loaded by the launch wrappers — never echo them into chat or files).
