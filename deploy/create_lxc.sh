#!/usr/bin/env bash
# =============================================================================
# Movomatic — create and provision a dedicated LXC container on Proxmox VE
#
# Run on the Proxmox VE HOST as root:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/sudoPierre/movomatic/main/deploy/create_lxc.sh)"
#   ./deploy/create_lxc.sh --ip 192.168.1.50/24 --gw 192.168.1.1
#
# Options:
#   --ctid ID             Container ID                    (default: next free ID)
#   --hostname NAME       Hostname                        (default: movomatic)
#   --os OS               debian-12 | debian-13 | ubuntu-24.04   (default: debian-12)
#   --storage NAME        Root disk storage               (default: local-lvm, else first available)
#   --template-storage N  Template storage                (default: local)
#   --disk GB             Root disk size                  (default: 4)
#   --cores N             CPU cores                       (default: 1)
#   --memory MB           RAM                             (default: 512)
#   --swap MB             Swap                            (default: 512)
#   --bridge NAME         Network bridge                  (default: vmbr0)
#   --ip dhcp|CIDR        IPv4 address                    (default: dhcp)
#   --gw IP               Gateway (required with a static IP)
#   --vlan TAG            VLAN tag
#   --dns IP              DNS server                      (default: host settings)
#   --password PW         Root password                   (default: none, use `pct enter`)
#   --ssh-key FILE        Public SSH key to authorize for root
#   --port N              Movomatic port                  (default: 8000)
#   --repo URL            Git repository                  (default: GitHub upstream)
#   --branch NAME         Git branch                      (default: main)
#   -y, --yes             Non-interactive: do not prompt or ask for confirmation
#   -h, --help            Show this help
#
# Secrets can be passed as environment variables, otherwise they are prompted:
#   GEMINI_API_KEY, JELLYSEERR_URL, JELLYSEERR_API_KEY
# =============================================================================
set -euo pipefail

CTID=""
HOSTNAME_CT="movomatic"
OS="debian-12"
STORAGE=""
TEMPLATE_STORAGE="local"
DISK_GB=4
CORES=1
MEMORY_MB=512
SWAP_MB=512
BRIDGE="vmbr0"
IP="dhcp"
GW=""
VLAN=""
DNS=""
PASSWORD=""
SSH_KEY=""
PORT=8000
REPO="https://github.com/sudoPierre/movomatic.git"
BRANCH="main"
ASSUME_YES=0

c_info()  { printf '\033[1;34m[i]\033[0m %s\n' "$*"; }
c_ok()    { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
c_warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
c_err()   { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; }
die()     { c_err "$*"; exit 1; }

usage() {
    if [[ -r "${BASH_SOURCE[0]:-}" ]]; then
        sed -n '/^# Run on/,/^# =====/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'
    else
        echo "See https://github.com/sudoPierre/movomatic#one-command-proxmox-install"
    fi
    exit 0
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --ctid)             CTID="$2"; shift ;;
            --hostname)         HOSTNAME_CT="$2"; shift ;;
            --os)               OS="$2"; shift ;;
            --storage)          STORAGE="$2"; shift ;;
            --template-storage) TEMPLATE_STORAGE="$2"; shift ;;
            --disk)             DISK_GB="$2"; shift ;;
            --cores)            CORES="$2"; shift ;;
            --memory)           MEMORY_MB="$2"; shift ;;
            --swap)             SWAP_MB="$2"; shift ;;
            --bridge)           BRIDGE="$2"; shift ;;
            --ip)               IP="$2"; shift ;;
            --gw)               GW="$2"; shift ;;
            --vlan)             VLAN="$2"; shift ;;
            --dns)              DNS="$2"; shift ;;
            --password)         PASSWORD="$2"; shift ;;
            --ssh-key)          SSH_KEY="$2"; shift ;;
            --port)             PORT="$2"; shift ;;
            --repo)             REPO="$2"; shift ;;
            --branch)           BRANCH="$2"; shift ;;
            -y|--yes)           ASSUME_YES=1 ;;
            -h|--help)          usage ;;
            *) die "Unknown option: $1 (see --help)" ;;
        esac
        shift
    done
}

# Run a command in the container with a locale that exists in every template.
ct_exec() { pct exec "${CTID}" -- env LC_ALL=C.UTF-8 LANG=C.UTF-8 "$@"; }

interactive() { [[ ${ASSUME_YES} -eq 0 && -t 0 ]]; }

check_host() {
    [[ ${EUID} -eq 0 ]] || die "Run this script as root on the Proxmox VE host."
    for cmd in pct pveam pvesh pvesm; do
        command -v "${cmd}" >/dev/null || die "'${cmd}' not found: this script must run on a Proxmox VE host."
    done
}

validate() {
    case "${OS}" in
        debian-12|debian-13) OSTYPE="debian" ;;
        ubuntu-24.04)        OSTYPE="ubuntu" ;;
        *) die "Unsupported --os '${OS}' (debian-12, debian-13, ubuntu-24.04)." ;;
    esac
    [[ -z "${CTID}" ]] && CTID="$(pvesh get /cluster/nextid)"
    [[ "${CTID}" =~ ^[0-9]+$ ]] || die "Invalid --ctid '${CTID}'."
    if pct status "${CTID}" >/dev/null 2>&1; then
        die "Container ${CTID} already exists. Choose another --ctid."
    fi
    for n in DISK_GB CORES MEMORY_MB SWAP_MB PORT; do
        [[ "${!n}" =~ ^[0-9]+$ ]] || die "${n} must be a number (got '${!n}')."
    done
    if [[ "${IP}" != "dhcp" ]]; then
        [[ "${IP}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || die "--ip must be 'dhcp' or CIDR (e.g. 192.168.1.50/24)."
        [[ -n "${GW}" ]] || die "--gw is required with a static IP."
    fi
    [[ -z "${SSH_KEY}" || -r "${SSH_KEY}" ]] || die "SSH key file not readable: ${SSH_KEY}"
    ip link show "${BRIDGE}" >/dev/null 2>&1 || die "Bridge '${BRIDGE}' does not exist on this host."

    if [[ -z "${STORAGE}" ]]; then
        local available
        available="$(pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active" {print $1}')"
        if grep -qx "local-lvm" <<<"${available}"; then
            STORAGE="local-lvm"
        else
            STORAGE="$(head -n1 <<<"${available}")"
        fi
        [[ -n "${STORAGE}" ]] || die "No active storage supports containers (rootdir). Use --storage."
    fi
    pvesm status -storage "${STORAGE}" >/dev/null 2>&1 || die "Storage '${STORAGE}' not found."
    pvesm status -storage "${TEMPLATE_STORAGE}" >/dev/null 2>&1 || die "Template storage '${TEMPLATE_STORAGE}' not found."
}

prompt_secret() {
    # prompt_secret VAR "Question" [visible]
    local var="$1" prompt="$2" visible="${3:-}" answer=""
    [[ -n "${!var:-}" ]] && return
    interactive || die "${var} is required (export it or run without --yes)."
    while [[ -z "${answer}" ]]; do
        if [[ -n "${visible}" ]]; then read -rp "${prompt}: " answer; else read -rsp "${prompt}: " answer; echo; fi
    done
    printf -v "${var}" '%s' "${answer}"
}

collect_secrets() {
    prompt_secret GEMINI_API_KEY     "Google Gemini API key (https://aistudio.google.com/apikey)"
    prompt_secret JELLYSEERR_URL     "Jellyseerr URL (e.g. http://192.168.1.20:5055)" visible
    prompt_secret JELLYSEERR_API_KEY "Jellyseerr API key (Settings ➜ General)"
    [[ "${JELLYSEERR_URL}" =~ ^https?:// ]] || die "JELLYSEERR_URL must start with http:// or https://"
}

confirm() {
    cat <<EOF

  Container ID : ${CTID}
  Hostname     : ${HOSTNAME_CT}
  OS           : ${OS} (unprivileged, nesting=1, start on boot)
  Resources    : ${CORES} vCPU · ${MEMORY_MB} MB RAM · ${SWAP_MB} MB swap · ${DISK_GB} GB on ${STORAGE}
  Network      : ${BRIDGE} · ${IP}${GW:+ · gw ${GW}}${VLAN:+ · VLAN ${VLAN}}${DNS:+ · DNS ${DNS}}
  Movomatic    : port ${PORT} · ${REPO} (${BRANCH})
  Jellyseerr   : ${JELLYSEERR_URL}

EOF
    if interactive; then
        local answer
        read -rp "Create this container? [Y/n] " answer
        [[ -z "${answer}" || "${answer}" =~ ^[YyOo] ]] || die "Aborted."
    fi
}

fetch_template() {
    c_info "Looking for the latest ${OS} template…"
    pveam update >/dev/null 2>&1 || c_warn "pveam update failed, using the cached template list."
    local name
    name="$(pveam available --section system | awk '{print $2}' | grep -E "^${OS}-standard_" | sort -V | tail -n1)"
    [[ -n "${name}" ]] || die "No '${OS}-standard' template available from pveam."
    if pveam list "${TEMPLATE_STORAGE}" | awk '{print $1}' | grep -q "/${name}$"; then
        c_ok "Template already present: ${name}"
    else
        c_info "Downloading ${name}…"
        pveam download "${TEMPLATE_STORAGE}" "${name}" >/dev/null
        c_ok "Template downloaded: ${name}"
    fi
    TEMPLATE="${TEMPLATE_STORAGE}:vztmpl/${name}"
}

create_container() {
    local net="name=eth0,bridge=${BRIDGE},ip=${IP}"
    [[ -n "${GW}" ]] && net+=",gw=${GW}"
    [[ -n "${VLAN}" ]] && net+=",tag=${VLAN}"

    local args=(
        "${CTID}" "${TEMPLATE}"
        --hostname "${HOSTNAME_CT}"
        --ostype "${OSTYPE}"
        --cores "${CORES}" --memory "${MEMORY_MB}" --swap "${SWAP_MB}"
        --rootfs "${STORAGE}:${DISK_GB}"
        --net0 "${net}"
        --unprivileged 1
        --features nesting=1
        --onboot 1
        --timezone host
        --tags movomatic
        --description "$(printf 'Movomatic — Instagram Reel ➜ Gemini ➜ Jellyseerr\n%s' "${REPO}")"
    )
    [[ -n "${DNS}" ]] && args+=(--nameserver "${DNS}")
    [[ -n "${PASSWORD}" ]] && args+=(--password "${PASSWORD}")
    [[ -n "${SSH_KEY}" ]] && args+=(--ssh-public-keys "${SSH_KEY}")

    c_info "Creating container ${CTID}…"
    pct create "${args[@]}" >/dev/null
    CREATED=1
    pct start "${CTID}"
    c_ok "Container ${CTID} created and started"
}

wait_network() {
    c_info "Waiting for network inside the container…"
    local i
    for i in $(seq 1 60); do
        if ct_exec getent hosts github.com >/dev/null 2>&1; then
            c_ok "Network is up"
            return
        fi
        sleep 2
    done
    die "The container has no working network/DNS after 2 minutes."
}

provision() {
    c_info "Installing git and cloning ${REPO} (${BRANCH})…"
    ct_exec bash -c "
        set -e
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends git ca-certificates >/dev/null
        rm -rf /root/movomatic
        git clone -q --depth 1 --branch '${BRANCH}' '${REPO}' /root/movomatic
    "
    c_ok "Repository cloned in /root/movomatic"

    # Secrets go through a temporary 600 file instead of the command line,
    # so they never show up in the host's process list.
    SECRETS_FILE="$(mktemp)"
    chmod 600 "${SECRETS_FILE}"
    {
        printf 'GEMINI_API_KEY=%q\n' "${GEMINI_API_KEY}"
        printf 'JELLYSEERR_URL=%q\n' "${JELLYSEERR_URL}"
        printf 'JELLYSEERR_API_KEY=%q\n' "${JELLYSEERR_API_KEY}"
        printf 'MOVOMATIC_PORT=%q\n' "${PORT}"
    } > "${SECRETS_FILE}"
    pct push "${CTID}" "${SECRETS_FILE}" /root/.movomatic-install.env --perms 600
    rm -f "${SECRETS_FILE}"

    c_info "Running the Movomatic installer inside the container…"
    ct_exec bash -c '
        set -a; . /root/.movomatic-install.env; set +a
        rm -f /root/.movomatic-install.env
        /root/movomatic/deploy/install_lxc.sh </dev/null
    '
}

on_exit() {
    local code=$?
    [[ -n "${SECRETS_FILE:-}" ]] && rm -f "${SECRETS_FILE}"
    if [[ ${code} -ne 0 && "${CREATED:-0}" -eq 1 ]]; then
        c_err "Provisioning failed. Container ${CTID} was kept for debugging:"
        c_err "  pct enter ${CTID}      # inspect"
        c_err "  pct destroy ${CTID} --force --purge   # remove it"
    fi
}

main() {
    parse_args "$@"
    check_host
    validate
    collect_secrets
    confirm
    trap on_exit EXIT
    fetch_template
    create_container
    wait_network
    provision

    cat <<EOF

  Container : ${CTID} (${HOSTNAME_CT})
  Shell     : pct enter ${CTID}
  Update    : pct exec ${CTID} -- bash -c 'cd /root/movomatic && git pull && ./deploy/install_lxc.sh --update'

EOF
}

main "$@"
