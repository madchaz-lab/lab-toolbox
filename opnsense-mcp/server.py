#!/usr/bin/env python3
"""MCP server exposing the OPNsense REST API.

A complete, generic MCP interface to an OPNsense firewall. It targets ONE
firewall per instance, configured via environment variables (see README):

  OPNSENSE_HOST        host or IP of the firewall (required)
  OPNSENSE_KEY         API key -> HTTP Basic username (required)
  OPNSENSE_SECRET      API secret -> HTTP Basic password (required)
  OPNSENSE_SCHEME      http (default) or https
  OPNSENSE_PORT        port (default 80 for http, 443 for https)
  OPNSENSE_VERIFY_SSL  true/false (default false)

Design: a small set of first-class "high-level" tools for the common
firewall-management tasks (system, interfaces/VLANs, firewall rules, NAT,
DHCP/DNS, services, backup, diagnostics) plus a generic ``opnsense_api``
escape hatch that can call *any* ``/api/<module>/<controller>/<command>``
endpoint. That combination makes the server effectively complete for the
whole OPNsense API surface (see docs.opnsense.org/development/api.html) even
when a specific high-level tool does not exist.

Transport: stdio. Built on FastMCP (mcp SDK) to match the other custom lab
MCPs (aruba, virsh).
"""
import json
import os
import re
from typing import Optional

import anyio
import requests
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("opnsense")

TIMEOUT = float(os.environ.get("OPNSENSE_TIMEOUT", "60"))


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
def _scheme() -> str:
    return os.environ.get("OPNSENSE_SCHEME", "http").lower().strip()


def _base() -> str:
    scheme = _scheme()
    host = os.environ.get("OPNSENSE_HOST", "").strip()
    port = os.environ.get("OPNSENSE_PORT", "").strip()
    if not port:
        port = "443" if scheme == "https" else "80"
    if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _auth():
    return (
        os.environ.get("OPNSENSE_KEY", ""),
        os.environ.get("OPNSENSE_SECRET", ""),
    )


def _verify() -> bool:
    return os.environ.get("OPNSENSE_VERIFY_SSL", "false").strip().lower() in (
        "1", "true", "yes")


def _require_config() -> Optional[str]:
    missing = [
        name for name, val in (
            ("OPNSENSE_HOST", os.environ.get("OPNSENSE_HOST")),
            ("OPNSENSE_KEY", os.environ.get("OPNSENSE_KEY")),
            ("OPNSENSE_SECRET", os.environ.get("OPNSENSE_SECRET")),
        ) if not (val or "").strip()
    ]
    if missing:
        return "Missing required environment: " + ", ".join(missing)
    return None


def _request(method: str, path: str, params=None, body=None) -> dict:
    """Perform an OPNsense API call and normalize the response.

    ``path`` is the part after /api/, e.g. "core/system/status" or
    "firewall/filter/searchRule".
    """
    err = _require_config()
    if err:
        return {"ok": False, "error": err}
    url = _base() + "/api/" + path.lstrip("/")
    send_body = body
    if method.upper() in ("POST", "PUT") and send_body is None:
        send_body = {}
    try:
        r = requests.request(
            method.upper(), url, auth=_auth(), params=params,
            json=send_body,
            verify=_verify(), timeout=TIMEOUT)
    except requests.exceptions.RequestException as e:
        return {"ok": False, "error": f"request failed: {e}"}
    try:
        data = r.json()
    except ValueError:
        data = r.text
    ok = r.status_code < 400
    out = {"ok": ok, "status": r.status_code, "data": data}
    if not ok and isinstance(data, dict) and data.get("errorMessage"):
        out["error"] = data["errorMessage"]
    return out


def _get(path: str, params=None) -> dict:
    return _request("GET", path, params=params)


def _post(path: str, body=None, params=None) -> dict:
    return _request("POST", path, params=params, body=body)


def _text(result) -> str:
    return json.dumps(result, indent=2, default=str)


async def _run(fn, *args, **kwargs) -> str:
    return await anyio.to_thread.run_sync(lambda: _text(fn(*args, **kwargs)))


# --------------------------------------------------------------------------
# foundation / generic
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_ping() -> str:
    """Health check: returns system status + the configured host. Use to confirm the
    MCP is connected to a reachable, authenticated OPNsense firewall.
    """
    def go():
        cfg_err = _require_config()
        if cfg_err:
            return {"ok": False, "error": cfg_err}
        res = _get("core/system/status")
        res["host"] = os.environ.get("OPNSENSE_HOST")
        return res
    return await _run(go)


@mcp.tool()
async def opnsense_api(method: str, path: str, params: Optional[dict] = None,
                       body: Optional[dict] = None) -> str:
    """Generic escape hatch: call ANY OPNsense REST endpoint.

    Args:
        method: HTTP verb, "GET" or "POST".
        path: endpoint after /api/, e.g. "core/system/status",
            "firewall/filter/searchRule", "dhcpv4/leases/searchLease".
            Optional path params can be inlined (e.g.
            "firewall/filter/delRule/<uuid>").
        params: optional JSON object of URL query parameters (e.g.
            {"current":1,"rowCount":50,"searchPhrase":"lan"}).
        body: optional JSON object for the POST request body.

    Returns {ok, status, data} (plus ``error`` on failure). Use the
    docs.opnsense.org API reference to find module/controller/command names.
    """
    def go():
        return _request(method, path, params=params, body=body)
    return await _run(go)


# --------------------------------------------------------------------------
# system
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_get_system_information() -> str:
    """System identity + version: hostname, FQDN and OS/kernel/OpenSSL versions
    (diagnostics/system/system_information).
    """
    return await _run(_get, "diagnostics/system/system_information")


@mcp.tool()
async def opnsense_get_system_status() -> str:
    """System status banner (pending config messages, subsystem status) via
    core/system/status.
    """
    return await _run(_get, "core/system/status")


@mcp.tool()
async def opnsense_get_dashboard() -> str:
    """Dashboard modules + widgets (core/dashboard/get_dashboard)."""
    return await _run(_get, "core/dashboard/get_dashboard")


@mcp.tool()
async def opnsense_reboot() -> str:
    """Reboot the firewall (core/system/reboot). MODIFIES the host; use with care.
    """
    return await _run(_post, "core/system/reboot")


@mcp.tool()
async def opnsense_halt() -> str:
    """Halt (power off) the firewall (core/system/halt). MODIFIES the host.
    """
    return await _run(_post, "core/system/halt")


# --------------------------------------------------------------------------
# interfaces + VLANs + LAGG
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_list_interfaces() -> str:
    """List all interfaces with live info (name, descr, IP, MAC, status) via
    interfaces/overview/interfaces_info.
    """
    return await _run(_get, "interfaces/overview/interfaces_info",
                      params={"details": True})


@mcp.tool()
async def opnsense_get_interface_config(iface: str) -> str:
    """Get configuration for one interface (interfaces/overview/get_interface).

    Args:
        iface: interface key, e.g. "wan", "lan", "vtnet0".
    """
    return await _run(_get, f"interfaces/overview/get_interface/{iface}")


@mcp.tool()
async def opnsense_get_interface_names() -> str:
    """Map of interface keys to their descriptions (diagnostics/interface/
    get_interface_names).
    """
    return await _run(_get, "diagnostics/interface/get_interface_names")


@mcp.tool()
async def opnsense_get_interface_statistics() -> str:
    """Per-interface traffic/error statistics (diagnostics/interface/
    get_interface_statistics).
    """
    return await _run(_get, "diagnostics/interface/get_interface_statistics")


@mcp.tool()
async def opnsense_reload_interface(iface: str) -> str:
    """Reload/apply one interface (interfaces/overview/reload_interface).

    Args:
        iface: interface key to reload.
    """
    return await _run(_post, f"interfaces/overview/reload_interface/{iface}")


@mcp.tool()
async def opnsense_list_vlans() -> str:
    """List configured VLANs (interfaces/vlan_settings/get)."""
    return await _run(_get, "interfaces/vlan_settings/get")


@mcp.tool()
async def opnsense_add_vlan(iface: str, vlan: int, descr: str,
                            pcid: Optional[int] = None) -> str:
    """Add a VLAN interface (interfaces/vlan_settings/add_item). The new VLAN must
    then be applied via ``opnsense_api`` POST interfaces/vlan_settings/reconfigure
    and given an IP via the interface config.

    Args:
        iface: parent interface key (e.g. "vtnet1").
        vlan: 802.1Q VLAN ID.
        descr: description shown in the UI.
        pcid: optional parent interface (for nested VLANs).
    """
    item = {"if": iface, "tag": int(vlan), "descr": descr}
    if pcid is not None:
        item["pcid"] = int(pcid)
    return await _run(_post, "interfaces/vlan_settings/add_item",
                      body={"vlan": item})


@mcp.tool()
async def opnsense_delete_vlan(uuid: str) -> str:
    """Delete a VLAN interface by uuid (interfaces/vlan_settings/del_item).

    Args:
        uuid: VLAN uuid (from opnsense_list_vlans).
    """
    return await _run(_post, f"interfaces/vlan_settings/del_item/{uuid}")


@mcp.tool()
async def opnsense_list_lagg() -> str:
    """List configured LAGG (link aggregation) interfaces (interfaces/lagg_settings/get).
    """
    return await _run(_get, "interfaces/lagg_settings/get")


@mcp.tool()
async def opnsense_add_lagg(ports: str, descr: str,
                            mode: str = "failover",
                            primary_member: Optional[str] = None) -> str:
    """Add a LAGG (link aggregation) interface (interfaces/lagg_settings/add_item).
    The new LAGG must then be applied via ``opnsense_api`` POST
    interfaces/lagg_settings/reconfigure.

    Args:
        ports: comma-separated member interface names (e.g. "vtnet2,vtnet3").
        descr: description shown in the UI.
        mode: proto — failover (default), lacp, loadbalance, roundrobin, fec, none.
        primary_member: primary (active) member, used with failover.
    """
    members = ",".join(p.strip() for p in ports.split(",") if p.strip())
    item = {"members": members, "proto": mode, "descr": descr}
    if primary_member:
        item["primary_member"] = primary_member
    return await _run(_post, "interfaces/lagg_settings/add_item",
                      body={"lagg": item})


@mcp.tool()
async def opnsense_delete_lagg(uuid: str) -> str:
    """Delete a LAGG interface by uuid (interfaces/lagg_settings/del_item).
    May fail if the LAGG is still in use by an interface or VLAN.

    Args:
        uuid: LAGG uuid (from opnsense_list_lagg).
    """
    return await _run(_post, f"interfaces/lagg_settings/del_item/{uuid}")


# --------------------------------------------------------------------------
# firewall rules
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_list_rules(search: Optional[str] = None,
                              current: int = 1, row_count: int = 100) -> str:
    """List firewall rules (firewall/filter/searchRule). The search phrase is
    POSTed in the body: the GET form ignores searchPhrase on 26.7.

    Args:
        search: optional search phrase (e.g. a rule description).
        current: page number (default 1).
        row_count: rows per page (default 100).
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "firewall/filter/searchRule", body=body)


@mcp.tool()
async def opnsense_get_rule(uuid: str) -> str:
    """Get one firewall rule by uuid (firewall/filter/getRule).

    Args:
        uuid: rule uuid (from opnsense_list_rules).
    """
    return await _run(_get, f"firewall/filter/getRule/{uuid}")


@mcp.tool()
async def opnsense_add_rule(rule: dict) -> str:
    """Add a firewall rule (firewall/filter/addRule). The rule is staged in config;
    call ``opnsense_apply_firewall`` to activate it.

    Args:
        rule: rule object, e.g. {"description":"allow lan->10.0.0.0/24",
            "interface":"lan","source_net":"192.168.0.0/24",
            "destination_net":"10.0.0.0/24","protocol":"TCP","target":"pass"}.
    """
    return await _run(_post, "firewall/filter/addRule", body={"rule": rule})


@mcp.tool()
async def opnsense_set_rule(uuid: str, rule: dict) -> str:
    """Update a firewall rule (firewall/filter/setRule).

    Args:
        uuid: rule uuid to update.
        rule: new rule object (same shape as opnsense_add_rule).
    """
    return await _run(_post, f"firewall/filter/setRule/{uuid}",
                      body={"rule": rule})


@mcp.tool()
async def opnsense_delete_rule(uuid: str) -> str:
    """Delete a firewall rule (firewall/filter/delRule).

    Args:
        uuid: rule uuid to delete.
    """
    return await _run(_post, f"firewall/filter/delRule/{uuid}")


@mcp.tool()
async def opnsense_toggle_rule(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a firewall rule (firewall/filter/toggleRule).

    Args:
        uuid: rule uuid.
        enabled: True to enable, False to disable.
    """
    return await _run(_post,
                      f"firewall/filter/toggleRule/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_apply_firewall() -> str:
    """Apply staged firewall/NAT changes so they become active (firewall/filter/apply).
    """
    return await _run(_post, "firewall/filter/apply")


# --------------------------------------------------------------------------
# NAT (port forwards, 1:1, outbound)
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_list_port_forwards(current: int = 1,
                                      row_count: int = 100) -> str:
    """List destination NAT / port-forward rules (firewall/d_nat/searchRule).

    Args:
        current: page number (default 1).
        row_count: rows per page (default 100).
    """
    return await _run(_get, "firewall/d_nat/searchRule",
                      params={"current": current, "rowCount": row_count})


@mcp.tool()
async def opnsense_add_port_forward(rule: dict) -> str:
    """Add a port-forward rule (firewall/d_nat/addRule).

    Args:
        rule: e.g. {"interface":"wan","target":"192.168.1.50","forward_port":"443",
            "local_port":"443","protocol":"TCP","description":"web"}.
    """
    return await _run(_post, "firewall/d_nat/addRule", body={"rule": rule})


@mcp.tool()
async def opnsense_delete_port_forward(uuid: str) -> str:
    """Delete a port-forward rule (firewall/d_nat/delRule).

    Args:
        uuid: rule uuid (from opnsense_list_port_forwards).
    """
    return await _run(_post, f"firewall/d_nat/delRule/{uuid}")


@mcp.tool()
async def opnsense_list_one2one(current: int = 1, row_count: int = 100) -> str:
    """List 1:1 NAT rules (firewall/one_to_one/searchRule).

    Args:
        current: page number.
        row_count: rows per page.
    """
    return await _run(_get, "firewall/one_to_one/searchRule",
                      params={"current": current, "rowCount": row_count})


@mcp.tool()
async def opnsense_list_outbound_nat(current: int = 1,
                                     row_count: int = 100) -> str:
    """List outbound (source) NAT rules (firewall/source_nat/searchRule).

    Args:
        current: page number.
        row_count: rows per page.
    """
    return await _run(_get, "firewall/source_nat/searchRule",
                      params={"current": current, "rowCount": row_count})


# --------------------------------------------------------------------------
# DHCP + DNS
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_list_dhcp_leases(current: int = 1,
                                    row_count: int = 100) -> str:
    """List active DHCPv4 leases (dnsmasq/leases/search). dnsmasq is the default
    DHCPv4 engine in OPNsense.

    Args:
        current: page number.
        row_count: rows per page.
    """
    return await _run(_get, "dnsmasq/leases/search",
                      params={"current": current, "rowCount": row_count})


@mcp.tool()
async def opnsense_get_dhcp_status() -> str:
    """DHCPv4 (dnsmasq) service status (dnsmasq/service/status)."""
    return await _run(_get, "dnsmasq/service/status")


@mcp.tool()
async def opnsense_get_dhcp_config() -> str:
    """DHCPv4/DNS (dnsmasq) configuration: ranges, hosts, options, domains
    (dnsmasq/settings/get).
    """
    return await _run(_get, "dnsmasq/settings/get")


@mcp.tool()
async def opnsense_get_unbound_settings() -> str:
    """DNS (Unbound) settings: forwarders, hosts, ACLs, blocklist (unbound/settings/get).
    """
    return await _run(_get, "unbound/settings/get")


@mcp.tool()
async def opnsense_get_dns_nameservers() -> str:
    """Configured upstream DNS forwarders (unbound/settings/get_nameservers).
    """
    return await _run(_get, "unbound/settings/get_nameservers")


@mcp.tool()
async def opnsense_get_unbound_status() -> str:
    """Unbound (DNS) service status (unbound/service/status)."""
    return await _run(_get, "unbound/service/status")


# --------------------------------------------------------------------------
# services
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_list_services() -> str:
    """List system services and their running state (core/service/search).
    """
    return await _run(_post, "core/service/search")


@mcp.tool()
async def opnsense_start_service(name: str) -> str:
    """Start a service (core/service/start).

    Args:
        name: service id, e.g. "dnsmasq", "unbound", "configd".
    """
    return await _run(_post, f"core/service/start/{name}")


@mcp.tool()
async def opnsense_stop_service(name: str) -> str:
    """Stop a service (core/service/stop).

    Args:
        name: service id.
    """
    return await _run(_post, f"core/service/stop/{name}")


@mcp.tool()
async def opnsense_restart_service(name: str) -> str:
    """Restart a service (core/service/restart).

    Args:
        name: service id.
    """
    return await _run(_post, f"core/service/restart/{name}")


# --------------------------------------------------------------------------
# backup (OPNsense config backups)
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_list_backups() -> str:
    """List OPNsense config backups on this firewall (core/backup/backups).
    """
    return await _run(_get, "core/backup/backups/this")


@mcp.tool()
async def opnsense_backup_providers() -> str:
    """List configured backup providers (core/backup/providers).
    """
    return await _run(_get, "core/backup/providers")


@mcp.tool()
async def opnsense_download_backup(host: str = "this",
                                   backup: Optional[str] = None) -> str:
    """Download an OPNsense config backup (core/backup/download). Returns the raw
    backup text (XML). Large; use sparingly.

    Args:
        host: backup provider host key (default "this").
        backup: specific backup name (default latest).
    """
    path = "core/backup/download/" + host
    if backup:
        path += "/" + backup
    return await _run(_get, path)


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_get_routes() -> str:
    """Routing table (diagnostics/interface/get_routes)."""
    return await _run(_get, "diagnostics/interface/get_routes")


@mcp.tool()
async def opnsense_get_arp_table() -> str:
    """ARP/NDP table (diagnostics/interface/get_arp)."""
    return await _run(_get, "diagnostics/interface/get_arp")


@mcp.tool()
async def opnsense_get_pf_states() -> str:
    """Active pf states (diagnostics/firewall/pf_states).
    """
    return await _run(_get, "diagnostics/firewall/pf_states")


@mcp.tool()
async def opnsense_get_pf_statistics(section: Optional[str] = None) -> str:
    """pf packet/byte counters (diagnostics/firewall/pf_statistics).

    Args:
        section: optional section filter.
    """
    path = "diagnostics/firewall/pf_statistics"
    if section:
        path += "/" + section
    return await _run(_get, path)


@mcp.tool()
async def opnsense_get_memory() -> str:
    """Memory statistics (diagnostics/system/memory)."""
    return await _run(_get, "diagnostics/system/memory")


@mcp.tool()
async def opnsense_get_system_time() -> str:
    """Current firewall time (diagnostics/system/system_time)."""
    return await _run(_get, "diagnostics/system/system_time")


# --------------------------------------------------------------------------
# access: user manager + legacy web session
#
# Some config nodes (e.g. system.timeservers) have NO REST endpoint — only the
# legacy web UI pages write them. The tools below cover that: user password
# management via the auth/user API, and legacy-form submission through a
# scripted web session (cookie + CSRF token, see _web_session).
# --------------------------------------------------------------------------
def _read_secret_file(path: str) -> str:
    """Read a secret (e.g. a password) from a file on the host running this
    server. The secret never appears in tool arguments or logs."""
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e}") from e


def _csrf_from_html(html: str):
    """Extract the (token, key) pair from a legacy CSRF hidden input."""
    m = re.search(
        r'<input type="hidden" name="([^"]+)" value="([^"]+)"[^>]*'
        r'autocomplete="new-password"', html)
    if not m:
        m = re.search(r'<input type="hidden" name="([^"]+)" value="([^"]+)"',
                      html)
    return (m.group(2), m.group(1)) if m else (None, None)


def _web_session(username: str, password: str) -> requests.Session:
    """Log into the OPNsense web UI and return an authenticated session.

    Every non-GET request to the web frontend must carry the session CSRF
    token (hidden form field or X-CSRFToken header); login is a plain form
    POST (usernamefld/passwordfld) to /index.php.
    """
    sess = requests.Session()
    sess.verify = _verify()
    base = _base()
    r = sess.get(base + "/index.php", timeout=TIMEOUT)
    token, _key = _csrf_from_html(r.text)
    headers = {"X-CSRFToken": token} if token else {}
    r = sess.post(
        base + "/index.php",
        data={"usernamefld": username, "passwordfld": password,
              "login": "Login"},
        headers=headers, timeout=TIMEOUT, allow_redirects=False)
    if r.status_code not in (302, 303):
        raise RuntimeError(
            f"web login failed (status={r.status_code}); check web_user "
            f"and password_file")
    return sess


@mcp.tool()
async def opnsense_search_users() -> str:
    """List local web/API users (auth/user/search): uuid, name, scope, priv,
    disabled, is_admin. Use to find a user's uuid before updating them.
    """
    return await _run(_get, "auth/user/search")


@mcp.tool()
async def opnsense_set_user_password(username: str,
                                     password_file: str) -> str:
    """Reset the web password of a local user (auth/user/set).

    Sends only the password field (the model hashes and stores it server
    side). If the partial update is rejected, it falls back to fetching the
    full user record, replacing the password, and re-applying it.

    Args:
        username: exact username, e.g. "opencode".
        password_file: path to a file containing the new password (read from
            disk; the password is never a tool argument).
    """
    def go():
        try:
            password = _read_secret_file(password_file)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if not password:
            return {"ok": False, "error": "password file is empty"}
        search = _get("auth/user/search")
        rows = (search.get("data") or {}).get("rows") or []
        match = [u for u in rows if u.get("name") == username]
        if not match:
            return {"ok": False,
                    "error": f"user {username!r} not found "
                             f"(have: {[u.get('name') for u in rows]})"}
        uuid = match[0]["uuid"]
        res = _post("auth/user/set/" + uuid, body={"user": {"password": password}})
        if not res.get("ok") or (res.get("data") or {}).get("result") not in (
                "saved", "ok"):
            # fallback: full-record apply with password swapped in
            rec = _get("auth/user/get/" + uuid)
            user = (rec.get("data") or {}).get("user") or {}
            if not user:
                return res
            user.pop("scrambled_password", None)
            user["password"] = password
            res = _post("auth/user/set/" + uuid, body={"user": user})
        data = res.get("data")
        result = data.get("result") if isinstance(data, dict) else None
        out = {"ok": res.get("ok", False) and result in ("saved", "ok"),
               "status": res.get("status"), "user": username,
               "uuid": uuid, "result": result}
        if not out["ok"]:
            out["error"] = (data or {}).get("validations") or \
                           (data or {}).get("errorMessage") or "set failed"
        return out
    return await _run(go)


@mcp.tool()
async def opnsense_set_timeservers(timeservers: str, web_user: str = "opencode",
                                   password_file: str = "") -> str:
    """Set the system NTP servers (system.timeservers) via the web UI.

    The REST API has no endpoint for this node — only the legacy
    /services_ntpd.php page writes it — so this performs a scripted web login
    as ``web_user`` and submits the NTP form. Saving restarts ntpd
    automatically. Pool hosts (*.<n>.pool.ntp.org) are marked as pools,
    matching the GUI behaviour.

    Args:
        timeservers: space-separated NTP server names, e.g.
            "0.ca.pool.ntp.org 1.ca.pool.ntp.org 2.ca.pool.ntp.org 3.ca.pool.ntp.org".
        web_user: web UI username to log in as (default "opencode").
        password_file: path to a file containing that user's web password.

    Returns the REST verification: ntpd/service/status + core/system/status.
    """
    def go():
        if not (password_file or "").strip():
            return {"ok": False, "error": "password_file is required"}
        try:
            password = _read_secret_file(password_file)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        try:
            sess = _web_session(web_user, password)
        except (RuntimeError, requests.exceptions.RequestException) as e:
            return {"ok": False, "error": str(e)}
        base = _base()
        r = sess.get(base + "/services_ntpd.php", timeout=TIMEOUT)
        if "usernamefld" in r.text:
            return {"ok": False,
                    "error": "web session not authenticated (login form "
                             "rendered)"}
        token, _key = _csrf_from_html(r.text)
        headers = {"X-CSRFToken": token} if token else {}
        hosts = [h for h in timeservers.split() if h]
        if not hosts:
            return {"ok": False, "error": "timeservers is empty"}
        data = {
            "timeservers_host[]": hosts,
            "timeservers_ispool[]": [h for h in hosts
                                     if h.endswith(".pool.ntp.org")],
            "Submit": "save",
        }
        r = sess.post(base + "/services_ntpd.php", data=data,
                      headers=headers, timeout=TIMEOUT, allow_redirects=False)
        if r.status_code not in (302, 303):
            return {"ok": False, "status": r.status_code,
                    "error": "NTP form not saved (no redirect)"}
        ntpd_status = _get("ntpd/service/status")
        sys_status = _get("core/system/status")
        meta = (sys_status.get("data") or {}).get("metadata", {})
        return {"ok": True, "status": r.status_code,
                "data": {"saved": True, "timeservers": hosts,
                         "ntpd_status": ntpd_status.get("data"),
                         "system": meta.get("system")}}
    return await _run(go)


@mcp.tool()
async def opnsense_get_ntp_status() -> str:
    """ntpd service status (ntpd/service/status)."""
    return await _run(_get, "ntpd/service/status")


if __name__ == "__main__":
    mcp.run()
