# 🎬 Movomatic

**Share an Instagram Reel from your iPhone ➜ every movie, series or anime it features is requested on Jellyseerr automatically.**

Movomatic is a lightweight FastAPI microservice. It:

1. receives an Instagram Reel URL sent by an **iOS Shortcut** from the share sheet;
2. extracts the caption, hashtags and author with **yt-dlp**;
3. asks **Google Gemini (Flash)** to identify **every** title it features (a single trailer as well as a "top 10"). If the caption does not name them all, Gemini analyses **the video itself** (frames, on-screen text and audio);
4. looks each title up on TMDB through **Jellyseerr**, detects **anime**, and sends one request per title, which Jellyseerr forwards to **Radarr / Sonarr**.

```
iPhone ─► iOS Shortcut ─► POST /movomatic ─► yt-dlp ─► Gemini ─► Jellyseerr ─► Radarr / Sonarr
                                    ▲                                                 │
                                    └───────────── "✅ Movie requested: Dune (2021)" ◄─┘
```

Movomatic is built to run **natively in a Debian/Ubuntu LXC container on Proxmox VE**, without Docker. It uses a Python virtual environment and a `systemd` service.

---

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [One-command Proxmox install](#one-command-proxmox-install)
- [Manual install in an existing LXC](#manual-install-in-an-existing-lxc)
- [Configuration](#configuration)
- [iOS Shortcut](#ios-shortcut)
- [API](#api)
- [Instagram cookies](#instagram-cookies)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Security](#security)
- [License](#license)

## Features

- 🔐 API key authentication (`X-API-Key`, compared in constant time).
- 🎞️ **Multi-title Reels**: top lists, compilations and recommendations are all requested (up to `MAX_TITLES_PER_REEL`).
- 🧠 Two-step identification: **text first** (fast and cheap), then **video** when the caption does not name every work featured.
- 📐 **Structured JSON output** enforced on Gemini through a Pydantic schema, so no fragile parsing.
- 🎯 Picks the right TMDB result with a scoring system: title, original title, year, type and popularity.
- 🍥 Anime detection (Animation genre + Japanese language), with optional overrides for the Sonarr/Radarr server, quality profile and root folder.
- ♻️ No duplicates: titles that are already available or already requested are reported without a new request.
- 🧪 `dry_run` mode to identify without requesting anything.
- 🧹 Temporary videos are deleted locally **and** on the Gemini side after analysis.
- 🛡️ Hardened systemd service (dedicated user, `ProtectSystem=strict`, `PrivateTmp`…).
- 📦 One-command container creation on the Proxmox host.

## Requirements

| Item | Details |
|---|---|
| Proxmox VE | 7 or 8. The container runs **Debian 12/13** or **Ubuntu 24.04** (Python ≥ 3.11) |
| Resources | 1 vCPU, 512 MB RAM and 4 GB of disk are enough |
| Gemini | A free API key: <https://aistudio.google.com/apikey> |
| Jellyseerr | An instance set up with Radarr/Sonarr, plus its API key (*Settings ➜ General*) |
| Network | The container must reach the Internet (Instagram, Gemini) and Jellyseerr |

## One-command Proxmox install

Run this as root in the **Proxmox VE host** shell (*Datacenter ➜ your node ➜ Shell*):

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/sudoPierre/movomatic/main/deploy/create_lxc.sh)"
```

[`deploy/create_lxc.sh`](deploy/create_lxc.sh) asks for your Gemini key, Jellyseerr URL and Jellyseerr key, then shows a summary and asks for confirmation. After that it:

1. downloads the latest Debian 12 template (`pveam`) if needed;
2. creates an **unprivileged** container with `nesting=1` (needed for systemd hardening), start on boot, the host timezone and the `movomatic` tag;
3. starts it and waits until the network is up;
4. clones this repository and runs [`install_lxc.sh`](deploy/install_lxc.sh) inside it. Secrets are passed through a temporary `600` file, never on the command line.

It ends by printing the endpoint URL and the generated API key for the iOS Shortcut.

Defaults: next free CT ID, `movomatic` hostname, 1 vCPU, 512 MB RAM, 4 GB on `local-lvm`, DHCP on `vmbr0`. Every value can be changed:

```bash
# Static IP, VLAN, more RAM, SSH key, fully non-interactive
GEMINI_API_KEY=xxx JELLYSEERR_URL=http://192.168.1.20:5055 JELLYSEERR_API_KEY=yyy \
  ./deploy/create_lxc.sh --ctid 120 --ip 192.168.1.50/24 --gw 192.168.1.1 --vlan 20 \
                         --memory 1024 --ssh-key ~/.ssh/id_ed25519.pub --yes
```

| Option | Default | Description |
|---|---|---|
| `--ctid ID` | next free | Container ID |
| `--hostname NAME` | `movomatic` | Hostname |
| `--os OS` | `debian-12` | `debian-12`, `debian-13` or `ubuntu-24.04` |
| `--storage NAME` | `local-lvm` | Root disk storage (falls back to the first storage that supports containers) |
| `--template-storage NAME` | `local` | Where templates are stored |
| `--disk GB` / `--cores N` / `--memory MB` / `--swap MB` | `4` / `1` / `512` / `512` | Resources |
| `--bridge NAME` | `vmbr0` | Network bridge |
| `--ip dhcp\|CIDR` / `--gw IP` | `dhcp` | IPv4 settings (`--gw` is required with a static IP) |
| `--vlan TAG` / `--dns IP` | — | VLAN tag, DNS server |
| `--password PW` / `--ssh-key FILE` | — | Root access (otherwise use `pct enter <ID>`) |
| `--port N` | `8000` | Movomatic port |
| `--repo URL` / `--branch NAME` | upstream / `main` | Fork or branch to deploy |
| `-y`, `--yes` | — | No prompts (secrets must be in the environment) |

If provisioning fails, the container is **kept** so you can investigate (`pct enter <ID>`). Remove it with `pct destroy <ID> --force --purge`.

**Update** from the host:

```bash
pct exec <ID> -- bash -c 'cd /root/movomatic && git pull && ./deploy/install_lxc.sh --update'
```

## Manual install in an existing LXC

If you already have a Debian/Ubuntu container (with `nesting=1` turned on), run this inside it:

```bash
apt update && apt install -y git
git clone https://github.com/sudoPierre/movomatic.git
cd movomatic
./deploy/install_lxc.sh          # as root (or with sudo)
```

The installer:

- installs `python3`, `python3-venv`, `ffmpeg`…;
- creates the `movomatic` system user;
- installs the code in `/opt/movomatic` with its own `.venv`;
- creates `/etc/movomatic/movomatic.env` (mode `640`), **generates the API key** and asks for the Gemini and Jellyseerr keys;
- installs and starts `movomatic.service`, then checks `/health`.

Other commands:

```bash
git pull && ./deploy/install_lxc.sh --update   # code + dependencies + latest yt-dlp
./deploy/install_lxc.sh --uninstall            # keeps /etc/movomatic and /var/lib/movomatic
```

> Instagram changes often. If extraction suddenly breaks, an `--update` (which upgrades yt-dlp) usually fixes it.

## Configuration

All settings are environment variables, read from `/etc/movomatic/movomatic.env` in production or from `.env` in development. `pydantic-settings` validates them at startup: an invalid value stops the service from starting, and the error shows up in `journalctl`. See [`.env.example`](.env.example).

| Variable | Default | Description |
|---|---|---|
| `MOVOMATIC_API_KEY` | — **(required)** | Key expected in `X-API-Key` (16 characters min.) |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Listen address |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `MAX_CONCURRENT_JOBS` | `2` | Reels processed in parallel |
| `MAX_TITLES_PER_REEL` | `10` | Max titles requested from one Reel |
| `GEMINI_API_KEY` | — **(required)** | Google AI Studio key |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Main Gemini model |
| `GEMINI_FALLBACK_MODELS` | `gemini-3.8-flash,gemini-flash-latest` | Tried in order when the main model is overloaded (429/5xx), times out or is retired (404) |
| `GEMINI_MIN_CONFIDENCE` | `0.6` | Per-title confidence threshold (0–1) |
| `GEMINI_VIDEO_FALLBACK` | `true` | Analyse the video when the text is not enough |
| `GEMINI_TIMEOUT_SECONDS` | `45` | Timeout per model before falling back (429/5xx are first retried twice on the same model) |
| `YTDLP_COOKIES_FILE` | *(empty)* | Netscape cookies file for Instagram |
| `YTDLP_MAX_FILESIZE_MB` | `100` | Max size of the downloaded video |
| `YTDLP_MAX_DURATION_SECONDS` | `600` | Max video duration |
| `TEMP_DIR` | *(empty = /tmp)* | Temporary directory |
| `JELLYSEERR_URL` | — **(required)** | e.g. `http://192.168.1.20:5055` |
| `JELLYSEERR_API_KEY` | — **(required)** | Jellyseerr API key |
| `JELLYSEERR_VERIFY_SSL` | `true` | Verify the HTTPS certificate |
| `JELLYSEERR_REQUEST_4K` | `false` | Request the 4K version |
| `JELLYSEERR_USER_ID` | *(empty)* | Request on behalf of another user |
| `ANIME_SONARR_SERVER_ID` / `_PROFILE_ID` / `_ROOT_FOLDER` | *(empty)* | Overrides for anime series |
| `ANIME_RADARR_SERVER_ID` / `_PROFILE_ID` / `_ROOT_FOLDER` | *(empty)* | Overrides for anime movies |

> **Anime:** Jellyseerr already applies the "Anime" profile and folder set in *Settings ➜ Services ➜ Sonarr*. The `ANIME_*` variables are only for forcing different values. To find the IDs, call `GET /api/v1/service/sonarr`, then `GET /api/v1/service/sonarr/{id}` on Jellyseerr.

After changing anything: `systemctl restart movomatic`.

## iOS Shortcut

1. Open **Shortcuts** ➜ **+** ➜ name it "Movomatic".
2. Tap **ⓘ** ➜ turn on **Show in Share Sheet**. Accepted types: **URLs** and **Text**.
3. Add a **Get Contents of URL** action:
   - URL: `http://<lxc-ip>:8000/movomatic`
   - Method: **POST**
   - Headers: `X-API-Key` = *your key*; `Content-Type` = `application/json`
   - Request Body: **JSON**, key `url` (Text) = **Shortcut Input**
4. Add **Get Dictionary Value**: key `message` in *Contents of URL*.
5. Add **Show Notification** with *Dictionary Value*.

In Instagram, tap **Share ➜ Movomatic**. A few seconds later a notification shows something like "✅ Movie requested: Dune: Part Two (2024)".

> Video analysis can take 20 to 60 seconds. Outside your home network, use a VPN (WireGuard, Tailscale…) or an HTTPS reverse proxy (see [Security](#security)).

## API

Interactive OpenAPI docs are available at `http://<ip>:8000/docs`.

### `GET /health`

```json
{ "status": "ok", "version": "1.1.0" }
```

### `POST /movomatic`

```bash
curl -X POST http://localhost:8000/movomatic \
  -H "X-API-Key: $MOVOMATIC_API_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=abc", "dry_run": false}'
```

The `url` field also accepts text that contains the link, which is what the share sheet sometimes sends. Tracking parameters (`?igsh=`, `?utm_source=`) are removed.

**Response (200):** one entry in `results` per title found in the Reel.

```json
{
  "status": "requested",
  "message": "🎬 2 titles: 1 requested, 1 already available\n✅ Movie requested: Arrival (2016)\n📺 Ad Astra (2019): already available",
  "source": "metadata",
  "works_featured": 2,
  "results": [
    {
      "status": "requested",
      "message": "✅ Movie requested: Arrival (2016)",
      "identification": {
        "title": "Arrival", "original_title": null, "year": 2016, "media_type": "movie",
        "is_anime": false, "confidence": 0.95, "reasoning": "Villeneuve film with Amy Adams and aliens."
      },
      "media": {
        "tmdb_id": 329865, "media_type": "movie", "title": "Arrival", "year": 2016,
        "is_anime": false, "poster_url": "https://image.tmdb.org/t/p/w500/…jpg"
      },
      "request_id": 42
    },
    { "status": "already_available", "message": "📺 Ad Astra (2019): already available", "…": "…" }
  ]
}
```

With a single title, `message` is just that title's line (e.g. `✅ Movie requested: Dune (2021)`). The top-level `status` is the best outcome across all titles, in this order:

| `status` | Meaning |
|---|---|
| `requested` | Request created in Jellyseerr |
| `identified` | Identified, no request sent (`dry_run`) |
| `already_requested` | Already requested or downloading |
| `already_available` | Already in the library |
| `not_found` | Title identified but not found on TMDB |
| `error` | Jellyseerr refused this title (the others are still processed) |
| `not_identified` | Gemini could not identify any title with enough confidence |

**Errors:** `401` (invalid key), `422` (invalid URL or unreadable Reel), `502` (Gemini or Jellyseerr error). The body is always `{"status": "error", "message": "❌ …"}`, so the Shortcut still shows a clear message.

## Instagram cookies

Without a logged-in session, Instagram often blocks access to Reels ("login required", "rate-limit"). To fix it:

1. In a browser logged into Instagram (ideally with a secondary account), export the cookies in **Netscape** format, for example with the "Get cookies.txt LOCALLY" extension.
2. Copy them into the container:
   ```bash
   install -o movomatic -g movomatic -m 600 cookies.txt /var/lib/movomatic/cookies.txt
   ```
3. In `/etc/movomatic/movomatic.env`, set `YTDLP_COOKIES_FILE=/var/lib/movomatic/cookies.txt`.
4. Run `systemctl restart movomatic`.

> The file must be in `/var/lib/movomatic`: it is the only directory the service can write to, and yt-dlp updates the cookies.

## Operations

```bash
systemctl status movomatic     # status
journalctl -u movomatic -f     # live logs
systemctl restart movomatic    # restart
```

| Path | Contents |
|---|---|
| `/opt/movomatic` | Code + virtualenv (`.venv`) |
| `/etc/movomatic/movomatic.env` | Configuration and secrets |
| `/var/lib/movomatic` | Instagram cookies |
| `/var/cache/movomatic` | yt-dlp cache |

## Troubleshooting

| Symptom | What to check |
|---|---|
| `status=226/NAMESPACE` at startup | Turn on nesting (`pct set <ID> --features nesting=1`, then restart the CT) or comment out the `Protect*`/`Private*` lines in the unit |
| `Could not read the Reel: … login required` | Set up [Instagram cookies](#instagram-cookies) |
| Extraction suddenly fails | `./deploy/install_lxc.sh --update` (new yt-dlp release) |
| `Jellyseerr API key rejected` | Check `JELLYSEERR_API_KEY` |
| `Jellyseerr unreachable` | From the CT, run `curl $JELLYSEERR_URL/api/v1/status` |
| `Gemini unavailable (… returned 429/503)` | Quota exceeded or every model overloaded: wait, turn on billing, or change `GEMINI_FALLBACK_MODELS` |
| `Gemini API error (404) … no longer available` | Google retired the model: set `GEMINI_MODEL` to a current one |
| Service does not start | `journalctl -u movomatic -n 50` shows the configuration validation error |
| Wrong title identified | Try `"dry_run": true` with `LOG_LEVEL=debug`; raise `GEMINI_MIN_CONFIDENCE` |
| `create_lxc.sh`: no network | Check the bridge/VLAN/gateway, or use `--dns` |

## Development

```bash
git clone https://github.com/sudoPierre/movomatic.git && cd movomatic
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in the keys
pytest                 # unit tests (yt-dlp, Gemini and Jellyseerr are mocked)
movomatic              # or: uvicorn app.main:app --reload
```

Layout:

```text
app/
├── main.py         # FastAPI routes and pipeline orchestration
├── config.py       # Settings (pydantic-settings)
├── models.py       # API schemas + Gemini output schema
├── extractor.py    # yt-dlp: metadata and temporary download
├── analyzer.py     # Gemini: text analysis, then video
└── jellyseerr.py   # TMDB search, scoring, anime detection, request
deploy/
├── create_lxc.sh   # Proxmox host: creates and provisions the container
├── install_lxc.sh  # Inside the container: installs/updates the service
└── movomatic.service
tests/test_api.py
```

## Security

- **Do not** expose port 8000 directly to the Internet. Use a VPN (WireGuard, Tailscale) or an HTTPS reverse proxy (Caddy, Nginx Proxy Manager, Traefik). With a proxy on the same machine, set `HOST=127.0.0.1`.
- The installer generates `MOVOMATIC_API_KEY` at random (256 bits).
- Secrets are readable only by root and the `movomatic` group (`640`).
- Analysed videos are sent to the Google Gemini API, then deleted after analysis.

## License

[MIT](LICENSE)
