# Monitoring System

Lightweight distributed monitoring: stock checkers + RSS alerts → Telegram / Email.

## Architecture

```
[Your Mac/PC]                     [VPS]
config_builder.py  →  config.yaml ──SFTP──→  worker.py
   (tkinter GUI)         ↑                   (headless daemon)
   no pip deps      export button            curl_cffi + apprise
```

---

## Quick Start

### 1. Local GUI (browser-only, no install needed)

Open the standalone visual builder directly in your browser:

```text
config_builder.html
```

- No Python / pip / Node needed
- Double-click the file to open it in your default browser
- Edit targets / RSS / Telegram config visually
- Click `导出 config.yaml`

### 2. Local Python GUI (optional)

```bash
python config_builder.py
```

- Add your Telegram bot URL (`tgram://TOKEN/CHAT_ID`)
- Add VPS product pages and RSS feeds
- Click **Export config.yaml**
- SFTP the file to your VPS

---

### 3. VPS Worker — bare Python

```bash
# Install deps
pip install -r requirements.txt

# Place config.yaml in same directory as worker.py, then:
python worker.py
```

Run as a systemd service for persistence:

```ini
# /etc/systemd/system/monitor.service
[Unit]
Description=Monitoring Worker
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/monitor/worker.py
WorkingDirectory=/opt/monitor
Restart=always
RestartSec=10
Environment=CONFIG_PATH=/opt/monitor/config.yaml
Environment=DB_PATH=/opt/monitor/history.db

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable --now monitor
journalctl -fu monitor
```

---

### 4. VPS Worker — Docker

```bash
# Build
docker build -t monitor-worker .

# Run (mount a data directory containing config.yaml)
docker run -d \
  --name monitor \
  --restart unless-stopped \
  -v /opt/monitor/data:/app/data \
  monitor-worker

# Update config without rebuilding:
# 1. Edit config.yaml locally in config_builder.py
# 2. sftp> put config.yaml /opt/monitor/data/config.yaml
# 3. Worker auto-detects file change within 60s — no restart needed
```

---

## Getting Your Telegram Bot URL

1. Message `@BotFather` on Telegram → `/newbot`
2. Copy the **token** (format: `123456:ABC-DEF...`)
3. Start a chat with your new bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
4. Find your `chat.id` in the response
5. Your apprise URL: `tgram://TOKEN/CHAT_ID`

---

## Detection Modes

| Mode | How it works | Use case |
|------|-------------|----------|
| `selector_disappears` | Alert when CSS selector vanishes | "Out of Stock" badge gone |
| `selector_appears`    | Alert when CSS selector appears  | "Add to Cart" button shows up |
| `content_hash`        | Alert on ANY visible text change | Catch-all for unknown layouts |

---

## Finding CSS Selectors

1. Open the product page in Chrome
2. Right-click the "Out of Stock" text → Inspect
3. In DevTools, right-click the element → Copy → Copy selector
4. Paste into the GUI's CSS Selector field

Common patterns:
```css
.out-of-stock
.stock-status
[data-stock="0"]
button[disabled]
.sold-out
```

---

## SQLite (history.db)

The worker writes **only dedup keys** to SQLite — no personal data, no scraped content.
- `seen` table: URL/GUID hashes to prevent duplicate alerts
- `last_heartbeat` table: timestamp of last heartbeat notification

The database is safe to delete at any time; the worker will recreate it and 
re-learn state from scratch (you may get one round of "new" alerts for existing items).
