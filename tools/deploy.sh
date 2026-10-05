#!/usr/bin/env bash
# Deploy het package naar de productie-HA-server.
#
# Gebruik:
#   HA_SSH=willem@192.168.40.11 HA_CONFIG=/pad/naar/config ./tools/deploy.sh
#
# HA_SSH      = ssh-target van de server die de HA-container draait
# HA_CONFIG   = pad naar de HA config-dir op die server (de dir met
#               configuration.yaml). Default: /opt/homeassistant/config
set -euo pipefail

HA_SSH="${HA_SSH:-willem@192.168.40.11}"
HA_CONFIG="${HA_CONFIG:-/opt/homeassistant/config}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

scp "$REPO/packages/jacuzzi.yaml" "$HA_SSH:$HA_CONFIG/packages/jacuzzi.yaml"
echo "Gedeployed. Vergeet niet te checken dat configuration.yaml bevat:"
echo "  homeassistant:"
echo "    packages: !include_dir_named packages"
echo "Reload via Developer tools -> YAML -> 'Automations' + 'Input booleans'"
echo "(of docker restart van de HA-container)."
