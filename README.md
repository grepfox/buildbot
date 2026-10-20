# Telegram Android Build Bot

A lightweight Telegram bot designed to trigger, track and manage Android  builds in real-time and upload build artifacts.

---

## Features

- **Real-Time Build Tracking**: Live-updates a single Telegram message with build progress, ninja stats, and time remaining.
- **Automated Artifact Uploads**: Streams finished builds over SCP/SFTP straight to a mirror host with real-time transfer progress, and generates public download links.
- **Queue Management**: Handles FIFO build queueing, cancellation, and concurrency control.
- **Device & ROM Matrix**: Configurable targets defined in `devices.json`.
- **System Monitoring**: Live host metrics (CPU, RAM, load averages, disk storage).
- **Interactive Storage Browser**: Browse and manage uploaded mirror files and folders directly via Telegram inline buttons.

---

## Setup & Installation

### 1. Prerequisites
- Docker & Docker Compose (or Python 3.10+ if running directly on the host)
- An SSH key pair for SFTP file uploads
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

### 2. Configuration
Copy the sample environment file to create your local `config.env`:

```bash
cp config.env.example config.env
```

Edit `config.env` with your credentials:
```env
# Telegram Bot Configuration
BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
ALLOWED_USERS=1234567890

# Mirror / Upload Configuration
MIRROR_URL=https://downloads.example.com/
SFTP_HOST=your.sftp.server.ip
SFTP_PORT=22
SFTP_USER=bot-uploader
SFTP_REMOTE_DIR=/files

# Optional Services & Intervals
MIRROR_KEY=
GOFILE_TOKEN=
EDIT_INTERVAL=10
```

### 3. SSH Keys for Uploads
Place the private key and known hosts used for SSH/SCP uploads in `.ssh/` inside the project root:
```bash
mkdir -p .ssh
chmod 600 .ssh/id_ed25519
```

### 4. Target Devices (`devices.json`)
Define your ROMs, build scripts, output directories, and zip matching patterns in `devices.json`:
```json
{
  "yaap": {
    "vayu": {
      "cwd": "~/y",
      "cmd": ["bash", "b"],
      "out_dir": "release-files",
      "zip_glob": "YAAP-*.zip",
      "variants": ["Vanilla", "Calypso"],
      "variant_re": "YAAP_VERSION=[^-]+-([A-Za-z]+)-"
    }
  }
}
```

---

## Running the Bot

### Option A: Using Docker Compose (Recommended)
You can deploy using the included script:
```bash
./deploy.sh
```
Or start manually via Docker Compose:
```bash
docker compose up -d --build
docker compose logs -f
```

### Option B: Systemd Service
To run as a systemd user service:
```bash
mkdir -p ~/.config/systemd/user/
cp buildbot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now buildbot.service
```

---

## Telegram Commands

- `/build <rom> <device>` — Start a new build or queue it if a build is active
- `/cancel` — Stop currently running build or cancel queue item (`/cancel <n>`)
- `/queue` — View queue status and current running build
- `/status` — Check bot health, ping, and uptime
- `/devices` — View list of configured ROMs and devices
- `/storage` — Interactive file manager for the remote mirror
- `/log` — Fetch build log tail
- `/id` — View your Telegram user ID
