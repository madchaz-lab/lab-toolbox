#!/bin/sh
# Example launch wrapper for one OPNsense firewall. Copy this file per box
# (e.g. run-opnsense-roshi.sh), edit the two paths, and point an opencode.json
# mcp entry at it. The key/secret are read from a PRIVATE file so they never
# appear in opencode.json or the repo.
set -eu

# 1) The private file holding "key=..." and "secret=..." for this firewall.
SECRETS_FILE=/path/to/private/opnsense-credentials.txt

# 2) This firewall's address + scheme.
export OPNSENSE_HOST=192.168.1.1
export OPNSENSE_SCHEME=http            # or https

# Read the credential lines and export them as the server's env.
export OPNSENSE_KEY=$(sed -n 's/^key=//p' "$SECRETS_FILE")
export OPNSENSE_SECRET=$(sed -n 's/^secret=//p' "$SECRETS_FILE")

# Launch the MCP server (stdio).
exec /path/to/opnsense-mcp/.venv/bin/python /path/to/opnsense-mcp/server.py
