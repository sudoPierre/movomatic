# 🎬 Movomatic

**Partage un Reel Instagram depuis ton iPhone ➜ le film, la série ou l'anime est automatiquement demandé sur Jellyseerr.**

Movomatic est un microservice FastAPI léger :

1. il reçoit l'URL d'un Reel Instagram envoyée par un **Raccourci iOS** (feuille de partage) ;
2. il en extrait la légende, les hashtags et l'auteur avec **yt-dlp** ;
3. il demande à **Google Gemini 2.5 Flash** d'identifier l'œuvre (et, si la légende ne suffit pas, il analyse **la vidéo elle-même** : images, texte à l'écran et audio) ;
4. il recherche l'œuvre sur TMDB via **Jellyseerr**, détecte les **animes** et envoie la requête, que Jellyseerr transmet à **Radarr / Sonarr**.

```
iPhone ─► Raccourci iOS ─► POST /movomatic ─► yt-dlp ─► Gemini ─► Jellyseerr ─► Radarr / Sonarr
                                     ▲                                                 │
                                     └──────────── « ✅ Film demandé : Dune (2021) » ◄───┘
```

Movomatic est conçu pour tourner **nativement dans un conteneur LXC Debian/Ubuntu sur Proxmox VE**, sans Docker : un environnement virtuel Python et un service `systemd`.

---

## Sommaire

- [Fonctionnalités](#fonctionnalités)
- [Prérequis](#prérequis)
- [Installation sur Proxmox (LXC)](#installation-sur-proxmox-lxc)
- [Configuration](#configuration)
- [Raccourci iOS](#raccourci-ios)
- [API](#api)
- [Cookies Instagram](#cookies-instagram)
- [Exploitation](#exploitation)
- [Dépannage](#dépannage)
- [Développement](#développement)
- [Sécurité](#sécurité)
- [Licence](#licence)

## Fonctionnalités

- 🔐 Authentification par clé API (`X-API-Key`, comparaison à temps constant).
- 🧠 Identification en deux temps : **texte d'abord** (rapide et peu coûteux), **vidéo ensuite** si la confiance est inférieure au seuil.
- 📐 Sortie **JSON structurée** imposée à Gemini (schéma Pydantic), sans parsing fragile.
- 🎯 Choix du bon résultat TMDB par score (titre, titre original, année, type, popularité).
- 🍥 Détection des animes (genre Animation + langue japonaise) avec surcharges optionnelles du serveur, du profil de qualité et du dossier racine Sonarr/Radarr.
- ♻️ Pas de doublons : les œuvres déjà disponibles ou déjà demandées sont signalées sans nouvelle requête.
- 🧪 Mode `dry_run` pour identifier sans rien demander.
- 🧹 Les vidéos temporaires sont supprimées localement **et** côté Gemini après analyse.
- 🛡️ Service systemd durci (utilisateur dédié, `ProtectSystem=strict`, `PrivateTmp`…).

## Prérequis

| Élément | Détail |
|---|---|
| Proxmox VE | 7 ou 8, conteneur LXC **Debian 12+** ou **Ubuntu 22.04+** (Python ≥ 3.11) |
| Ressources | 1 vCPU, 512 Mo de RAM, 4 Go de disque suffisent |
| Gemini | Une clé API gratuite : <https://aistudio.google.com/apikey> |
| Jellyseerr | Une instance configurée avec Radarr/Sonarr et sa clé API (*Paramètres ➜ Général*) |
| Réseau | Le LXC doit joindre Internet (Instagram, Gemini) et Jellyseerr |

## Installation sur Proxmox (LXC)

### 1. Créer le conteneur

Via l'interface Proxmox (*Créer CT*) ou en ligne de commande sur l'hôte :

```bash
pveam update
pveam download local debian-12-standard_12.7-1_amd64.tar.zst   # adaptez à la version listée par `pveam available`

pct create 120 local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst \
  --hostname movomatic --cores 1 --memory 512 --swap 512 \
  --rootfs local-lvm:4 --net0 name=eth0,bridge=vmbr0,ip=dhcp \
  --unprivileged 1 --features nesting=1 --onboot 1
pct start 120
pct enter 120
```

> `nesting=1` permet au durcissement systemd (espaces de noms) de fonctionner dans un LXC non privilégié.

### 2. Installer Movomatic

Dans le conteneur :

```bash
apt update && apt install -y git
git clone https://github.com/sudoPierre/movomatic.git
cd movomatic
sudo ./deploy/install_lxc.sh
```

Le script :

- installe `python3`, `python3-venv`, `ffmpeg`… ;
- crée l'utilisateur système `movomatic` ;
- installe le code dans `/opt/movomatic` avec son propre virtualenv `.venv` ;
- crée `/etc/movomatic/movomatic.env` (droits `640`), **génère la clé API** et demande les clés Gemini et Jellyseerr ;
- installe et démarre le service `movomatic.service`, puis vérifie `/health`.

À la fin, il affiche l'URL de l'endpoint et la clé à mettre dans le Raccourci iOS.

**Installation non interactive :**

```bash
sudo GEMINI_API_KEY=xxx JELLYSEERR_URL=http://192.168.1.20:5055 JELLYSEERR_API_KEY=yyy \
     ./deploy/install_lxc.sh
```

### 3. Mettre à jour

```bash
cd movomatic && git pull
sudo ./deploy/install_lxc.sh --update   # code + dépendances + dernière version de yt-dlp
```

> Instagram change régulièrement. Si l'extraction échoue d'un coup, un `--update` (qui met à jour yt-dlp) suffit généralement.

### Désinstaller

```bash
sudo ./deploy/install_lxc.sh --uninstall   # conserve /etc/movomatic et /var/lib/movomatic
```

## Configuration

Toute la configuration se fait par variables d'environnement, dans `/etc/movomatic/movomatic.env` en production ou dans `.env` en développement. Elle est validée au démarrage par `pydantic-settings` : une valeur invalide empêche le service de démarrer et l'erreur apparaît dans `journalctl`. Voir [`.env.example`](.env.example).

| Variable | Défaut | Description |
|---|---|---|
| `MOVOMATIC_API_KEY` | — **(requis)** | Clé attendue dans `X-API-Key` (16 caractères min.) |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Adresse d'écoute |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `MAX_CONCURRENT_JOBS` | `2` | Reels traités en parallèle |
| `GEMINI_API_KEY` | — **(requis)** | Clé Google AI Studio |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Modèle Gemini |
| `GEMINI_MIN_CONFIDENCE` | `0.6` | Seuil de confiance (0–1) |
| `GEMINI_VIDEO_FALLBACK` | `true` | Analyser la vidéo si le texte ne suffit pas |
| `GEMINI_TIMEOUT_SECONDS` | `180` | Délai max. par appel Gemini |
| `YTDLP_COOKIES_FILE` | *(vide)* | Fichier cookies Netscape pour Instagram |
| `YTDLP_MAX_FILESIZE_MB` | `100` | Taille max. de la vidéo téléchargée |
| `YTDLP_MAX_DURATION_SECONDS` | `600` | Durée max. de la vidéo |
| `TEMP_DIR` | *(vide = /tmp)* | Répertoire temporaire |
| `JELLYSEERR_URL` | — **(requis)** | ex. `http://192.168.1.20:5055` |
| `JELLYSEERR_API_KEY` | — **(requis)** | Clé API Jellyseerr |
| `JELLYSEERR_VERIFY_SSL` | `true` | Vérifier le certificat HTTPS |
| `JELLYSEERR_REQUEST_4K` | `false` | Demander la version 4K |
| `JELLYSEERR_USER_ID` | *(vide)* | Demander au nom d'un autre utilisateur |
| `ANIME_SONARR_SERVER_ID` / `_PROFILE_ID` / `_ROOT_FOLDER` | *(vide)* | Surcharges pour les séries anime |
| `ANIME_RADARR_SERVER_ID` / `_PROFILE_ID` / `_ROOT_FOLDER` | *(vide)* | Surcharges pour les films anime |

> **Animes :** Jellyseerr applique déjà automatiquement le profil et le dossier « Anime » définis dans *Paramètres ➜ Services ➜ Sonarr*. Les variables `ANIME_*` ne servent qu'à forcer d'autres valeurs. Pour trouver les identifiants : `GET /api/v1/service/sonarr` puis `GET /api/v1/service/sonarr/{id}` sur Jellyseerr.

Après modification : `sudo systemctl restart movomatic`.

## Raccourci iOS

1. Ouvre **Raccourcis** ➜ **+** ➜ nomme-le « Movomatic ».
2. Touche **ⓘ** ➜ active **Afficher dans la feuille de partage** ; types acceptés : **URL** et **Texte**.
3. Ajoute l'action **Obtenir le contenu de l'URL** :
   - URL : `http://<ip-du-lxc>:8000/movomatic`
   - Méthode : **POST**
   - En-têtes : `X-API-Key` = *ta clé* ; `Content-Type` = `application/json`
   - Corps de la requête : **JSON**, clé `url` (texte) = **Entrée du raccourci**
4. Ajoute **Obtenir la valeur du dictionnaire** : clé `message`, dans *Contenu de l'URL*.
5. Ajoute **Afficher la notification** avec *Valeur du dictionnaire*.

Dans Instagram : **Partager ➜ Movomatic**. Quelques secondes plus tard, une notification affiche par exemple « ✅ Film demandé : Dune: Part Two (2024) ».

> L'analyse vidéo peut prendre 20 à 60 s. Hors de ton réseau local, passe par un VPN (WireGuard, Tailscale…) ou par un reverse proxy HTTPS (voir [Sécurité](#sécurité)).

## API

La documentation interactive OpenAPI est disponible sur `http://<ip>:8000/docs`.

### `GET /health`

```json
{ "status": "ok", "version": "1.0.0" }
```

### `POST /movomatic`

```bash
curl -X POST http://localhost:8000/movomatic \
  -H "X-API-Key: $MOVOMATIC_API_KEY" -H "Content-Type: application/json" \
  -d '{"url": "https://www.instagram.com/reel/C1a2B3c4D5e/?igsh=abc", "dry_run": false}'
```

Le champ `url` accepte aussi un texte qui contient le lien (comme ce que renvoie parfois la feuille de partage). Les paramètres de suivi (`?igsh=`, `?utm_source=`) sont supprimés.

**Réponse (200) :**

```json
{
  "status": "requested",
  "message": "✅ Film demandé : Dune: Part Two (2024)",
  "source": "metadata",
  "identification": {
    "identified": true, "title": "Dune: Part Two", "original_title": null,
    "year": 2024, "media_type": "movie", "is_anime": false,
    "confidence": 0.96, "reasoning": "La légende cite explicitement Dune 2 et Denis Villeneuve."
  },
  "media": {
    "tmdb_id": 693134, "media_type": "movie", "title": "Dune: Part Two", "year": 2024,
    "is_anime": false, "poster_url": "https://image.tmdb.org/t/p/w500/…jpg"
  },
  "request_id": 42
}
```

| `status` | Signification |
|---|---|
| `requested` | Requête créée dans Jellyseerr |
| `already_available` | Déjà présent dans la médiathèque |
| `already_requested` | Déjà demandé ou en cours de téléchargement |
| `identified` | Identifié, aucune requête envoyée (`dry_run`) |
| `not_identified` | Gemini n'a pas su identifier l'œuvre avec assez de confiance |
| `not_found` | Œuvre identifiée mais introuvable sur TMDB |

**Erreurs :** `401` (clé invalide), `422` (URL invalide ou Reel illisible), `502` (Gemini ou Jellyseerr en erreur). Le corps est toujours `{"status": "error", "message": "❌ …"}`, donc le Raccourci affiche aussi un message clair.

## Cookies Instagram

Sans connexion, Instagram bloque souvent l'accès aux Reels (« login required », « rate-limit »). Pour y remédier :

1. Sur un navigateur connecté à Instagram (de préférence avec un compte secondaire), exporte les cookies au format **Netscape**, par exemple avec l'extension « Get cookies.txt LOCALLY ».
2. Copie-les dans le LXC :
   ```bash
   sudo install -o movomatic -g movomatic -m 600 cookies.txt /var/lib/movomatic/cookies.txt
   ```
3. Dans `/etc/movomatic/movomatic.env` : `YTDLP_COOKIES_FILE=/var/lib/movomatic/cookies.txt`
4. `sudo systemctl restart movomatic`

> Le fichier doit être dans `/var/lib/movomatic` : c'est le seul répertoire où le service a le droit d'écrire (yt-dlp met à jour les cookies).

## Exploitation

```bash
systemctl status movomatic         # état
journalctl -u movomatic -f         # logs en direct
sudo systemctl restart movomatic   # redémarrage
```

| Chemin | Contenu |
|---|---|
| `/opt/movomatic` | Code + virtualenv (`.venv`) |
| `/etc/movomatic/movomatic.env` | Configuration et secrets |
| `/var/lib/movomatic` | Cookies Instagram |
| `/var/cache/movomatic` | Cache yt-dlp |

## Dépannage

| Symptôme | Piste |
|---|---|
| `status=226/NAMESPACE` au démarrage | Active `nesting=1` sur le LXC (`pct set 120 --features nesting=1`) ou commente les lignes `Protect*`/`Private*` du service |
| `Reel illisible : … login required` | Configure les [cookies Instagram](#cookies-instagram) |
| Erreurs d'extraction soudaines | `sudo ./deploy/install_lxc.sh --update` (nouvelle version de yt-dlp) |
| `clé API Jellyseerr refusée` | Vérifie `JELLYSEERR_API_KEY` |
| `Jellyseerr injoignable` | Vérifie `JELLYSEERR_URL` depuis le LXC : `curl $JELLYSEERR_URL/api/v1/status` |
| `erreur API Gemini (429)` | Quota gratuit dépassé : attends ou active la facturation |
| Le service ne démarre pas | `journalctl -u movomatic -n 50` affiche l'erreur de validation de configuration |
| Mauvaise œuvre identifiée | Teste avec `"dry_run": true` et `LOG_LEVEL=debug` ; augmente `GEMINI_MIN_CONFIDENCE` |

## Développement

```bash
git clone https://github.com/sudoPierre/movomatic.git && cd movomatic
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # puis renseigne les clés
pytest                 # tests unitaires (yt-dlp, Gemini et Jellyseerr sont mockés)
movomatic              # ou : uvicorn app.main:app --reload
```

Structure :

```text
app/
├── main.py        # FastAPI, routes et orchestration du pipeline
├── config.py      # Configuration (pydantic-settings)
├── models.py      # Schémas API + schéma de sortie Gemini
├── extractor.py   # yt-dlp : métadonnées et téléchargement temporaire
├── analyzer.py    # Gemini : analyse du texte puis de la vidéo
└── jellyseerr.py  # Recherche TMDB, scoring, détection anime, requête
deploy/
├── install_lxc.sh
└── movomatic.service
tests/test_api.py
```

## Sécurité

- **N'expose pas** le port 8000 directement sur Internet. Préfère un VPN (WireGuard, Tailscale) ou un reverse proxy HTTPS (Caddy, Nginx Proxy Manager, Traefik). Avec un proxy sur la même machine, mets `HOST=127.0.0.1`.
- La clé `MOVOMATIC_API_KEY` est générée aléatoirement (256 bits) par le script d'installation.
- Les secrets ne sont lisibles que par root et le groupe `movomatic` (`640`).
- Les vidéos analysées sont envoyées à l'API Google Gemini, puis supprimées après analyse.

## Licence

[MIT](LICENSE)
