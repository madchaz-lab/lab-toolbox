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
# firmware + packages
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_firmware_info() -> str:
    """Firmware + installed package list (core/firmware/info)."""
    return await _run(_get, "core/firmware/info")


@mcp.tool()
async def opnsense_firmware_status() -> str:
    """Firmware update status/availability (core/firmware/status)."""
    return await _run(_post, "core/firmware/status")


@mcp.tool()
async def opnsense_firmware_running() -> str:
    """Whether the system is ready to run an upgrade (core/firmware/running)."""
    return await _run(_get, "core/firmware/running")


@mcp.tool()
async def opnsense_firmware_upgradestatus() -> str:
    """In-progress upgrade status (core/firmware/upgradestatus)."""
    return await _run(_get, "core/firmware/upgradestatus")


@mcp.tool()
async def opnsense_firmware_check() -> str:
    """Check for firmware updates (core/firmware/check)."""
    return await _run(_post, "core/firmware/check")


@mcp.tool()
async def opnsense_firmware_changelog(version: str) -> str:
    """Release changelog for a version (core/firmware/changelog/<version>).

    Args:
        version: e.g. "26.7".
    """
    return await _run(_post, "core/firmware/changelog/" + version)


@mcp.tool()
async def opnsense_firmware_get() -> str:
    """Current firmware settings (core/firmware/get)."""
    return await _run(_get, "core/firmware/get")


@mcp.tool()
async def opnsense_firmware_set(settings: dict) -> str:
    """Update firmware settings (core/firmware/set), e.g. auto-update options.

    Args:
        settings: settings dict as returned by opnsense_firmware_get.
    """
    return await _run(_post, "core/firmware/set", body={"firmware": settings})


@mcp.tool()
async def opnsense_firmware_health() -> str:
    """Pre-upgrade health checks (core/firmware/health)."""
    return await _run(_post, "core/firmware/health")


@mcp.tool()
async def opnsense_firmware_log(clear: bool = False) -> str:
    """Upgrade log (core/firmware/log; pass clear=True to empty it)."""
    return await _run(_post, "core/firmware/log/" + ("1" if clear else "0"))


@mcp.tool()
async def opnsense_firmware_upgrade() -> str:
    """Run a firmware upgrade (core/firmware/upgrade). MODIFIES the host
    (reboots); use with care.
    """
    return await _run(_post, "core/firmware/upgrade")


@mcp.tool()
async def opnsense_firmware_update() -> str:
    """Apply pending package updates (core/firmware/update). MODIFIES the host.
    """
    return await _run(_post, "core/firmware/update")


@mcp.tool()
async def opnsense_firmware_sync_plugins(resync: bool = False) -> str:
    """(Re)sync plugin metadata (core/firmware/syncPlugins or resyncPlugins)."""
    path = "core/firmware/" + ("resyncPlugins" if resync else "syncPlugins")
    return await _run(_post, path)


@mcp.tool()
async def opnsense_firmware_audit() -> str:
    """Run a config audit (core/firmware/audit)."""
    return await _run(_post, "core/firmware/audit")


@mcp.tool()
async def opnsense_pkg_install(pkg: str) -> str:
    """Install a package (core/firmware/install/<pkg>). MODIFIES the host.

    Args:
        pkg: package name, e.g. "os-idse".
    """
    return await _run(_post, "core/firmware/install/" + pkg)


@mcp.tool()
async def opnsense_pkg_remove(pkg: str) -> str:
    """Remove a package (core/firmware/remove/<pkg>). MODIFIES the host.

    Args:
        pkg: package name.
    """
    return await _run(_post, "core/firmware/remove/" + pkg)


@mcp.tool()
async def opnsense_pkg_reinstall(pkg: str) -> str:
    """Reinstall a package (core/firmware/reinstall/<pkg>). MODIFIES the host.

    Args:
        pkg: package name.
    """
    return await _run(_post, "core/firmware/reinstall/" + pkg)


@mcp.tool()
async def opnsense_pkg_lock(pkg: str, locked: bool = True) -> str:
    """Lock/unlock a package against auto-update
    (core/firmware/lock|unlock/<pkg>).

    Args:
        pkg: package name.
        locked: True to lock, False to unlock.
    """
    path = "core/firmware/" + ("lock" if locked else "unlock") + "/" + pkg
    return await _run(_post, path)


@mcp.tool()
async def opnsense_pkg_details(pkg: str) -> str:
    """Package details (core/firmware/details/<pkg>).

    Args:
        pkg: package name.
    """
    return await _run(_post, "core/firmware/details/" + pkg)


@mcp.tool()
async def opnsense_pkg_license(pkg: str) -> str:
    """Package license text (core/firmware/license/<pkg>).

    Args:
        pkg: package name.
    """
    return await _run(_post, "core/firmware/license/" + pkg)


# --------------------------------------------------------------------------
# cron
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_cron_get() -> str:
    """Cron settings (cron/settings/get). Note: plugin-style "cron" module on
    26.7, not "core/cron".
    """
    return await _run(_get, "cron/settings/get")


@mcp.tool()
async def opnsense_cron_search_jobs(current: int = 1, row_count: int = 100,
                                    search: Optional[str] = None) -> str:
    """List cron jobs (cron/settings/search_jobs).

    Args:
        current: page number.
        row_count: rows per page.
        search: optional search phrase.
    """
    params = {"current": current, "rowCount": row_count}
    if search:
        params["searchPhrase"] = search
    return await _run(_get, "cron/settings/search_jobs", params=params)


@mcp.tool()
async def opnsense_cron_add_job(job: dict) -> str:
    """Add a cron job (cron/settings/add_job).

    Args:
        job: job dict, e.g. {"name":"myjob","action":"...","enabled":"1"}.
    """
    return await _run(_post, "cron/settings/add_job", body={"job": job})


@mcp.tool()
async def opnsense_cron_set_job(uuid: str, job: dict) -> str:
    """Update a cron job (cron/settings/set_job/<uuid>).

    Args:
        uuid: job uuid (from opnsense_cron_search_jobs).
        job: new job dict.
    """
    return await _run(_post, "cron/settings/set_job/" + uuid,
                      body={"job": job})


@mcp.tool()
async def opnsense_cron_delete_job(uuid: str) -> str:
    """Delete a cron job (cron/settings/del_job/<uuid>).

    Args:
        uuid: job uuid.
    """
    return await _run(_post, "cron/settings/del_job/" + uuid)


@mcp.tool()
async def opnsense_cron_toggle_job(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a cron job (cron/settings/toggle_job/<uuid>/<0|1>).

    Args:
        uuid: job uuid.
        enabled: True to enable, False to disable.
    """
    return await _run(_post,
                      "cron/settings/toggle_job/" + uuid +
                      "/1" if enabled else
                      "cron/settings/toggle_job/" + uuid + "/0")


@mcp.tool()
async def opnsense_cron_set(settings: dict) -> str:
    """Update cron settings (cron/settings/set).

    Args:
        settings: settings dict as returned by opnsense_cron_get.
    """
    return await _run(_post, "cron/settings/set", body={"job": settings})


@mcp.tool()
async def opnsense_cron_reconfigure() -> str:
    """Apply staged cron changes (cron/service/reconfigure)."""
    return await _run(_post, "cron/service/reconfigure")


# --------------------------------------------------------------------------
# core extras: snapshots, tunables, hasync, menu, defaults
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_snapshots_search() -> str:
    """List config snapshots (core/snapshots/search)."""
    return await _run(_get, "core/snapshots/search")


@mcp.tool()
async def opnsense_snapshots_add(name: str, description: str = "") -> str:
    """Create a config snapshot (core/snapshots/add).

    Args:
        name: snapshot name.
        description: optional description.
    """
    item = {"name": name, "description": description}
    return await _run(_post, "core/snapshots/add", body={"snapshot": item})


@mcp.tool()
async def opnsense_snapshots_delete(uuid: str) -> str:
    """Delete a config snapshot (core/snapshots/del/<uuid>).

    Args:
        uuid: snapshot uuid.
    """
    return await _run(_post, "core/snapshots/del/" + uuid)


@mcp.tool()
async def opnsense_snapshots_activate(uuid: str) -> str:
    """Activate/restore a config snapshot (core/snapshots/activate/<uuid>).
    MODIFIES the host configuration.

    Args:
        uuid: snapshot uuid.
    """
    return await _run(_post, "core/snapshots/activate/" + uuid)


@mcp.tool()
async def opnsense_tunables_search() -> str:
    """List tunables (core/tunables/search_item)."""
    return await _run(_post, "core/tunables/search_item", body={})


@mcp.tool()
async def opnsense_tunables_add(name: str, description: str = "",
                                enabled: bool = True) -> str:
    """Add a tunable (core/tunables/add_item).

    Args:
        name: tunable name (sysctl), e.g. "net.inet.tcp.sendspace".
        description: optional description.
        enabled: whether the tunable is active.
    """
    item = {"name": name, "description": description,
            "enabled": "1" if enabled else "0"}
    return await _run(_post, "core/tunables/add_item", body={"tunable": item})


@mcp.tool()
async def opnsense_tunables_delete(uuid: str) -> str:
    """Delete a tunable (core/tunables/del_item/<uuid>).

    Args:
        uuid: tunable uuid.
    """
    return await _run(_post, "core/tunables/del_item/" + uuid)


@mcp.tool()
async def opnsense_tunables_reconfigure() -> str:
    """Apply staged tunable changes (core/tunables/reconfigure)."""
    return await _run(_post, "core/tunables/reconfigure")


@mcp.tool()
async def opnsense_hasync_get() -> str:
    """High-availability (pfsync) settings (core/hasync/get)."""
    return await _run(_get, "core/hasync/get")


@mcp.tool()
async def opnsense_hasync_set(settings: dict) -> str:
    """Update hasync settings (core/hasync/set).

    Args:
        settings: settings dict as returned by opnsense_hasync_get.
    """
    return await _run(_post, "core/hasync/set", body={"hasync": settings})


@mcp.tool()
async def opnsense_hasync_reconfigure() -> str:
    """Apply staged hasync changes (core/hasync/reconfigure)."""
    return await _run(_post, "core/hasync/reconfigure")


@mcp.tool()
async def opnsense_hasync_status_services() -> str:
    """HA service replication status (core/hasync_status/services)."""
    return await _run(_get, "core/hasync_status/services")


@mcp.tool()
async def opnsense_hasync_status_version() -> str:
    """HA version info (core/hasync_status/version)."""
    return await _run(_get, "core/hasync_status/version")


@mcp.tool()
async def opnsense_hasync_status_restart(service: str = None,
                                         service_id: str = None) -> str:
    """Restart an HA replication service (core/hasync_status/restart).

    Args:
        service: service id (default all).
        service_id: optional service instance id.
    """
    path = "core/hasync_status/restart"
    if service:
        path += "/" + service
        if service_id:
            path += "/" + service_id
    return await _run(_post, path)


@mcp.tool()
async def opnsense_menu_tree() -> str:
    """Full UI menu tree (core/menu/tree). Large; use to discover which
    modules/plugins are installed and their pages.
    """
    return await _run(_get, "core/menu/tree")


@mcp.tool()
async def opnsense_defaults_get() -> str:
    """Factory-defaults reference value (core/defaults/get)."""
    return await _run(_get, "core/defaults/get")


@mcp.tool()
async def opnsense_dashboard_save_widgets(widgets: dict) -> str:
    """Save dashboard widget layout (core/dashboard/save_widgets).

    Args:
        widgets: widget layout dict.
    """
    return await _run(_post, "core/dashboard/save_widgets",
                      body={"widgets": widgets})


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

    This is the endpoint that actually applies an IP change: the REST
    ``set_item`` endpoint silently drops IP fields on an interface node that
    does not already carry them, so the proven path for "give an interface an
    IP" is (1) submit the legacy ``interfaces.php?if=<if>`` WebUI form, then
    (2) call this tool to bring the new address up. ``reconfigure`` is
    todo-only and will NOT apply an IP. Verify afterwards via
    ``interfaces/overview/interfaces_info`` (rows carry ``status``/``addr4``).

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

    Notes (learned live):
        * ``protocol`` is an OptionField — over-specific values like
          ``"ICMPv4"`` can fail with ``validations: {"rule.protocol":
          "Option [] not in list."}``. For a reliable pass rule use
          ``protocol:"any"`` + ``ipprotocol:"inet"`` (IPv4).
        * To target the firewall's own address use ``destination_type:"this"``.
        * source/destination net type must match ``ipprotocol``.

    Args:
        rule: rule object, e.g. {"description":"allow lan->10.0.0.0/24",
            "interface":"lan","source_net":"192.168.0.0/24",
            "destination_net":"10.0.0.0/24","protocol":"TCP","target":"pass"}.
            A verified "allow 192.168.50.0/24 to the firewall itself" rule:
            {"interface":"opt1","ipprotocol":"inet","protocol":"any",
             "source_net":"192.168.50.0/24","destination_type":"this",
             "target":"pass"}.
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


# --------------------------------------------------------------------------
# interfaces extras: global settings, assignment, loopback / neighbor /
# bridge / gif / gre / vxlan / vip
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_get_interfaces_settings() -> str:
    """Global interface settings (interfaces/settings/get): offload flags,
    IPv6/DHCPv6 options, DUIDs.
    """
    return await _run(_get, "interfaces/settings/get")


@mcp.tool()
async def opnsense_set_interfaces_settings(settings: dict) -> str:
    """Update global interface settings (interfaces/settings/set).

    Args:
        settings: the "settings" object as returned by
            opnsense_get_interfaces_settings.
    """
    return await _run(_post, "interfaces/settings/set",
                      body={"settings": settings})


@mcp.tool()
async def opnsense_search_interface_assignment(search: Optional[str] = None,
                                               current: int = 1,
                                               row_count: int = 100) -> str:
    """List logical interface assignments (interfaces/assignment/search_item;
    new in 26.7). Maps logical roles (lan, opt1, ...) to physical devices.

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "interfaces/assignment/search_item", body=body)


@mcp.tool()
async def opnsense_get_interface_assignment(ifname: str) -> str:
    """Get one interface assignment (interfaces/assignment/get_item/<ifname>).

    Args:
        ifname: logical interface name, e.g. "lan", "opt1".
    """
    return await _run(_get, f"interfaces/assignment/get_item/{ifname}")


@mcp.tool()
async def opnsense_add_interface_assignment(item: dict) -> str:
    """Add a logical interface assignment (interfaces/assignment/add_item).
    Staged until opnsense_reconfigure_interface_assignment.

    Args:
        item: assignment dict, e.g. {"if":"vtnet2","descr":"opt5",
            "enable":"1","type4":"none","type6":"none"} (full field list as
            shown in opnsense_search_interface_assignment).
    """
    return await _run(_post, "interfaces/assignment/add_item",
                      body={"interface": item})


@mcp.tool()
async def opnsense_set_interface_assignment(ifname: str, item: dict) -> str:
    """Update an interface assignment
    (interfaces/assignment/set_item/<ifname>).

    Args:
        ifname: logical interface name.
        item: assignment dict as shown in
            opnsense_search_interface_assignment.
    """
    return await _run(_post, f"interfaces/assignment/set_item/{ifname}",
                      body={"interface": item})


@mcp.tool()
async def opnsense_delete_interface_assignment(ifnames: str) -> str:
    """Delete interface assignment(s) (interfaces/assignment/del_item).
    Fails if the interface is in a group/bridge/tunnel or locked.

    Args:
        ifnames: comma-separated logical interface names.
    """
    return await _run(_post, f"interfaces/assignment/del_item/{ifnames}")


@mcp.tool()
async def opnsense_reconfigure_interface_assignment() -> str:
    """Apply staged interface assignment changes
    (interfaces/assignment/reconfigure). MODIFIES live interfaces.
    """
    return await _run(_post, "interfaces/assignment/reconfigure")


def _interface_type_tools(type_: str, label: str) -> None:
    """Register list/add/delete/set/reconfigure tools for one interface
    type (26.7 pattern: interfaces/<type>_settings/<command>)."""
    base = f"interfaces/{type_}_settings"

    async def list_fn() -> str:
        return await _run(_get, f"{base}/get")
    list_fn.__name__ = f"opnsense_list_{type_}"
    list_fn.__doc__ = f"List {label} interfaces ({base}/get)."
    mcp.tool()(list_fn)

    async def add_fn(item: dict) -> str:
        return await _run(_post, f"{base}/add_item", body={type_: item})
    add_fn.__name__ = f"opnsense_add_{type_}"
    add_fn.__doc__ = (f"Add a {label} interface ({base}/add_item). Staged "
                      f"until reconfigure. Args: item dict as shown in the "
                      f"matching list tool.")
    mcp.tool()(add_fn)

    async def delete_fn(uuid: str) -> str:
        return await _run(_post, f"{base}/del_item/{uuid}")
    delete_fn.__name__ = f"opnsense_delete_{type_}"
    delete_fn.__doc__ = f"Delete a {label} interface ({base}/del_item/<uuid>)."
    mcp.tool()(delete_fn)

    async def set_fn(uuid: str, item: dict) -> str:
        return await _run(_post, f"{base}/set_item/{uuid}", body={type_: item})
    set_fn.__name__ = f"opnsense_set_{type_}"
    set_fn.__doc__ = f"Update a {label} interface ({base}/set_item/<uuid>)."
    mcp.tool()(set_fn)

    async def reconfigure_fn() -> str:
        return await _run(_post, f"{base}/reconfigure")
    reconfigure_fn.__name__ = f"opnsense_reconfigure_{type_}"
    reconfigure_fn.__doc__ = (f"Apply staged {label} changes ({base}/"
                              f"reconfigure).")
    mcp.tool()(reconfigure_fn)


for _t, _lbl in (("loopback", "loopback (lo subinterface)"),
                 ("neighbor", "static neighbor (ARP/NDP) entry"),
                 ("bridge", "bridge"),
                 ("gif", "gif tunnel"),
                 ("gre", "gre tunnel"),
                 ("vxlan", "vxlan interface"),
                 ("vip", "virtual IP (carp/vip)")):
    _interface_type_tools(_t, _lbl)
del _t, _lbl


# --------------------------------------------------------------------------
# firewall: aliases, interface groups, alias util, filter util
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_list_aliases(search: Optional[str] = None,
                                current: int = 1, row_count: int = 100) -> str:
    """List firewall aliases (firewall/alias/search_item).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "firewall/alias/search_item", body=body)


@mcp.tool()
async def opnsense_get_alias(uuid: str) -> str:
    """Get one alias by uuid (firewall/alias/get_item/<uuid>)."""
    return await _run(_get, f"firewall/alias/get_item/{uuid}")


@mcp.tool()
async def opnsense_add_alias(alias: dict) -> str:
    """Add a firewall alias (firewall/alias/add_item). Staged until
    reconfigure.

    Args:
        alias: e.g. {"name":"SERVERS","type":"host","address":"10.0.0.5",
            "description":"web server"}.
    """
    return await _run(_post, "firewall/alias/add_item", body={"alias": alias})


@mcp.tool()
async def opnsense_set_alias(uuid: str, alias: dict) -> str:
    """Update an alias (firewall/alias/set_item/<uuid>)."""
    return await _run(_post, f"firewall/alias/set_item/{uuid}",
                      body={"alias": alias})


@mcp.tool()
async def opnsense_delete_alias(uuid: str) -> str:
    """Delete an alias (firewall/alias/del_item/<uuid>). Fails if still in
    use by a rule.
    """
    return await _run(_post, f"firewall/alias/del_item/{uuid}")


@mcp.tool()
async def opnsense_toggle_alias(uuid: str, enabled: bool = True) -> str:
    """Enable/disable an alias (firewall/alias/toggle_item/<uuid>/<0|1>)."""
    return await _run(_post,
                      f"firewall/alias/toggle_item/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_reconfigure_aliases() -> str:
    """Apply staged alias changes (firewall/alias/reconfigure)."""
    return await _run(_post, "firewall/alias/reconfigure")


@mcp.tool()
async def opnsense_alias_util_tables() -> str:
    """List active live pf alias table names (firewall/alias_util/aliases)."""
    return await _run(_get, "firewall/alias_util/aliases")


@mcp.tool()
async def opnsense_alias_util_list(name: str) -> str:
    """List the contents of a live pf alias table
    (firewall/alias_util/list/<name>). Rows carry the "ip" field.

    Args:
        name: alias table name.
    """
    return await _run(_get, f"firewall/alias_util/list/{name}")


@mcp.tool()
async def opnsense_alias_util_add(name: str, address: str) -> str:
    """Add an address to an existing live pf alias table
    (firewall/alias_util/add/<name>).

    Args:
        name: existing alias name (config alias of type host/network).
        address: IP or network, e.g. "10.0.0.9" or "10.0.0.0/24".
    """
    return await _run(_post, f"firewall/alias_util/add/{name}",
                      body={"address": address})


@mcp.tool()
async def opnsense_alias_util_delete(name: str, address: str) -> str:
    """Delete an address from a live pf alias table
    (firewall/alias_util/delete/<name>).

    Args:
        name: alias table name.
        address: address to remove.
    """
    return await _run(_post, f"firewall/alias_util/delete/{name}",
                      body={"address": address})


@mcp.tool()
async def opnsense_alias_util_flush(name: str) -> str:
    """Flush (empty) a live pf alias table (firewall/alias_util/flush/<name>).
    """
    return await _run(_post, f"firewall/alias_util/flush/{name}")


@mcp.tool()
async def opnsense_alias_util_find_references(ip: str) -> str:
    """Find which rules use an IP address (firewall/alias_util/find_references).

    Args:
        ip: IP address to look up.
    """
    return await _run(_post, "firewall/alias_util/find_references",
                      body={"ip": ip})


@mcp.tool()
async def opnsense_list_groups(search: Optional[str] = None,
                               current: int = 1, row_count: int = 100) -> str:
    """List interface groups (firewall/group/search_item).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "firewall/group/search_item", body=body)


@mcp.tool()
async def opnsense_add_group(group: dict) -> str:
    """Add an interface group (firewall/group/add_item).

    Args:
        group: e.g. {"ifname":"LANIFs","members":["lan","opt1"],
            "sequence":0,"descr":"lan + opt1"}.
    """
    return await _run(_post, "firewall/group/add_item", body={"group": group})


@mcp.tool()
async def opnsense_set_group(uuid: str, group: dict) -> str:
    """Update an interface group (firewall/group/set_item/<uuid>)."""
    return await _run(_post, f"firewall/group/set_item/{uuid}",
                      body={"group": group})


@mcp.tool()
async def opnsense_delete_group(uuid: str) -> str:
    """Delete an interface group (firewall/group/del_item/<uuid>)."""
    return await _run(_post, f"firewall/group/del_item/{uuid}")


@mcp.tool()
async def opnsense_get_group(uuid: str) -> str:
    """Get one interface group (firewall/group/get_item/<uuid>)."""
    return await _run(_get, f"firewall/group/get_item/{uuid}")


@mcp.tool()
async def opnsense_reconfigure_groups() -> str:
    """Apply staged interface-group changes (firewall/group/reconfigure)."""
    return await _run(_post, "firewall/group/reconfigure")


@mcp.tool()
async def opnsense_filter_util_rule_stats() -> str:
    """Per-rule packet/byte statistics (firewall/filter_util/rule_stats)."""
    return await _run(_get, "firewall/filter_util/rule_stats")


# --------------------------------------------------------------------------
# firewall rule tables: move / log-toggle for all tables, plus 1:1 and
# source-NAT CRUD and the d_nat toggle (disabled-flag semantics)
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_move_rule_before(uuid: str, target: str) -> str:
    """Move a firewall rule above the rule with uuid ``target``
    (firewall/filter/moveRuleBefore/<uuid>/<target>). Staged until apply.
    """
    return await _run(_post,
                      f"firewall/filter/moveRuleBefore/{uuid}/{target}")


@mcp.tool()
async def opnsense_toggle_rule_log(uuid: str, enabled: bool = True) -> str:
    """Enable/disable logging on a firewall rule
    (firewall/filter/toggleRuleLog/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"firewall/filter/toggleRuleLog/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_toggle_port_forward(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a port-forward rule (firewall/d_nat/toggleRule).

    NOTE: on 26.7 the d_nat toggle path carries the *disabled* flag,
    opposite to the other rule tables.

    Args:
        uuid: rule uuid.
        enabled: True to enable, False to disable.
    """
    return await _run(_post,
                      f"firewall/d_nat/toggleRule/{uuid}/{'0' if enabled else '1'}")


@mcp.tool()
async def opnsense_set_port_forward(uuid: str, rule: dict) -> str:
    """Update a port-forward rule (firewall/d_nat/setRule/<uuid>).
    Staged until apply.
    """
    return await _run(_post, f"firewall/d_nat/setRule/{uuid}",
                      body={"rule": rule})


@mcp.tool()
async def opnsense_move_port_forward_before(uuid: str, target: str) -> str:
    """Move a port-forward rule above the rule with uuid ``target``
    (firewall/d_nat/moveRuleBefore). Staged until apply.
    """
    return await _run(_post,
                      f"firewall/d_nat/moveRuleBefore/{uuid}/{target}")


@mcp.tool()
async def opnsense_add_one2one_rule(rule: dict) -> str:
    """Add a 1:1 NAT rule (firewall/one_to_one/addRule). Staged until apply.

    Args:
        rule: rule object as returned by opnsense_list_one2one.
    """
    return await _run(_post, "firewall/one_to_one/addRule", body={"rule": rule})


@mcp.tool()
async def opnsense_set_one2one_rule(uuid: str, rule: dict) -> str:
    """Update a 1:1 NAT rule (firewall/one_to_one/setRule/<uuid>).
    Staged until apply.
    """
    return await _run(_post, f"firewall/one_to_one/setRule/{uuid}",
                      body={"rule": rule})


@mcp.tool()
async def opnsense_delete_one2one_rule(uuid: str) -> str:
    """Delete a 1:1 NAT rule (firewall/one_to_one/delRule/<uuid>)."""
    return await _run(_post, f"firewall/one_to_one/delRule/{uuid}")


@mcp.tool()
async def opnsense_toggle_one2one_rule(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a 1:1 NAT rule (firewall/one_to_one/toggleRule)."""
    return await _run(_post,
                      f"firewall/one_to_one/toggleRule/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_move_one2one_rule_before(uuid: str, target: str) -> str:
    """Move a 1:1 NAT rule above the rule with uuid ``target``
    (firewall/one_to_one/moveRuleBefore). Staged until apply.
    """
    return await _run(_post,
                      f"firewall/one_to_one/moveRuleBefore/{uuid}/{target}")


@mcp.tool()
async def opnsense_add_source_nat_rule(rule: dict) -> str:
    """Add an outbound (source) NAT rule (firewall/source_nat/addRule).
    Staged until apply.

    Args:
        rule: rule object as returned by opnsense_list_outbound_nat.
    """
    return await _run(_post, "firewall/source_nat/addRule", body={"rule": rule})


@mcp.tool()
async def opnsense_set_source_nat_rule(uuid: str, rule: dict) -> str:
    """Update an outbound (source) NAT rule
    (firewall/source_nat/setRule/<uuid>). Staged until apply.
    """
    return await _run(_post, f"firewall/source_nat/setRule/{uuid}",
                      body={"rule": rule})


@mcp.tool()
async def opnsense_delete_source_nat_rule(uuid: str) -> str:
    """Delete an outbound (source) NAT rule
    (firewall/source_nat/delRule/<uuid>).
    """
    return await _run(_post, f"firewall/source_nat/delRule/{uuid}")


@mcp.tool()
async def opnsense_toggle_source_nat_rule(uuid: str, enabled: bool = True) -> str:
    """Enable/disable an outbound (source) NAT rule
    (firewall/source_nat/toggleRule).
    """
    return await _run(_post,
                      f"firewall/source_nat/toggleRule/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_move_source_nat_rule_before(uuid: str, target: str) -> str:
    """Move an outbound (source) NAT rule above the rule with uuid ``target``
    (firewall/source_nat/moveRuleBefore). Staged until apply.
    """
    return await _run(_post,
                      f"firewall/source_nat/moveRuleBefore/{uuid}/{target}")


# --------------------------------------------------------------------------
# routing: gateways (routing/settings/*), gateway groups, static routes
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_get_routing_settings() -> str:
    """Gateway configuration (routing/settings/get)."""
    return await _run(_get, "routing/settings/get")


@mcp.tool()
async def opnsense_set_routing_settings(gateways: dict) -> str:
    """Update gateway configuration (routing/settings/set).

    Args:
        gateways: the "gateways" object as returned by
            opnsense_get_routing_settings.
    """
    return await _run(_post, "routing/settings/set",
                      body={"gateways": gateways})


@mcp.tool()
async def opnsense_search_gateway(search: Optional[str] = None,
                                  current: int = 1,
                                  row_count: int = 100) -> str:
    """List gateways (routing/settings/search_gateway).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "routing/settings/search_gateway", body=body)


@mcp.tool()
async def opnsense_get_gateway(uuid: str) -> str:
    """Get one gateway (routing/settings/get_gateway/<uuid>)."""
    return await _run(_get, f"routing/settings/get_gateway/{uuid}")


@mcp.tool()
async def opnsense_add_gateway(gateway: dict) -> str:
    """Add a gateway (routing/settings/add_gateway).

    Args:
        gateway: e.g. {"interface":"wan","gateway":"192.168.1.1",
            "name":"GW2","weight":"1","description":"failover wan"}.
    """
    return await _run(_post, "routing/settings/add_gateway",
                      body={"gateway_item": gateway})


@mcp.tool()
async def opnsense_set_gateway(uuid: str, gateway: dict) -> str:
    """Update a gateway (routing/settings/set_gateway/<uuid>)."""
    return await _run(_post, f"routing/settings/set_gateway/{uuid}",
                      body={"gateway_item": gateway})


@mcp.tool()
async def opnsense_delete_gateway(uuid: str) -> str:
    """Delete a gateway (routing/settings/del_gateway/<uuid>)."""
    return await _run(_post, f"routing/settings/del_gateway/{uuid}")


@mcp.tool()
async def opnsense_toggle_gateway(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a gateway
    (routing/settings/toggle_gateway/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"routing/settings/toggle_gateway/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_reconfigure_routing() -> str:
    """Apply staged gateway changes (routing/settings/reconfigure)."""
    return await _run(_post, "routing/settings/reconfigure")


@mcp.tool()
async def opnsense_get_gateway_groups() -> str:
    """Gateway groups (routing/group_settings/get)."""
    return await _run(_get, "routing/group_settings/get")


@mcp.tool()
async def opnsense_list_routes(search: Optional[str] = None,
                               current: int = 1, row_count: int = 100) -> str:
    """List static routes (routes/routes/searchroute).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "routes/routes/searchroute", body=body)


@mcp.tool()
async def opnsense_get_route(uuid: str) -> str:
    """Get one static route (routes/routes/getroute/<uuid>)."""
    return await _run(_get, f"routes/routes/getroute/{uuid}")


@mcp.tool()
async def opnsense_add_route(route: dict) -> str:
    """Add a static route (routes/routes/addroute).

    Args:
        route: e.g. {"network":"10.9.0.0/24","gateway":"WAN_DHCP",
            "description":"vpn"}; "gateway" is a configured gateway name
            (from opnsense_search_gateway), not an interface name.
    """
    return await _run(_post, "routes/routes/addroute", body={"route": route})


@mcp.tool()
async def opnsense_set_route(uuid: str, route: dict) -> str:
    """Update a static route (routes/routes/setroute/<uuid>)."""
    return await _run(_post, f"routes/routes/setroute/{uuid}",
                      body={"route": route})


@mcp.tool()
async def opnsense_delete_route(uuid: str) -> str:
    """Delete a static route (routes/routes/delroute/<uuid>)."""
    return await _run(_post, f"routes/routes/delroute/{uuid}")


@mcp.tool()
async def opnsense_toggle_route(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a static route (routes/routes/toggleroute).

    NOTE: the path carries the *disabled* flag (opposite of enabled).
    """
    return await _run(_post,
                      f"routes/routes/toggleroute/{uuid}/{'0' if enabled else '1'}")


@mcp.tool()
async def opnsense_reconfigure_routes() -> str:
    """Apply staged static-route changes (routes/routes/reconfigure)."""
    return await _run(_post, "routes/routes/reconfigure")


@mcp.tool()
async def opnsense_get_gateway_status() -> str:
    """Gateway up/down status (routes/gateway/status)."""
    return await _run(_get, "routes/gateway/status")


# --------------------------------------------------------------------------
# auth: users, API keys, auth groups, privileges
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_add_user(user: dict) -> str:
    """Add a local web/API user (auth/user/add).

    Args:
        user: e.g. {"name":"jdoe","scope":"internal","email":"jdoe@example.com",
            "priv":"full","password":"<plaintext>"} (model hashes it).
    """
    return await _run(_post, "auth/user/add", body={"user": user})


@mcp.tool()
async def opnsense_get_user(uuid: str) -> str:
    """Get one user by uuid (auth/user/get/<uuid>)."""
    return await _run(_get, f"auth/user/get/{uuid}")


@mcp.tool()
async def opnsense_delete_user(uuid: str) -> str:
    """Delete a local user (auth/user/del/<uuid>)."""
    return await _run(_post, f"auth/user/del/{uuid}")


@mcp.tool()
async def opnsense_add_api_key(username: str) -> str:
    """Generate a new API key for a user
    (auth/user/add_api_key/<username>). The key/secret is returned once
    and is not stored.
    """
    return await _run(_post, f"auth/user/add_api_key/{username}")


@mcp.tool()
async def opnsense_search_api_key(username: str = "") -> str:
    """List API keys for a user (auth/user/search_api_key/<username>).

    Args:
        username: user name; empty for all users.
    """
    path = "auth/user/search_api_key"
    if username:
        path += "/" + username
    return await _run(_get, path)


@mcp.tool()
async def opnsense_delete_api_key(username: str, key_id: str) -> str:
    """Delete an API key (auth/user/del_api_key/<username>/<id>)."""
    return await _run(_post, f"auth/user/del_api_key/{username}/{key_id}")


@mcp.tool()
async def opnsense_list_auth_groups(search: Optional[str] = None,
                                    current: int = 1,
                                    row_count: int = 100) -> str:
    """List authentication groups (auth/group/search).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "auth/group/search", body=body)


@mcp.tool()
async def opnsense_add_auth_group(group: dict) -> str:
    """Add an authentication group (auth/group/add).

    Args:
        group: e.g. {"name":"ops","description":"operators"}.
    """
    return await _run(_post, "auth/group/add", body={"group": group})


@mcp.tool()
async def opnsense_set_auth_group(uuid: str, group: dict) -> str:
    """Update an authentication group (auth/group/set/<uuid>)."""
    return await _run(_post, f"auth/group/set/{uuid}", body={"group": group})


@mcp.tool()
async def opnsense_delete_auth_group(uuid: str) -> str:
    """Delete an authentication group (auth/group/del/<uuid>)."""
    return await _run(_post, f"auth/group/del/{uuid}")


@mcp.tool()
async def opnsense_list_privs(search: Optional[str] = None,
                              current: int = 1, row_count: int = 100) -> str:
    """List privilege definitions with user/group assignments
    (auth/priv/search).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "auth/priv/search", body=body)


# --------------------------------------------------------------------------
# diagnostics: firewall log/states, ping, portprobe, traceroute
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_get_firewall_log(limit: int = 100, start: int = 0,
                                    since: Optional[int] = None) -> str:
    """Firewall log (diagnostics/firewall/log).

    Args:
        limit: max lines (default 100).
        start: start line.
        since: unix epoch to start from.
    """
    params = {"limit": limit, "start": start}
    if since:
        params["since"] = since
    return await _run(_get, "diagnostics/firewall/log", params=params)


@mcp.tool()
async def opnsense_get_firewall_log_stats() -> str:
    """Firewall log statistics (diagnostics/firewall/stats)."""
    return await _run(_get, "diagnostics/firewall/stats")


@mcp.tool()
async def opnsense_get_firewall_log_filters() -> str:
    """Firewall log filter options (diagnostics/firewall/log_filters)."""
    return await _run(_get, "diagnostics/firewall/log_filters")


@mcp.tool()
async def opnsense_list_rule_ids() -> str:
    """Rule IDs present in the log (diagnostics/firewall/list_rule_ids)."""
    return await _run(_get, "diagnostics/firewall/list_rule_ids")


@mcp.tool()
async def opnsense_query_pf_states() -> str:
    """Query the active pf state table (diagnostics/firewall/query_states).
    """
    return await _run(_post, "diagnostics/firewall/query_states")


@mcp.tool()
async def opnsense_query_pf_top() -> str:
    """Top pf states by traffic (diagnostics/firewall/query_pf_top)."""
    return await _run(_post, "diagnostics/firewall/query_pf_top")


@mcp.tool()
async def opnsense_kill_pf_states(states: list) -> str:
    """Kill active pf states (diagnostics/firewall/kill_states).

    Args:
        states: list of state entries from opnsense_query_pf_states.
    """
    return await _run(_post, "diagnostics/firewall/kill_states",
                      body={"states": states})


@mcp.tool()
async def opnsense_flush_pf_states() -> str:
    """Flush the entire pf state table (diagnostics/firewall/flush_states).
    """
    return await _run(_post, "diagnostics/firewall/flush_states")


@mcp.tool()
async def opnsense_flush_pf_sources(sources: list) -> str:
    """Flush states created by the given source IPs
    (diagnostics/firewall/flush_sources).

    Args:
        sources: list of source IP addresses.
    """
    return await _run(_post, "diagnostics/firewall/flush_sources",
                      body={"sources": sources})


@mcp.tool()
async def opnsense_ping_set(settings: dict) -> str:
    """Configure a continuous ping job (diagnostics/ping/set) and get its
    uuid. Model-based double-nested body: {"ping": {"settings": {...}}}.

    This is the way to prove L3 reachability without shell access to the
    guest: start a job to the peer, then read ``opnsense_ping_search_jobs``
    (live ``loss``/``send``/``received``) and/or ``opnsense_query_pf_states``
    (an active ICMP state ``src→dst`` with balanced ``pkts:[out,in]`` = 0%
    loss). There is no ``count`` field — the job runs until you stop it.

    Args:
        settings: e.g. {"hostname":"1.1.1.1","fam":"ip","interval":1}.
    """
    return await _run(_post, "diagnostics/ping/set",
                      body={"ping": {"settings": settings}})


@mcp.tool()
async def opnsense_ping_get() -> str:
    """Latest ping job result (diagnostics/ping/get)."""
    return await _run(_get, "diagnostics/ping/get")


@mcp.tool()
async def opnsense_ping_search_jobs() -> str:
    """List ping jobs (diagnostics/ping/search_jobs)."""
    return await _run(_get, "diagnostics/ping/search_jobs")


@mcp.tool()
async def opnsense_ping_start(job_id: str) -> str:
    """Start a ping job (diagnostics/ping/start/<jobid>)."""
    return await _run(_post, f"diagnostics/ping/start/{job_id}")


@mcp.tool()
async def opnsense_ping_stop(job_id: str) -> str:
    """Stop a ping job (diagnostics/ping/stop/<jobid>)."""
    return await _run(_post, f"diagnostics/ping/stop/{job_id}")


@mcp.tool()
async def opnsense_ping_remove(job_id: str) -> str:
    """Remove a ping job (diagnostics/ping/remove/<jobid>).

    Fails while the job is still running — call ``opnsense_ping_stop`` first,
    then this.
    """
    return await _run(_post, f"diagnostics/ping/remove/{job_id}")


@mcp.tool()
async def opnsense_portprobe_set(settings: dict) -> str:
    """Run a port probe (diagnostics/portprobe/set, one-shot).
    Double-nested body: {"portprobe": {"settings": {...}}}.

    Args:
        settings: e.g. {"hostname":"1.1.1.1","port_list":"80","timeout":5}.
    """
    return await _run(_post, "diagnostics/portprobe/set",
                      body={"portprobe": {"settings": settings}})


@mcp.tool()
async def opnsense_portprobe_get() -> str:
    """Latest port-probe result (diagnostics/portprobe/get)."""
    return await _run(_get, "diagnostics/portprobe/get")


@mcp.tool()
async def opnsense_traceroute_set(settings: dict) -> str:
    """Run a traceroute (diagnostics/traceroute/set, one-shot).
    Double-nested body: {"traceroute": {"settings": {...}}}.

    Args:
        settings: e.g. {"hostname":"1.1.1.1","timeout":5,"max_ttl":30}.
    """
    return await _run(_post, "diagnostics/traceroute/set",
                      body={"traceroute": {"settings": settings}})


@mcp.tool()
async def opnsense_traceroute_get() -> str:
    """Latest traceroute result (diagnostics/traceroute/get)."""
    return await _run(_get, "diagnostics/traceroute/get")


# --------------------------------------------------------------------------
# diagnostics: packet capture, NDP/ARP, netflow, system, misc
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_packet_capture_set(settings: dict) -> str:
    """Configure a packet-capture job (diagnostics/packet_capture/set).
    Double-nested body: {"packet_capture": {"settings": {...}}}.

    Args:
        settings: e.g. {"interface":"wan","filter":"port 443",
            "count":"100","timeout":"60"}.
    """
    return await _run(_post, "diagnostics/packet_capture/set",
                      body={"packet_capture": {"settings": settings}})


@mcp.tool()
async def opnsense_packet_capture_search_jobs() -> str:
    """List packet-capture jobs (diagnostics/packet_capture/search_jobs)."""
    return await _run(_get, "diagnostics/packet_capture/search_jobs")


@mcp.tool()
async def opnsense_packet_capture_start(job_id: str) -> str:
    """Start a packet-capture job (diagnostics/packet_capture/start/<jobid>).
    """
    return await _run(_post, f"diagnostics/packet_capture/start/{job_id}")


@mcp.tool()
async def opnsense_packet_capture_stop(job_id: str) -> str:
    """Stop a packet-capture job (diagnostics/packet_capture/stop/<jobid>)."""
    return await _run(_post, f"diagnostics/packet_capture/stop/{job_id}")


@mcp.tool()
async def opnsense_packet_capture_view(job_id: str,
                                       detail: str = "normal") -> str:
    """View a packet-capture result (diagnostics/packet_capture/view/<jobid>).
    """
    return await _run(_get,
                      f"diagnostics/packet_capture/view/{job_id}/{detail}")


@mcp.tool()
async def opnsense_packet_capture_download(job_id: str) -> str:
    """Download a captured file (diagnostics/packet_capture/download/<jobid>).
    """
    return await _run(_get, f"diagnostics/packet_capture/download/{job_id}")


@mcp.tool()
async def opnsense_packet_capture_remove(job_id: str) -> str:
    """Remove a packet-capture job (diagnostics/packet_capture/remove/<jobid>).
    """
    return await _run(_post, f"diagnostics/packet_capture/remove/{job_id}")


@mcp.tool()
async def opnsense_get_ndp_table() -> str:
    """NDP (IPv6 neighbor) table (diagnostics/interface/get_ndp)."""
    return await _run(_get, "diagnostics/interface/get_ndp")


@mcp.tool()
async def opnsense_search_arp(search: Optional[str] = None,
                              current: int = 1, row_count: int = 100) -> str:
    """Search the ARP table (diagnostics/interface/search_arp).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    params = {"current": current, "rowCount": row_count}
    if search:
        params["searchPhrase"] = search
    return await _run(_get, "diagnostics/interface/search_arp", params=params)


@mcp.tool()
async def opnsense_search_ndp(search: Optional[str] = None,
                              current: int = 1, row_count: int = 100) -> str:
    """Search the NDP table (diagnostics/interface/search_ndp).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    params = {"current": current, "rowCount": row_count}
    if search:
        params["searchPhrase"] = search
    return await _run(_get, "diagnostics/interface/search_ndp", params=params)


@mcp.tool()
async def opnsense_get_netflow_status() -> str:
    """Netflow status (diagnostics/netflow/status)."""
    return await _run(_get, "diagnostics/netflow/status")


@mcp.tool()
async def opnsense_get_netflow_config() -> str:
    """Netflow configuration (diagnostics/netflow/getconfig)."""
    return await _run(_get, "diagnostics/netflow/getconfig")


@mcp.tool()
async def opnsense_set_netflow_config(settings: dict) -> str:
    """Update netflow configuration (diagnostics/netflow/setconfig).

    Args:
        settings: as returned by opnsense_get_netflow_config.
    """
    return await _run(_post, "diagnostics/netflow/setconfig",
                      body={"settings": settings})


@mcp.tool()
async def opnsense_reconfigure_netflow() -> str:
    """Apply staged netflow changes (diagnostics/netflow/reconfigure)."""
    return await _run(_post, "diagnostics/netflow/reconfigure")


@mcp.tool()
async def opnsense_get_system_disk() -> str:
    """Disk usage (diagnostics/system/system_disk)."""
    return await _run(_get, "diagnostics/system/system_disk")


@mcp.tool()
async def opnsense_get_system_swap() -> str:
    """Swap usage (diagnostics/system/system_swap)."""
    return await _run(_get, "diagnostics/system/system_swap")


@mcp.tool()
async def opnsense_get_system_mbuf() -> str:
    """mbuf usage (diagnostics/system/system_mbuf)."""
    return await _run(_get, "diagnostics/system/system_mbuf")


@mcp.tool()
async def opnsense_get_system_temperature() -> str:
    """CPU temperatures (diagnostics/system/system_temperature)."""
    return await _run(_get, "diagnostics/system/system_temperature")


@mcp.tool()
async def opnsense_get_system_resources() -> str:
    """CPU/memory resources (diagnostics/system/system_resources)."""
    return await _run(_get, "diagnostics/system/system_resources")


@mcp.tool()
async def opnsense_get_activity() -> str:
    """Live activity feed (diagnostics/activity/get_activity)."""
    return await _run(_get, "diagnostics/activity/get_activity")


@mcp.tool()
async def opnsense_dns_reverse_lookup() -> str:
    """DNS reverse-lookup options (diagnostics/dns/reverse_lookup)."""
    return await _run(_get, "diagnostics/dns/reverse_lookup")


@mcp.tool()
async def opnsense_get_dns_diagnostics() -> str:
    """DNS diagnostic settings (diagnostics/dns_diagnostics/get)."""
    return await _run(_get, "diagnostics/dns_diagnostics/get")


@mcp.tool()
async def opnsense_get_pfsync_nodes() -> str:
    """pfsync nodes (diagnostics/interface/get_pfsync_nodes)."""
    return await _run(_get, "diagnostics/interface/get_pfsync_nodes")


@mcp.tool()
async def opnsense_get_vip_status() -> str:
    """Virtual IP / CARP status (diagnostics/interface/get_vip_status)."""
    return await _run(_get, "diagnostics/interface/get_vip_status")


# --------------------------------------------------------------------------
# dnsmasq items: hosts, domain overrides, ranges, options, boot, tags
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_set_dhcp_config(dnsmasq: dict) -> str:
    """Update the whole dnsmasq model (dnsmasq/settings/set).

    Args:
        dnsmasq: the "dnsmasq" object as returned by
            opnsense_get_dhcp_config.
    """
    return await _run(_post, "dnsmasq/settings/set", body={"dnsmasq": dnsmasq})


@mcp.tool()
async def opnsense_search_dhcp_hosts(search: Optional[str] = None,
                                     current: int = 1,
                                     row_count: int = 100) -> str:
    """List static DHCP hosts (dnsmasq/settings/search_host).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "dnsmasq/settings/search_host", body=body)


@mcp.tool()
async def opnsense_get_dhcp_host(uuid: str) -> str:
    """Get one static DHCP host (dnsmasq/settings/get_host/<uuid>)."""
    return await _run(_get, f"dnsmasq/settings/get_host/{uuid}")


@mcp.tool()
async def opnsense_add_dhcp_host(host: dict) -> str:
    """Add a static DHCP host (dnsmasq/settings/add_host).

    Args:
        host: e.g. {"interface":"lan","hostname":"nas","ip":"192.168.1.50",
            "mac":"aa:bb:cc:dd:ee:ff"}.
    """
    return await _run(_post, "dnsmasq/settings/add_host", body={"host": host})


@mcp.tool()
async def opnsense_set_dhcp_host(uuid: str, host: dict) -> str:
    """Update a static DHCP host (dnsmasq/settings/set_host/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/set_host/{uuid}",
                      body={"host": host})


@mcp.tool()
async def opnsense_delete_dhcp_host(uuid: str) -> str:
    """Delete a static DHCP host (dnsmasq/settings/del_host/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/del_host/{uuid}")


@mcp.tool()
async def opnsense_search_dhcp_domains(search: Optional[str] = None,
                                       current: int = 1,
                                       row_count: int = 100) -> str:
    """List domain overrides (dnsmasq/settings/search_domain)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "dnsmasq/settings/search_domain", body=body)


@mcp.tool()
async def opnsense_add_dhcp_domain(domain: dict) -> str:
    """Add a domain override (dnsmasq/settings/add_domain).

    Args:
        domain: e.g. {"domain":"lab.local","server":"10.0.0.53"} (keys as in
            opnsense_search_dhcp_domains).
    """
    return await _run(_post, "dnsmasq/settings/add_domain",
                      body={"domainoverride": domain})


@mcp.tool()
async def opnsense_set_dhcp_domain(uuid: str, domain: dict) -> str:
    """Update a domain override (dnsmasq/settings/set_domain/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/set_domain/{uuid}",
                      body={"domainoverride": domain})


@mcp.tool()
async def opnsense_delete_dhcp_domain(uuid: str) -> str:
    """Delete a domain override (dnsmasq/settings/del_domain/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/del_domain/{uuid}")


@mcp.tool()
async def opnsense_search_dhcp_ranges(search: Optional[str] = None,
                                      current: int = 1,
                                      row_count: int = 100) -> str:
    """List DHCP ranges (dnsmasq/settings/search_range)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "dnsmasq/settings/search_range", body=body)


@mcp.tool()
async def opnsense_add_dhcp_range(rng: dict) -> str:
    """Add a DHCP range (dnsmasq/settings/add_range).

    Args:
        rng: e.g. {"interface":"lan","range":"192.168.1.100-192.168.1.200"}.
    """
    return await _run(_post, "dnsmasq/settings/add_range", body={"range": rng})


@mcp.tool()
async def opnsense_set_dhcp_range(uuid: str, rng: dict) -> str:
    """Update a DHCP range (dnsmasq/settings/set_range/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/set_range/{uuid}",
                      body={"range": rng})


@mcp.tool()
async def opnsense_delete_dhcp_range(uuid: str) -> str:
    """Delete a DHCP range (dnsmasq/settings/del_range/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/del_range/{uuid}")


@mcp.tool()
async def opnsense_search_dhcp_options(search: Optional[str] = None,
                                       current: int = 1,
                                       row_count: int = 100) -> str:
    """List DHCP options (dnsmasq/settings/search_option)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "dnsmasq/settings/search_option", body=body)


@mcp.tool()
async def opnsense_add_dhcp_option(option: dict) -> str:
    """Add a DHCP option (dnsmasq/settings/add_option).

    Args:
        option: e.g. {"name":"150","value":"10.0.0.1"} (keys as in the
            search result).
    """
    return await _run(_post, "dnsmasq/settings/add_option",
                      body={"option": option})


@mcp.tool()
async def opnsense_set_dhcp_option(uuid: str, option: dict) -> str:
    """Update a DHCP option (dnsmasq/settings/set_option/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/set_option/{uuid}",
                      body={"option": option})


@mcp.tool()
async def opnsense_delete_dhcp_option(uuid: str) -> str:
    """Delete a DHCP option (dnsmasq/settings/del_option/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/del_option/{uuid}")


@mcp.tool()
async def opnsense_search_dhcp_boot(search: Optional[str] = None,
                                    current: int = 1,
                                    row_count: int = 100) -> str:
    """List DHCP boot entries (dnsmasq/settings/search_boot)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "dnsmasq/settings/search_boot", body=body)


@mcp.tool()
async def opnsense_add_dhcp_boot(boot: dict) -> str:
    """Add a DHCP boot entry (dnsmasq/settings/add_boot)."""
    return await _run(_post, "dnsmasq/settings/add_boot",
                      body={"boot": boot})


@mcp.tool()
async def opnsense_set_dhcp_boot(uuid: str, boot: dict) -> str:
    """Update a DHCP boot entry (dnsmasq/settings/set_boot/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/set_boot/{uuid}",
                      body={"boot": boot})


@mcp.tool()
async def opnsense_delete_dhcp_boot(uuid: str) -> str:
    """Delete a DHCP boot entry (dnsmasq/settings/del_boot/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/del_boot/{uuid}")


@mcp.tool()
async def opnsense_search_dhcp_tags(search: Optional[str] = None,
                                    current: int = 1,
                                    row_count: int = 100) -> str:
    """List DHCP tags (dnsmasq/settings/search_tag)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "dnsmasq/settings/search_tag", body=body)


@mcp.tool()
async def opnsense_add_dhcp_tag(tag: dict) -> str:
    """Add a DHCP tag (dnsmasq/settings/add_tag)."""
    return await _run(_post, "dnsmasq/settings/add_tag", body={"tag": tag})


@mcp.tool()
async def opnsense_set_dhcp_tag(uuid: str, tag: dict) -> str:
    """Update a DHCP tag (dnsmasq/settings/set_tag/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/set_tag/{uuid}",
                      body={"tag": tag})


@mcp.tool()
async def opnsense_delete_dhcp_tag(uuid: str) -> str:
    """Delete a DHCP tag (dnsmasq/settings/del_tag/<uuid>)."""
    return await _run(_post, f"dnsmasq/settings/del_tag/{uuid}")


@mcp.tool()
async def opnsense_get_dhcp_tag_list() -> str:
    """List tag names (dnsmasq/settings/get_tag_list)."""
    return await _run(_get, "dnsmasq/settings/get_tag_list")


# --------------------------------------------------------------------------
# unbound items: forwarders, host overrides, aliases, ACLs, blocklist
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_set_unbound_settings(unbound: dict) -> str:
    """Update the whole unbound model (unbound/settings/set).

    Args:
        unbound: the "unbound" object as returned by
            opnsense_get_unbound_settings.
    """
    return await _run(_post, "unbound/settings/set",
                      body={"unbound": unbound})


@mcp.tool()
async def opnsense_search_forwards(search: Optional[str] = None,
                                   current: int = 1,
                                   row_count: int = 100) -> str:
    """List DNS forwarders (unbound/settings/search_forward).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "unbound/settings/search_forward", body=body)


@mcp.tool()
async def opnsense_add_forward(fwd: dict) -> str:
    """Add a DNS forwarder (unbound/settings/add_forward).

    Args:
        fwd: e.g. {"forward_address":"1.1.1.1","forward_zone":"."} (keys as
            in opnsense_search_forwards).
    """
    return await _run(_post, "unbound/settings/add_forward",
                      body={"dot": fwd})


@mcp.tool()
async def opnsense_set_forward(uuid: str, fwd: dict) -> str:
    """Update a DNS forwarder (unbound/settings/set_forward/<uuid>)."""
    return await _run(_post, f"unbound/settings/set_forward/{uuid}",
                      body={"dot": fwd})


@mcp.tool()
async def opnsense_delete_forward(uuid: str) -> str:
    """Delete a DNS forwarder (unbound/settings/del_forward/<uuid>)."""
    return await _run(_post, f"unbound/settings/del_forward/{uuid}")


@mcp.tool()
async def opnsense_toggle_forward(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a DNS forwarder
    (unbound/settings/toggle_forward/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"unbound/settings/toggle_forward/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_search_host_overrides(search: Optional[str] = None,
                                         current: int = 1,
                                         row_count: int = 100) -> str:
    """List host overrides (unbound/settings/search_host_override)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "unbound/settings/search_host_override", body=body)


@mcp.tool()
async def opnsense_add_host_override(host: dict) -> str:
    """Add a host override (unbound/settings/add_host_override).

    Args:
        host: e.g. {"domain":"nas.lab.local","server":"192.168.1.50",
            "description":"nas"}.
    """
    return await _run(_post, "unbound/settings/add_host_override",
                      body={"host": host})


@mcp.tool()
async def opnsense_set_host_override(uuid: str, host: dict) -> str:
    """Update a host override (unbound/settings/set_host_override/<uuid>)."""
    return await _run(_post, f"unbound/settings/set_host_override/{uuid}",
                      body={"host": host})


@mcp.tool()
async def opnsense_delete_host_override(uuid: str) -> str:
    """Delete a host override
    (unbound/settings/del_host_override/<uuid>).
    """
    return await _run(_post, f"unbound/settings/del_host_override/{uuid}")


@mcp.tool()
async def opnsense_toggle_host_override(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a host override (unbound/settings/toggle_host_override).
    """
    return await _run(_post,
                      f"unbound/settings/toggle_host_override/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_search_host_aliases(search: Optional[str] = None,
                                       current: int = 1,
                                       row_count: int = 100) -> str:
    """List host aliases (unbound/settings/search_host_alias)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "unbound/settings/search_host_alias", body=body)


@mcp.tool()
async def opnsense_add_host_alias(alias: dict) -> str:
    """Add a host alias (unbound/settings/add_host_alias).

    Args:
        alias: e.g. {"name":"nas","target":"nas.lab.local"}.
    """
    return await _run(_post, "unbound/settings/add_host_alias",
                      body={"alias": alias})


@mcp.tool()
async def opnsense_set_host_alias(uuid: str, alias: dict) -> str:
    """Update a host alias (unbound/settings/set_host_alias/<uuid>)."""
    return await _run(_post, f"unbound/settings/set_host_alias/{uuid}",
                      body={"alias": alias})


@mcp.tool()
async def opnsense_delete_host_alias(uuid: str) -> str:
    """Delete a host alias (unbound/settings/del_host_alias/<uuid>)."""
    return await _run(_post, f"unbound/settings/del_host_alias/{uuid}")


@mcp.tool()
async def opnsense_toggle_host_alias(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a host alias (unbound/settings/toggle_host_alias)."""
    return await _run(_post,
                      f"unbound/settings/toggle_host_alias/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_search_acls(search: Optional[str] = None,
                               current: int = 1, row_count: int = 100) -> str:
    """List local ACLs (unbound/settings/search_acl)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "unbound/settings/search_acl", body=body)


@mcp.tool()
async def opnsense_add_acl(acl: dict) -> str:
    """Add a local ACL (unbound/settings/add_acl).

    Args:
        acl: e.g. {"acl":"192.168.1.0/24","action":"allow"}.
    """
    return await _run(_post, "unbound/settings/add_acl", body={"acl": acl})


@mcp.tool()
async def opnsense_set_acl(uuid: str, acl: dict) -> str:
    """Update a local ACL (unbound/settings/set_acl/<uuid>)."""
    return await _run(_post, f"unbound/settings/set_acl/{uuid}",
                      body={"acl": acl})


@mcp.tool()
async def opnsense_delete_acl(uuid: str) -> str:
    """Delete a local ACL (unbound/settings/del_acl/<uuid>)."""
    return await _run(_post, f"unbound/settings/del_acl/{uuid}")


@mcp.tool()
async def opnsense_toggle_acl(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a local ACL (unbound/settings/toggle_acl/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"unbound/settings/toggle_acl/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_search_blocklists(search: Optional[str] = None,
                                     current: int = 1,
                                     row_count: int = 100) -> str:
    """List DNS blocklist entries (unbound/settings/search_dnsbl)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "unbound/settings/search_dnsbl", body=body)


@mcp.tool()
async def opnsense_add_blocklist(blocklist: dict) -> str:
    """Add a DNS blocklist entry (unbound/settings/add_dnsbl).

    Args:
        blocklist: e.g. {"name":"ads","source":"https://.../list",
            "type":"block"} (keys as in opnsense_search_blocklists).
    """
    return await _run(_post, "unbound/settings/add_dnsbl",
                      body={"blocklist": blocklist})


@mcp.tool()
async def opnsense_set_blocklist(uuid: str, blocklist: dict) -> str:
    """Update a DNS blocklist entry (unbound/settings/set_dnsbl/<uuid>)."""
    return await _run(_post, f"unbound/settings/set_dnsbl/{uuid}",
                      body={"blocklist": blocklist})


@mcp.tool()
async def opnsense_delete_blocklist(uuid: str) -> str:
    """Delete a DNS blocklist entry (unbound/settings/del_dnsbl/<uuid>)."""
    return await _run(_post, f"unbound/settings/del_dnsbl/{uuid}")


@mcp.tool()
async def opnsense_toggle_blocklist(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a DNS blocklist entry
    (unbound/settings/toggle_dnsbl/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"unbound/settings/toggle_dnsbl/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_update_blocklist(uuid: str, domain: str,
                                    type: str = "blocklists") -> str:
    """Update a DNS blocklist entry: add/remove a single domain live
    (unbound/settings/update_blocklist).

    Args:
        uuid: blocklist entry uuid (from opnsense_search_blocklists).
        domain: domain to update, e.g. "ads.example.com".
        type: "blocklists" (default) or "allowlists".
    """
    return await _run(_post, "unbound/settings/update_blocklist",
                      body={"uuid": uuid, "domain": domain, "type": type})


@mcp.tool()
async def opnsense_unbound_stats() -> str:
    """Unbound statistics (unbound/diagnostics/stats)."""
    return await _run(_get, "unbound/diagnostics/stats")


# --------------------------------------------------------------------------
# syslog: destinations
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_get_syslog_config() -> str:
    """Syslog configuration (syslog/settings/get)."""
    return await _run(_get, "syslog/settings/get")


@mcp.tool()
async def opnsense_set_syslog_config(syslog: dict) -> str:
    """Update the whole syslog model (syslog/settings/set).

    Args:
        syslog: the "syslog" object as returned by
            opnsense_get_syslog_config.
    """
    return await _run(_post, "syslog/settings/set", body={"syslog": syslog})


@mcp.tool()
async def opnsense_search_syslog_destinations(search: Optional[str] = None,
                                              current: int = 1,
                                              row_count: int = 100) -> str:
    """List syslog destinations (syslog/settings/search_destinations)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "syslog/settings/search_destinations", body=body)


@mcp.tool()
async def opnsense_add_syslog_destination(destination: dict) -> str:
    """Add a syslog destination (syslog/settings/add_destination).

    Args:
        destination: e.g. {"hostname":"192.168.1.50","port":"514",
            "transport":"udp4","description":"syslog server"}; transport is
            one of udp4/tcp4/udp6/tcp6/tls4/tls6.
    """
    return await _run(_post, "syslog/settings/add_destination",
                      body={"destination": destination})


@mcp.tool()
async def opnsense_set_syslog_destination(uuid: str,
                                          destination: dict) -> str:
    """Update a syslog destination (syslog/settings/set_destination/<uuid>)."""
    return await _run(_post, f"syslog/settings/set_destination/{uuid}",
                      body={"destination": destination})


@mcp.tool()
async def opnsense_delete_syslog_destination(uuid: str) -> str:
    """Delete a syslog destination
    (syslog/settings/del_destination/<uuid>).
    """
    return await _run(_post, f"syslog/settings/del_destination/{uuid}")


@mcp.tool()
async def opnsense_toggle_syslog_destination(uuid: str,
                                             enabled: bool = True) -> str:
    """Enable/disable a syslog destination
    (syslog/settings/toggle_destination/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"syslog/settings/toggle_destination/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_reconfigure_syslog() -> str:
    """Apply staged syslog changes (syslog/service/reconfigure)."""
    return await _run(_post, "syslog/service/reconfigure")


@mcp.tool()
async def opnsense_get_syslog_status() -> str:
    """Syslog service status (syslog/service/status)."""
    return await _run(_get, "syslog/service/status")


# --------------------------------------------------------------------------
# trafficshaper: pipes, queues, rules (model key "ts")
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_get_shaper_config() -> str:
    """Traffic shaper configuration (trafficshaper/settings/get)."""
    return await _run(_get, "trafficshaper/settings/get")


@mcp.tool()
async def opnsense_set_shaper_config(ts: dict) -> str:
    """Update the whole traffic-shaper model (trafficshaper/settings/set).

    Args:
        ts: the "ts" object as returned by opnsense_get_shaper_config.
    """
    return await _run(_post, "trafficshaper/settings/set", body={"ts": ts})


@mcp.tool()
async def opnsense_search_pipes(search: Optional[str] = None,
                                current: int = 1, row_count: int = 100) -> str:
    """List shaper pipes (trafficshaper/settings/search_pipes)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "trafficshaper/settings/search_pipes", body=body)


@mcp.tool()
async def opnsense_add_pipe(pipe: dict) -> str:
    """Add a shaper pipe (trafficshaper/settings/add_pipe).

    Args:
        pipe: pipe dict as shown in opnsense_search_pipes (number,
            interface, direction, bandwidth, ...).
    """
    return await _run(_post, "trafficshaper/settings/add_pipe",
                      body={"pipe": pipe})


@mcp.tool()
async def opnsense_set_pipe(uuid: str, pipe: dict) -> str:
    """Update a shaper pipe (trafficshaper/settings/set_pipe/<uuid>)."""
    return await _run(_post, f"trafficshaper/settings/set_pipe/{uuid}",
                      body={"pipe": pipe})


@mcp.tool()
async def opnsense_delete_pipe(uuid: str) -> str:
    """Delete a shaper pipe (trafficshaper/settings/del_pipe/<uuid>)."""
    return await _run(_post, f"trafficshaper/settings/del_pipe/{uuid}")


@mcp.tool()
async def opnsense_toggle_pipe(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a shaper pipe
    (trafficshaper/settings/toggle_pipe/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"trafficshaper/settings/toggle_pipe/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_search_queues(search: Optional[str] = None,
                                 current: int = 1, row_count: int = 100) -> str:
    """List shaper queues (trafficshaper/settings/search_queues)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "trafficshaper/settings/search_queues", body=body)


@mcp.tool()
async def opnsense_add_queue(queue: dict) -> str:
    """Add a shaper queue (trafficshaper/settings/add_queue).

    Args:
        queue: queue dict as shown in opnsense_search_queues.
    """
    return await _run(_post, "trafficshaper/settings/add_queue",
                      body={"queue": queue})


@mcp.tool()
async def opnsense_set_queue(uuid: str, queue: dict) -> str:
    """Update a shaper queue (trafficshaper/settings/set_queue/<uuid>)."""
    return await _run(_post, f"trafficshaper/settings/set_queue/{uuid}",
                      body={"queue": queue})


@mcp.tool()
async def opnsense_delete_queue(uuid: str) -> str:
    """Delete a shaper queue (trafficshaper/settings/del_queue/<uuid>)."""
    return await _run(_post, f"trafficshaper/settings/del_queue/{uuid}")


@mcp.tool()
async def opnsense_toggle_queue(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a shaper queue
    (trafficshaper/settings/toggle_queue/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"trafficshaper/settings/toggle_queue/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_search_shaper_rules(search: Optional[str] = None,
                                       current: int = 1,
                                       row_count: int = 100) -> str:
    """List shaper rules (trafficshaper/settings/search_rules)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "trafficshaper/settings/search_rules", body=body)


@mcp.tool()
async def opnsense_add_shaper_rule(rule: dict) -> str:
    """Add a shaper rule (trafficshaper/settings/add_rule).

    Args:
        rule: rule dict as shown in opnsense_search_shaper_rules.
    """
    return await _run(_post, "trafficshaper/settings/add_rule",
                      body={"rule": rule})


@mcp.tool()
async def opnsense_set_shaper_rule(uuid: str, rule: dict) -> str:
    """Update a shaper rule (trafficshaper/settings/set_rule/<uuid>)."""
    return await _run(_post, f"trafficshaper/settings/set_rule/{uuid}",
                      body={"rule": rule})


@mcp.tool()
async def opnsense_delete_shaper_rule(uuid: str) -> str:
    """Delete a shaper rule (trafficshaper/settings/del_rule/<uuid>)."""
    return await _run(_post, f"trafficshaper/settings/del_rule/{uuid}")


@mcp.tool()
async def opnsense_toggle_shaper_rule(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a shaper rule
    (trafficshaper/settings/toggle_rule/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"trafficshaper/settings/toggle_rule/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_reconfigure_shaper() -> str:
    """Apply staged shaper changes (trafficshaper/service/reconfigure)."""
    return await _run(_post, "trafficshaper/service/reconfigure")


@mcp.tool()
async def opnsense_shaper_statistics() -> str:
    """Shaper statistics (trafficshaper/service/statistics)."""
    return await _run(_get, "trafficshaper/service/statistics")


# --------------------------------------------------------------------------
# monit: services, alerts, tests
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_get_monit_config() -> str:
    """Monit configuration (monit/settings/get)."""
    return await _run(_get, "monit/settings/get")


@mcp.tool()
async def opnsense_set_monit_config(monit: dict) -> str:
    """Update the whole monit model (monit/settings/set).

    Args:
        monit: the "monit" object as returned by opnsense_get_monit_config.
    """
    return await _run(_post, "monit/settings/set", body={"monit": monit})


@mcp.tool()
async def opnsense_get_monit_general() -> str:
    """Monit general settings (monit/settings/get_general)."""
    return await _run(_get, "monit/settings/get_general")


@mcp.tool()
async def opnsense_search_monit_services(search: Optional[str] = None,
                                         current: int = 1,
                                         row_count: int = 100) -> str:
    """List monit services (monit/settings/search_service)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "monit/settings/search_service", body=body)


@mcp.tool()
async def opnsense_add_monit_service(service: dict) -> str:
    """Add a monit service (monit/settings/add_service).

    Args:
        service: service dict as shown in opnsense_search_monit_services;
            "type" is required (process/file/fifo/filesystem/directory/host/
            system/custom/network), e.g. {"name":"myproc","type":"process",
            "path":"/usr/local/bin/myproc","description":"..."}.
    """
    return await _run(_post, "monit/settings/add_service",
                      body={"service": service})


@mcp.tool()
async def opnsense_set_monit_service(uuid: str, service: dict) -> str:
    """Update a monit service (monit/settings/set_service/<uuid>)."""
    return await _run(_post, f"monit/settings/set_service/{uuid}",
                      body={"service": service})


@mcp.tool()
async def opnsense_delete_monit_service(uuid: str) -> str:
    """Delete a monit service (monit/settings/del_service/<uuid>)."""
    return await _run(_post, f"monit/settings/del_service/{uuid}")


@mcp.tool()
async def opnsense_toggle_monit_service(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a monit service
    (monit/settings/toggle_service/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"monit/settings/toggle_service/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_search_monit_alerts(search: Optional[str] = None,
                                       current: int = 1,
                                       row_count: int = 100) -> str:
    """List monit alerts (monit/settings/search_alert)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "monit/settings/search_alert", body=body)


@mcp.tool()
async def opnsense_add_monit_alert(alert: dict) -> str:
    """Add a monit alert (monit/settings/add_alert).

    Args:
        alert: alert dict as shown in opnsense_search_monit_alerts.
    """
    return await _run(_post, "monit/settings/add_alert", body={"alert": alert})


@mcp.tool()
async def opnsense_set_monit_alert(uuid: str, alert: dict) -> str:
    """Update a monit alert (monit/settings/set_alert/<uuid>)."""
    return await _run(_post, f"monit/settings/set_alert/{uuid}",
                      body={"alert": alert})


@mcp.tool()
async def opnsense_delete_monit_alert(uuid: str) -> str:
    """Delete a monit alert (monit/settings/del_alert/<uuid>)."""
    return await _run(_post, f"monit/settings/del_alert/{uuid}")


@mcp.tool()
async def opnsense_toggle_monit_alert(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a monit alert (monit/settings/toggle_alert/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"monit/settings/toggle_alert/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_search_monit_tests(search: Optional[str] = None,
                                      current: int = 1,
                                      row_count: int = 100) -> str:
    """List monit tests (monit/settings/search_test)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "monit/settings/search_test", body=body)


@mcp.tool()
async def opnsense_add_monit_test(test: dict) -> str:
    """Add a monit test (monit/settings/add_test).

    Args:
        test: test dict as shown in opnsense_search_monit_tests.
    """
    return await _run(_post, "monit/settings/add_test", body={"test": test})


@mcp.tool()
async def opnsense_set_monit_test(uuid: str, test: dict) -> str:
    """Update a monit test (monit/settings/set_test/<uuid>)."""
    return await _run(_post, f"monit/settings/set_test/{uuid}",
                      body={"test": test})


@mcp.tool()
async def opnsense_delete_monit_test(uuid: str) -> str:
    """Delete a monit test (monit/settings/del_test/<uuid>)."""
    return await _run(_post, f"monit/settings/del_test/{uuid}")


@mcp.tool()
async def opnsense_monit_status() -> str:
    """Monit status (monit/status/get)."""
    return await _run(_get, "monit/status/get")


@mcp.tool()
async def opnsense_reconfigure_monit() -> str:
    """Apply staged monit changes (monit/service/reconfigure)."""
    return await _run(_post, "monit/service/reconfigure")


# --------------------------------------------------------------------------
# trust: CAs, certificates, CRLs
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_search_ca(search: Optional[str] = None,
                             current: int = 1, row_count: int = 100) -> str:
    """List CAs (trust/ca/search).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "trust/ca/search", body=body)


@mcp.tool()
async def opnsense_ca_list() -> str:
    """CA reference list (trust/ca/ca_list)."""
    return await _run(_get, "trust/ca/ca_list")


@mcp.tool()
async def opnsense_add_ca(ca: dict) -> str:
    """Add a CA (trust/ca/add).

    Args:
        ca: CA dict as shown in opnsense_search_ca (csr/cert/ckey, type
            "user" or "acme" fields).
    """
    return await _run(_post, "trust/ca/add", body={"ca": ca})


@mcp.tool()
async def opnsense_delete_ca(uuid: str) -> str:
    """Delete a CA (trust/ca/del/<uuid>). Fails if certificates reference it.
    """
    return await _run(_post, f"trust/ca/del/{uuid}")


@mcp.tool()
async def opnsense_ca_raw_dump(uuid: str) -> str:
    """Raw PEM of a CA (trust/ca/raw_dump/<uuid>)."""
    return await _run(_get, f"trust/ca/raw_dump/{uuid}")


@mcp.tool()
async def opnsense_search_cert(search: Optional[str] = None,
                               current: int = 1, row_count: int = 100) -> str:
    """List certificates (trust/cert/search).

    Args:
        search: optional search phrase.
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "trust/cert/search", body=body)


@mcp.tool()
async def opnsense_add_cert(cert: dict) -> str:
    """Add a certificate (trust/cert/add).

    Args:
        cert: certificate dict as shown in opnsense_search_cert (csr,
            caref, certref, ckey, ...).
    """
    return await _run(_post, "trust/cert/add", body={"cert": cert})


@mcp.tool()
async def opnsense_delete_cert(uuid: str) -> str:
    """Delete a certificate (trust/cert/del/<uuid>). Fails if in use.
    """
    return await _run(_post, f"trust/cert/del/{uuid}")


@mcp.tool()
async def opnsense_cert_raw_dump(uuid: str) -> str:
    """Raw PEM of a certificate (trust/cert/raw_dump/<uuid>)."""
    return await _run(_get, f"trust/cert/raw_dump/{uuid}")


@mcp.tool()
async def opnsense_search_crl(search: Optional[str] = None,
                              current: int = 1, row_count: int = 100) -> str:
    """List CRLs (trust/crl/search)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "trust/crl/search", body=body)


@mcp.tool()
async def opnsense_get_trust_config() -> str:
    """Trust general settings (trust/settings/get)."""
    return await _run(_get, "trust/settings/get")


@mcp.tool()
async def opnsense_reconfigure_trust() -> str:
    """Apply staged trust changes (trust/settings/reconfigure)."""
    return await _run(_post, "trust/settings/reconfigure")


# --------------------------------------------------------------------------
# wireguard: general, servers, clients
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_get_wireguard_general() -> str:
    """WireGuard general settings (wireguard/general/get)."""
    return await _run(_get, "wireguard/general/get")


@mcp.tool()
async def opnsense_set_wireguard_general(general: dict) -> str:
    """Update WireGuard general settings (wireguard/general/set).

    Args:
        general: the "general" object as returned by
            opnsense_get_wireguard_general.
    """
    return await _run(_post, "wireguard/general/set",
                      body={"general": general})


@mcp.tool()
async def opnsense_search_wireguard_servers(search: Optional[str] = None,
                                            current: int = 1,
                                            row_count: int = 100) -> str:
    """List WireGuard servers (wireguard/server/search_server)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "wireguard/server/search_server", body=body)


@mcp.tool()
async def opnsense_add_wireguard_server(server: dict) -> str:
    """Add a WireGuard server (wireguard/server/add_server).

    Args:
        server: server dict as shown in
            opnsense_search_wireguard_servers (description, interface,
            port, key, dns, ...).
    """
    return await _run(_post, "wireguard/server/add_server",
                      body={"server": server})


@mcp.tool()
async def opnsense_set_wireguard_server(uuid: str, server: dict) -> str:
    """Update a WireGuard server
    (wireguard/server/set_server/<uuid>).
    """
    return await _run(_post, f"wireguard/server/set_server/{uuid}",
                      body={"server": server})


@mcp.tool()
async def opnsense_delete_wireguard_server(uuid: str) -> str:
    """Delete a WireGuard server (wireguard/server/del_server/<uuid>)."""
    return await _run(_post, f"wireguard/server/del_server/{uuid}")


@mcp.tool()
async def opnsense_toggle_wireguard_server(uuid: str, enabled: bool = True) -> str:
    """Enable/disable a WireGuard server
    (wireguard/server/toggle_server/<uuid>).
    """
    return await _run(_post, f"wireguard/server/toggle_server/{uuid}")


@mcp.tool()
async def opnsense_wireguard_key_pair() -> str:
    """Generate a WireGuard key pair (wireguard/server/key_pair)."""
    return await _run(_get, "wireguard/server/key_pair")


@mcp.tool()
async def opnsense_search_wireguard_clients(search: Optional[str] = None,
                                            current: int = 1,
                                            row_count: int = 100) -> str:
    """List WireGuard clients (wireguard/client/search_client)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "wireguard/client/search_client", body=body)


@mcp.tool()
async def opnsense_add_wireguard_client(client: dict) -> str:
    """Add a WireGuard client (wireguard/client/set_client with no uuid, via
    wireguard/client/set).

    Args:
        client: client dict as shown in opnsense_search_wireguard_clients.
    """
    return await _run(_post, "wireguard/client/set", body={"client": client})


@mcp.tool()
async def opnsense_set_wireguard_client(uuid: str, client: dict) -> str:
    """Update a WireGuard client (wireguard/client/set_client/<uuid>)."""
    return await _run(_post, f"wireguard/client/set_client/{uuid}",
                      body={"client": client})


@mcp.tool()
async def opnsense_delete_wireguard_client(uuid: str) -> str:
    """Delete a WireGuard client (wireguard/client/del_client/<uuid>)."""
    return await _run(_post, f"wireguard/client/del_client/{uuid}")


@mcp.tool()
async def opnsense_toggle_wireguard_client(uuid: str) -> str:
    """Toggle a WireGuard client (wireguard/client/toggle_client/<uuid>)."""
    return await _run(_post, f"wireguard/client/toggle_client/{uuid}")


@mcp.tool()
async def opnsense_wireguard_client_list_servers() -> str:
    """Servers available to clients (wireguard/client/list_servers)."""
    return await _run(_get, "wireguard/client/list_servers")


@mcp.tool()
async def opnsense_wireguard_client_psk() -> str:
    """Generate a WireGuard preshared key (wireguard/client/psk)."""
    return await _run(_get, "wireguard/client/psk")


@mcp.tool()
async def opnsense_wireguard_status() -> str:
    """WireGuard service status (wireguard/service/status)."""
    return await _run(_get, "wireguard/service/status")


@mcp.tool()
async def opnsense_reconfigure_wireguard() -> str:
    """Apply staged WireGuard changes (wireguard/service/reconfigure)."""
    return await _run(_post, "wireguard/service/reconfigure")


# --------------------------------------------------------------------------
# ipsec: connections, pools, key pairs, pre-shared keys, sessions, service
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_get_ipsec_config() -> str:
    """IPsec general settings (ipsec/settings/get)."""
    return await _run(_get, "ipsec/settings/get")


@mcp.tool()
async def opnsense_set_ipsec_config(ipsec: dict) -> str:
    """Update IPsec general settings (ipsec/settings/set).

    Args:
        ipsec: the "ipsec" object as returned by opnsense_get_ipsec_config.
    """
    return await _run(_post, "ipsec/settings/set", body={"ipsec": ipsec})


@mcp.tool()
async def opnsense_search_ipsec_connections(search: Optional[str] = None,
                                            current: int = 1,
                                            row_count: int = 100) -> str:
    """List IPsec connections (ipsec/connections/search_connection)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "ipsec/connections/search_connection", body=body)


@mcp.tool()
async def opnsense_add_ipsec_connection(connection: dict) -> str:
    """Add an IPsec connection (ipsec/connections/add_connection).

    Args:
        connection: connection dict as shown in
            opnsense_search_ipsec_connections.
    """
    return await _run(_post, "ipsec/connections/add_connection",
                      body={"connection": connection})


@mcp.tool()
async def opnsense_set_ipsec_connection(uuid: str, connection: dict) -> str:
    """Update an IPsec connection
    (ipsec/connections/set_connection/<uuid>).
    """
    return await _run(_post, f"ipsec/connections/set_connection/{uuid}",
                      body={"connection": connection})


@mcp.tool()
async def opnsense_delete_ipsec_connection(uuid: str) -> str:
    """Delete an IPsec connection
    (ipsec/connections/del_connection/<uuid>).
    """
    return await _run(_post, f"ipsec/connections/del_connection/{uuid}")


@mcp.tool()
async def opnsense_toggle_ipsec_connection(uuid: str, enabled: bool = True) -> str:
    """Enable/disable an IPsec connection
    (ipsec/connections/toggle_connection/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"ipsec/connections/toggle_connection/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_ipsec_is_enabled() -> str:
    """Whether IPsec is enabled (ipsec/connections/is_enabled)."""
    return await _run(_get, "ipsec/connections/is_enabled")


@mcp.tool()
async def opnsense_ipsec_swanctl() -> str:
    """Rendered swanctl.conf (ipsec/connections/swanctl)."""
    return await _run(_get, "ipsec/connections/swanctl")


@mcp.tool()
async def opnsense_search_ipsec_pools(search: Optional[str] = None,
                                      current: int = 1,
                                      row_count: int = 100) -> str:
    """List IPsec pools (ipsec/pools/search)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "ipsec/pools/search", body=body)


@mcp.tool()
async def opnsense_add_ipsec_pool(pool: dict) -> str:
    """Add an IPsec pool (ipsec/pools/add).

    Args:
        pool: pool dict as shown in opnsense_search_ipsec_pools.
    """
    return await _run(_post, "ipsec/pools/add", body={"pool": pool})


@mcp.tool()
async def opnsense_set_ipsec_pool(uuid: str, pool: dict) -> str:
    """Update an IPsec pool (ipsec/pools/set/<uuid>)."""
    return await _run(_post, f"ipsec/pools/set/{uuid}", body={"pool": pool})


@mcp.tool()
async def opnsense_delete_ipsec_pool(uuid: str) -> str:
    """Delete an IPsec pool (ipsec/pools/del/<uuid>)."""
    return await _run(_post, f"ipsec/pools/del/{uuid}")


@mcp.tool()
async def opnsense_search_ipsec_key_pairs(search: Optional[str] = None,
                                          current: int = 1,
                                          row_count: int = 100) -> str:
    """List IPsec key pairs (ipsec/key_pairs/search_item)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "ipsec/key_pairs/search_item", body=body)


@mcp.tool()
async def opnsense_add_ipsec_key_pair(item: dict) -> str:
    """Add an IPsec key pair (ipsec/key_pairs/add_item).

    Args:
        item: key-pair dict as shown in opnsense_search_ipsec_key_pairs.
    """
    return await _run(_post, "ipsec/key_pairs/add_item", body={"item": item})


@mcp.tool()
async def opnsense_set_ipsec_key_pair(uuid: str, item: dict) -> str:
    """Update an IPsec key pair (ipsec/key_pairs/set_item/<uuid>)."""
    return await _run(_post, f"ipsec/key_pairs/set_item/{uuid}",
                      body={"item": item})


@mcp.tool()
async def opnsense_delete_ipsec_key_pair(uuid: str) -> str:
    """Delete an IPsec key pair (ipsec/key_pairs/del_item/<uuid>)."""
    return await _run(_post, f"ipsec/key_pairs/del_item/{uuid}")


@mcp.tool()
async def opnsense_gen_ipsec_key_pair(key_type: str, size: str = "") -> str:
    """Generate an IPsec key pair (ipsec/key_pairs/gen_key_pair/<type>/<size>).

    Args:
        key_type: e.g. "ecp256", "ecp384", "rsa2048".
        size: optional size for RSA keys.
    """
    path = f"ipsec/key_pairs/gen_key_pair/{key_type}"
    if size:
        path += f"/{size}"
    return await _run(_get, path)


@mcp.tool()
async def opnsense_search_ipsec_psk(search: Optional[str] = None,
                                    current: int = 1,
                                    row_count: int = 100) -> str:
    """List IPsec pre-shared keys (ipsec/pre_shared_keys/search_item)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "ipsec/pre_shared_keys/search_item", body=body)


@mcp.tool()
async def opnsense_add_ipsec_psk(item: dict) -> str:
    """Add an IPsec pre-shared key (ipsec/pre_shared_keys/add_item).

    Args:
        item: PSK dict as shown in opnsense_search_ipsec_psk.
    """
    return await _run(_post, "ipsec/pre_shared_keys/add_item",
                      body={"item": item})


@mcp.tool()
async def opnsense_set_ipsec_psk(uuid: str, item: dict) -> str:
    """Update an IPsec pre-shared key
    (ipsec/pre_shared_keys/set_item/<uuid>).
    """
    return await _run(_post, f"ipsec/pre_shared_keys/set_item/{uuid}",
                      body={"item": item})


@mcp.tool()
async def opnsense_delete_ipsec_psk(uuid: str) -> str:
    """Delete an IPsec pre-shared key
    (ipsec/pre_shared_keys/del_item/<uuid>).
    """
    return await _run(_post, f"ipsec/pre_shared_keys/del_item/{uuid}")


@mcp.tool()
async def opnsense_search_ipsec_leases() -> str:
    """IPsec pool lease assignments (ipsec/leases/search)."""
    return await _run(_get, "ipsec/leases/search")


@mcp.tool()
async def opnsense_search_ipsec_phase1() -> str:
    """Active IKE phase-1 sessions (ipsec/sessions/search_phase1)."""
    return await _run(_get, "ipsec/sessions/search_phase1")


@mcp.tool()
async def opnsense_search_ipsec_phase2() -> str:
    """Active phase-2 child SAs (ipsec/sessions/search_phase2)."""
    return await _run(_get, "ipsec/sessions/search_phase2")


@mcp.tool()
async def opnsense_ipsec_status() -> str:
    """IPsec service status (ipsec/service/status)."""
    return await _run(_get, "ipsec/service/status")


@mcp.tool()
async def opnsense_reconfigure_ipsec() -> str:
    """Apply staged IPsec changes (ipsec/service/reconfigure)."""
    return await _run(_post, "ipsec/service/reconfigure")


# --------------------------------------------------------------------------
# openvpn: instances, static keys, sessions
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_search_openvpn_instances(search: Optional[str] = None,
                                            current: int = 1,
                                            row_count: int = 100) -> str:
    """List OpenVPN instances (openvpn/instances/search)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "openvpn/instances/search", body=body)


@mcp.tool()
async def opnsense_get_openvpn_instance(uuid: str) -> str:
    """Get one OpenVPN instance (openvpn/instances/get/<uuid>)."""
    return await _run(_get, f"openvpn/instances/get/{uuid}")


@mcp.tool()
async def opnsense_add_openvpn_instance(instance: dict) -> str:
    """Add an OpenVPN instance (openvpn/instances/add).

    Args:
        instance: instance dict as shown in
            opnsense_search_openvpn_instances.
    """
    return await _run(_post, "openvpn/instances/add",
                      body={"instance": instance})


@mcp.tool()
async def opnsense_set_openvpn_instance(uuid: str, instance: dict) -> str:
    """Update an OpenVPN instance (openvpn/instances/set/<uuid>)."""
    return await _run(_post, f"openvpn/instances/set/{uuid}",
                      body={"instance": instance})


@mcp.tool()
async def opnsense_delete_openvpn_instance(uuid: str) -> str:
    """Delete an OpenVPN instance (openvpn/instances/del/<uuid>)."""
    return await _run(_post, f"openvpn/instances/del/{uuid}")


@mcp.tool()
async def opnsense_toggle_openvpn_instance(uuid: str, enabled: bool = True) -> str:
    """Enable/disable an OpenVPN instance
    (openvpn/instances/toggle/<uuid>/<0|1>).
    """
    return await _run(_post,
                      f"openvpn/instances/toggle/{uuid}/{'1' if enabled else '0'}")


@mcp.tool()
async def opnsense_search_openvpn_static_keys(search: Optional[str] = None,
                                              current: int = 1,
                                              row_count: int = 100) -> str:
    """List OpenVPN static keys (openvpn/instances/search_static_key)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "openvpn/instances/search_static_key", body=body)


@mcp.tool()
async def opnsense_add_openvpn_static_key(key: dict) -> str:
    """Add an OpenVPN static key (openvpn/instances/add_static_key).

    Args:
        key: static-key dict as shown in
            opnsense_search_openvpn_static_keys.
    """
    return await _run(_post, "openvpn/instances/add_static_key",
                      body={"statickey": key})


@mcp.tool()
async def opnsense_delete_openvpn_static_key(uuid: str) -> str:
    """Delete an OpenVPN static key
    (openvpn/instances/del_static_key/<uuid>).
    """
    return await _run(_post, f"openvpn/instances/del_static_key/{uuid}")


@mcp.tool()
async def opnsense_search_openvpn_sessions() -> str:
    """Active OpenVPN sessions (openvpn/service/search_sessions)."""
    return await _run(_get, "openvpn/service/search_sessions")


@mcp.tool()
async def opnsense_openvpn_reconfigure() -> str:
    """Apply staged OpenVPN changes (openvpn/service/reconfigure)."""
    return await _run(_post, "openvpn/service/reconfigure")


# --------------------------------------------------------------------------
# ids: settings, rulesets, user rules, policies, service
# --------------------------------------------------------------------------
@mcp.tool()
async def opnsense_get_ids_config() -> str:
    """Suricata/IDS configuration (ids/settings/get)."""
    return await _run(_get, "ids/settings/get")


@mcp.tool()
async def opnsense_set_ids_config(ids: dict) -> str:
    """Update the whole IDS model (ids/settings/set).

    Args:
        ids: the "ids" object as returned by opnsense_get_ids_config.
    """
    return await _run(_post, "ids/settings/set", body={"ids": ids})


@mcp.tool()
async def opnsense_ids_list_rulesets() -> str:
    """Available rulesets (ids/settings/list_rulesets)."""
    return await _run(_get, "ids/settings/list_rulesets")


@mcp.tool()
async def opnsense_ids_get_ruleset_properties() -> str:
    """Ruleset properties (ids/settings/get_rulesetproperties)."""
    return await _run(_get, "ids/settings/get_rulesetproperties")


@mcp.tool()
async def opnsense_ids_search_installed_rules(search: Optional[str] = None,
                                              current: int = 1,
                                              row_count: int = 100) -> str:
    """Search installed IDS rules (ids/settings/search_installed_rules).

    Args:
        search: optional search phrase (SID, etc.).
        current: page number.
        row_count: rows per page.
    """
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "ids/settings/search_installed_rules", body=body)


@mcp.tool()
async def opnsense_ids_search_user_rules(search: Optional[str] = None,
                                         current: int = 1,
                                         row_count: int = 100) -> str:
    """List user-defined IDS rules (ids/settings/search_user_rule)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "ids/settings/search_user_rule", body=body)


@mcp.tool()
async def opnsense_ids_add_user_rule(rule: dict) -> str:
    """Add a user-defined IDS rule (ids/settings/add_user_rule).

    Args:
        rule: rule dict as shown in opnsense_ids_search_user_rules.
    """
    return await _run(_post, "ids/settings/add_user_rule",
                      body={"rule": rule})


@mcp.tool()
async def opnsense_ids_set_user_rule(uuid: str, rule: dict) -> str:
    """Update a user-defined IDS rule (ids/settings/set_user_rule/<uuid>)."""
    return await _run(_post, f"ids/settings/set_user_rule/{uuid}",
                      body={"rule": rule})


@mcp.tool()
async def opnsense_ids_delete_user_rule(uuid: str) -> str:
    """Delete a user-defined IDS rule
    (ids/settings/del_user_rule/<uuid>).
    """
    return await _run(_post, f"ids/settings/del_user_rule/{uuid}")


@mcp.tool()
async def opnsense_ids_search_policies(search: Optional[str] = None,
                                       current: int = 1,
                                       row_count: int = 100) -> str:
    """List IDS policies (ids/settings/search_policy)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "ids/settings/search_policy", body=body)


@mcp.tool()
async def opnsense_ids_search_policy_rules(search: Optional[str] = None,
                                           current: int = 1,
                                           row_count: int = 100) -> str:
    """List IDS policy rules (ids/settings/search_policy_rule)."""
    body = {"current": current, "rowCount": row_count}
    if search:
        body["searchPhrase"] = search
    return await _run(_post, "ids/settings/search_policy_rule", body=body)


@mcp.tool()
async def opnsense_ids_get_alert_logs() -> str:
    """IDS alert log (ids/service/get_alert_logs)."""
    return await _run(_get, "ids/service/get_alert_logs")


@mcp.tool()
async def opnsense_ids_update_rules(wait: bool = False) -> str:
    """Update IDS rulesets (ids/service/update_rules/<0|1>).

    Args:
        wait: True to wait for the update to finish.
    """
    return await _run(_post, f"ids/service/update_rules/{1 if wait else 0}")


@mcp.tool()
async def opnsense_ids_status() -> str:
    """IDS service status (ids/service/status)."""
    return await _run(_get, "ids/service/status")


@mcp.tool()
async def opnsense_reconfigure_ids() -> str:
    """Apply staged IDS changes (ids/service/reconfigure)."""
    return await _run(_post, "ids/service/reconfigure")


if __name__ == "__main__":
    mcp.run()
