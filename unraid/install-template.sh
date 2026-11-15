#!/bin/bash
# Installiert oder aktualisiert die Unraid-Benutzervorlage.
#
# Auf dem Unraid-Terminal (Vorlage von GitHub holen):
#   curl -fsSL https://raw.githubusercontent.com/PaulG67/music-play/main/unraid/install-template.sh | bash
#
# Oder, wenn das Repo bereits unter appdata liegt:
#   bash /mnt/user/appdata/music-play/unraid/install-template.sh

set -euo pipefail

TEMPLATE_DIR="/boot/config/plugins/dockerMan/templates-user"
USER_XML="${TEMPLATE_DIR}/my-music-play.xml"
IMAGE="ghcr.io/paulg67/music-play:latest"
REMOTE_XML="https://raw.githubusercontent.com/PaulG67/music-play/main/unraid/music-play.xml"
SCRIPT_DIR=""
SOURCE_XML=""
TMP_XML=""
set +u
SELF="${BASH_SOURCE[0]}"
set -u

cleanup() {
  if [[ -n "${TMP_XML}" && -f "${TMP_XML}" ]]; then
    rm -f "${TMP_XML}"
  fi
}
trap cleanup EXIT

if [[ -n "${SELF}" && "${SELF}" != "-" && "${SELF}" != "bash" && -f "${SELF}" ]]; then
  SCRIPT_DIR="$(cd "$(dirname "${SELF}")" && pwd)"
  if [[ -f "${SCRIPT_DIR}/music-play.xml" ]]; then
    SOURCE_XML="${SCRIPT_DIR}/music-play.xml"
  fi
fi

if [[ -z "${SOURCE_XML}" || ! -f "${SOURCE_XML}" ]]; then
  TMP_XML="$(mktemp /tmp/music-play-xml.XXXXXX)"
  echo "==> Vorlage von GitHub laden"
  curl -fsSL "${REMOTE_XML}" -o "${TMP_XML}"
  SOURCE_XML="${TMP_XML}"
fi

echo "==> 1/3 Unraid-Vorlage"
mkdir -p "${TEMPLATE_DIR}"

if [[ ! -f "${USER_XML}" ]]; then
  cp "${SOURCE_XML}" "${USER_XML}"
  echo "    neu angelegt: ${USER_XML}"
else
  # Bestehende Container-XML behalten, fehlende Musik-Felder nachziehen.
  if grep -q 'Name="Musikbibliothek"' "${USER_XML}"; then
    sed -i 's/Name="Musikbibliothek"/Name="Musik-Pfad"/' "${USER_XML}"
    echo "    Feld umbenannt: Musik-Pfad"
  fi
  if ! grep -q 'Target="/music"' "${USER_XML}"; then
    sed -i 's|</Container>|  <Config Name="Musik-Pfad" Target="/music" Default="/mnt/user/music" Mode="rw" Description="Unraid-Pfad der Musikbibliothek – denselben Share wie Navidrome wählen." Type="Path" Display="always" Required="false" Mask="false">/mnt/user/music</Config>\n</Container>|' "${USER_XML}"
    echo "    Feld ergänzt: Musik-Pfad (Mount)"
  fi
  if ! grep -q 'Target="NAVIDROME_MUSIC_HOST"' "${USER_XML}"; then
    sed -i 's|</Container>|  <Config Name="Musik-Pfad Variable" Target="NAVIDROME_MUSIC_HOST" Default="/mnt/user/music" Mode="" Description="Derselbe Unraid-Pfad wie unter Musik-Pfad, damit die App ihn anzeigen kann." Type="Variable" Display="always" Required="false" Mask="false">/mnt/user/music</Config>\n</Container>|' "${USER_XML}"
    echo "    Feld ergänzt: Musik-Pfad Variable"
  else
    sed -i 's/Name="Unraid-Musikpfad (Anzeige)"/Name="Musik-Pfad Variable"/' "${USER_XML}" || true
    sed -i '/Target="NAVIDROME_MUSIC_HOST"/s/Display="advanced"/Display="always"/' "${USER_XML}" || true
    echo "    Feld sichtbar: Musik-Pfad Variable"
  fi
  echo "    aktualisiert: ${USER_XML}"
fi

rm -f "${TEMPLATE_DIR}/my-simpmusic-app.xml"

echo "==> 2/3 Docker-Image"
if docker pull "${IMAGE}" 2>/dev/null; then
  echo "    ${IMAGE}"
else
  echo "    GHCR-Pull nicht möglich."
  if [[ -n "${SCRIPT_DIR}" && -f "${SCRIPT_DIR}/../Dockerfile" ]]; then
    ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
    docker build -t "${IMAGE}" "${ROOT_DIR}"
    echo "    lokal gebaut und als ${IMAGE} getaggt"
  else
    echo "    Später: Docker → music-play → Force Update"
  fi
fi

echo "==> 3/3 Fertig"
echo
echo "In Unraid den bestehenden Container übernehmen:"
echo "  Docker → music-play → Edit"
echo "  „Musik-Pfad“ und „Musik-Pfad Variable“ auf denselben Share setzen"
echo "  (Standard /mnt/user/music) → Apply"
echo
echo "Oder neu anlegen: Docker → Container hinzufügen → Template «music-play»"
echo "WebUI: http://UNRAID-IP:5080"
