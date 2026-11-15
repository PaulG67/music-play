#!/bin/bash
# Holt die aktuelle Unraid-Vorlage von GitHub.
# Auf Unraid (drei Zeilen, kein curl|bash nötig):
#
#   docker stop music-play; docker rm music-play
#   mkdir -p /boot/config/plugins/dockerMan/templates-user
#   wget -O /boot/config/plugins/dockerMan/templates-user/my-music-play.xml \
#     https://raw.githubusercontent.com/PaulG67/music-play/main/unraid/music-play.xml
#
# Danach: Docker → Container hinzufügen → Template music-play → Apply
# Appdata (/mnt/user/appdata/music-play) nicht löschen.

set -euo pipefail

TEMPLATE_DIR="/boot/config/plugins/dockerMan/templates-user"
USER_XML="${TEMPLATE_DIR}/my-music-play.xml"
REMOTE_XML="https://raw.githubusercontent.com/PaulG67/music-play/main/unraid/music-play.xml"

mkdir -p "${TEMPLATE_DIR}"
echo "==> Vorlage von GitHub"
wget -O "${USER_XML}" "${REMOTE_XML}"
rm -f "${TEMPLATE_DIR}/my-simpmusic-app.xml"
echo "    ${USER_XML}"

if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx 'music-play'; then
  echo "==> Container entfernen (Appdata bleibt)"
  docker stop music-play >/dev/null 2>&1 || true
  docker rm music-play >/dev/null 2>&1 || true
  echo "    music-play entfernt"
fi

echo
echo "Fertig. In Unraid:"
echo "  Docker → Container hinzufügen → Template «music-play»"
echo "  Musik-Pfad und Musik-Pfad Variable prüfen → Apply"
echo "  Config-Pfad muss /mnt/user/appdata/music-play/config bleiben"
