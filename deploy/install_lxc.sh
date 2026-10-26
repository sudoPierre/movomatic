#!/usr/bin/env bash
# =============================================================================
# Movomatic — installation dans un conteneur LXC Debian/Ubuntu (Proxmox VE)
#
#   sudo ./deploy/install_lxc.sh            # installation / réinstallation
#   sudo ./deploy/install_lxc.sh --update   # met à jour le code + yt-dlp, redémarre
#   sudo ./deploy/install_lxc.sh --uninstall
#
# Variables optionnelles (installation non interactive) :
#   GEMINI_API_KEY, JELLYSEERR_URL, JELLYSEERR_API_KEY, MOVOMATIC_PORT
# =============================================================================
set -euo pipefail

APP_NAME="movomatic"
APP_USER="movomatic"
INSTALL_DIR="/opt/movomatic"
CONFIG_DIR="/etc/movomatic"
ENV_FILE="${CONFIG_DIR}/movomatic.env"
SERVICE_FILE="/etc/systemd/system/movomatic.service"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MIN_PY_MINOR=11

c_info()  { printf '\033[1;34m[i]\033[0m %s\n' "$*"; }
c_ok()    { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
c_warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
c_err()   { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; }
die()     { c_err "$*"; exit 1; }

require_root() {
    [[ ${EUID} -eq 0 ]] || die "Ce script doit être exécuté en root (sudo)."
}

check_os() {
    [[ -r /etc/os-release ]] || die "/etc/os-release introuvable."
    # shellcheck disable=SC1091
    . /etc/os-release
    case "${ID:-} ${ID_LIKE:-}" in
        *debian*|*ubuntu*) c_ok "Système détecté : ${PRETTY_NAME:-$ID}" ;;
        *) die "Système non supporté (${ID:-inconnu}). Debian ou Ubuntu requis." ;;
    esac
    command -v systemctl >/dev/null || die "systemd est requis."
}

install_packages() {
    c_info "Installation des paquets système…"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends \
        python3 python3-venv python3-pip ffmpeg ca-certificates curl openssl >/dev/null
    local minor
    minor="$(python3 -c 'import sys; print(sys.version_info.minor)')"
    if [[ "$(python3 -c 'import sys; print(sys.version_info.major)')" -ne 3 || ${minor} -lt ${MIN_PY_MINOR} ]]; then
        die "Python 3.${MIN_PY_MINOR}+ requis (trouvé : $(python3 --version)). Utilisez Debian 12+ ou Ubuntu 22.04+."
    fi
    c_ok "Paquets installés ($(python3 --version), $(ffmpeg -version | head -n1 | cut -d' ' -f1-3))"
}

create_user() {
    if ! id -u "${APP_USER}" >/dev/null 2>&1; then
        useradd --system --home-dir "${INSTALL_DIR}" --no-create-home \
                --shell /usr/sbin/nologin "${APP_USER}"
        c_ok "Utilisateur système '${APP_USER}' créé"
    fi
}

copy_sources() {
    c_info "Copie du code dans ${INSTALL_DIR}…"
    [[ -f "${SRC_DIR}/pyproject.toml" && -d "${SRC_DIR}/app" ]] \
        || die "Sources introuvables dans ${SRC_DIR} (lancez le script depuis le dépôt cloné)."
    mkdir -p "${INSTALL_DIR}"
    if [[ "${SRC_DIR}" != "${INSTALL_DIR}" ]]; then
        rm -rf "${INSTALL_DIR}/app"
        cp -r "${SRC_DIR}/app" "${INSTALL_DIR}/"
        cp "${SRC_DIR}/pyproject.toml" "${SRC_DIR}/README.md" "${SRC_DIR}/LICENSE" "${INSTALL_DIR}/"
        mkdir -p "${INSTALL_DIR}/deploy"
        cp "${SRC_DIR}/deploy/"* "${INSTALL_DIR}/deploy/"
        cp "${SRC_DIR}/.env.example" "${INSTALL_DIR}/" 2>/dev/null || true
    fi
    find "${INSTALL_DIR}/app" -name '__pycache__' -type d -prune -exec rm -rf {} +
}

setup_venv() {
    c_info "Création de l'environnement virtuel Python…"
    [[ -x "${INSTALL_DIR}/.venv/bin/python" ]] || python3 -m venv "${INSTALL_DIR}/.venv"
    "${INSTALL_DIR}/.venv/bin/pip" install -q --upgrade pip setuptools wheel
    "${INSTALL_DIR}/.venv/bin/pip" install -q --upgrade "${INSTALL_DIR}"
    chown -R root:"${APP_USER}" "${INSTALL_DIR}"
    chmod -R u=rwX,g=rX,o= "${INSTALL_DIR}"
    c_ok "Dépendances installées"
}

# Remplace (ou ajoute) KEY=VALUE dans le fichier d'environnement.
set_env() {
    local key="$1" value="$2" escaped
    escaped="$(printf '%s' "${value}" | sed -e 's/[\/&|]/\\&/g')"
    if grep -qE "^${key}=" "${ENV_FILE}"; then
        sed -i "s|^${key}=.*|${key}=${escaped}|" "${ENV_FILE}"
    else
        printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
    fi
}

get_env() {
    grep -E "^$1=" "${ENV_FILE}" 2>/dev/null | head -n1 | cut -d= -f2- || true
}

ask() {
    # ask VAR "Question" [secret] — demande la valeur si absente, factice ou à la 1re installation.
    local var="$1" prompt="$2" secret="${3:-}" current answer=""
    current="$(get_env "${var}")"
    if [[ -n "${!var:-}" ]]; then
        set_env "${var}" "${!var}"
        return
    fi
    if [[ "${FRESH_CONFIG}" -eq 1 || -z "${current}" || "${current}" == change-me* ]] && [[ -t 0 ]]; then
        local default=""
        [[ -z "${secret}" && "${current}" != change-me* ]] && default="${current}"
        while [[ -z "${answer}" ]]; do
            if [[ -n "${secret}" ]]; then
                read -rsp "${prompt} : " answer; echo
            else
                read -rp "${prompt}${default:+ [${default}]} : " answer
                answer="${answer:-${default}}"
            fi
        done
        set_env "${var}" "${answer}"
    fi
}

configure() {
    mkdir -p "${CONFIG_DIR}"
    FRESH_CONFIG=0
    if [[ ! -f "${ENV_FILE}" ]]; then
        FRESH_CONFIG=1
        cp "${INSTALL_DIR}/.env.example" "${ENV_FILE}"
        set_env MOVOMATIC_API_KEY "$(openssl rand -hex 32)"
        # Dans le service, les cookies doivent être dans /var/lib/movomatic (écriture autorisée).
        set_env YTDLP_COOKIES_FILE ""
        c_ok "Fichier de configuration créé : ${ENV_FILE}"
    else
        c_info "Configuration existante conservée : ${ENV_FILE}"
    fi

    ask GEMINI_API_KEY      "Clé API Google Gemini (https://aistudio.google.com/apikey)" secret
    ask JELLYSEERR_URL      "URL de Jellyseerr (ex. http://192.168.1.20:5055)"
    ask JELLYSEERR_API_KEY  "Clé API Jellyseerr (Paramètres ➜ Général)" secret
    [[ -n "${MOVOMATIC_PORT:-}" ]] && set_env PORT "${MOVOMATIC_PORT}"

    chown root:"${APP_USER}" "${CONFIG_DIR}" "${ENV_FILE}"
    chmod 750 "${CONFIG_DIR}"
    chmod 640 "${ENV_FILE}"
}

install_service() {
    c_info "Installation du service systemd…"
    install -m 644 "${INSTALL_DIR}/deploy/movomatic.service" "${SERVICE_FILE}"
    systemctl daemon-reload
    systemctl enable "${APP_NAME}" >/dev/null 2>&1
    systemctl restart "${APP_NAME}"
}

wait_healthy() {
    local port i
    port="$(get_env PORT)"; port="${port:-8000}"
    for i in $(seq 1 20); do
        if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
            c_ok "Movomatic répond sur le port ${port}"
            return 0
        fi
        sleep 1
    done
    c_err "Le service ne répond pas. Consultez : journalctl -u ${APP_NAME} -n 50 --no-pager"
    return 1
}

summary() {
    local port ip
    port="$(get_env PORT)"; port="${port:-8000}"
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    cat <<EOF

────────────────────────────────────────────────────────────────
  Movomatic est installé 🎬

  Endpoint   : http://${ip:-<ip-du-lxc>}:${port}/movomatic
  En-tête    : X-API-Key: $(get_env MOVOMATIC_API_KEY)
  Config     : ${ENV_FILE}
  Logs       : journalctl -u ${APP_NAME} -f
  Mise à jour: git pull && sudo ./deploy/install_lxc.sh --update
────────────────────────────────────────────────────────────────
EOF
}

do_update() {
    [[ -d "${INSTALL_DIR}/.venv" ]] || die "Movomatic n'est pas installé."
    copy_sources
    setup_venv
    # yt-dlp évolue très vite pour suivre Instagram : on prend la dernière version.
    "${INSTALL_DIR}/.venv/bin/pip" install -q --upgrade yt-dlp
    c_ok "yt-dlp $("${INSTALL_DIR}/.venv/bin/yt-dlp" --version)"
    install -m 644 "${INSTALL_DIR}/deploy/movomatic.service" "${SERVICE_FILE}"
    systemctl daemon-reload
    systemctl restart "${APP_NAME}"
    wait_healthy
}

do_uninstall() {
    systemctl disable --now "${APP_NAME}" 2>/dev/null || true
    rm -f "${SERVICE_FILE}"
    systemctl daemon-reload
    rm -rf "${INSTALL_DIR}" /var/cache/movomatic
    c_ok "Service et code supprimés."
    c_warn "Conservés : ${CONFIG_DIR} et /var/lib/movomatic (supprimez-les à la main si besoin)."
}

main() {
    require_root
    case "${1:-}" in
        --update)    do_update ;;
        --uninstall) do_uninstall ;;
        ""|--install)
            check_os
            install_packages
            create_user
            copy_sources
            setup_venv
            configure
            install_service
            wait_healthy && summary
            ;;
        -h|--help) sed -n '2,12p' "$0" ;;
        *) die "Option inconnue : $1 (voir --help)" ;;
    esac
}

main "$@"
