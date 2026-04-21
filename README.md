# rutracker-mcp-server

A **Model Context Protocol (MCP) server** for [RuTracker](https://rutracker.org) — the popular Russian torrent tracker.

Built with [FastMCP](https://github.com/PrefectHQ/fastmcp) and [py-rutracker-client](https://pypi.org/project/py-rutracker-client/), this server exposes RuTracker search and download functionality as MCP tools that can be used directly by Claude (Desktop or API) and other MCP-compatible clients.

---

## Features

| Tool | Description |
|------|-------------|
| `search_torrents` | Search RuTracker by keyword on a specific page (50 results/page) |
| `search_all_pages` | Parallel multi-page search with streaming progress updates |
| `get_torrent_info` | Fetch full description, quality/codec details, and metadata from a topic page |
| `download_torrent` | Download a `.torrent` file by topic ID — returns Base64-encoded content |
| `get_topic_url` | Get the forum page URL and direct download URL for a topic ID |

All long-running tools use `ctx.report_progress()` and `ctx.info()` for **streaming progress feedback** in compatible clients.

---

## Requirements

- Python 3.11+
- A valid [RuTracker](https://rutracker.org) account
- `pip install -r requirements.txt`

---

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/notnooblord/rutracker-mcp-server.git
cd rutracker-mcp-server

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure credentials
cp .env.example .env
# Edit .env and fill in RUTRACKER_LOGIN and RUTRACKER_PASSWORD
```

---

## Configuration

Set the following environment variables (or use a `.env` file with `python-dotenv`):

| Variable | Required | Description |
|---|---|---|
| `RUTRACKER_LOGIN` | ✅ | Your RuTracker username |
| `RUTRACKER_PASSWORD` | ✅ | Your RuTracker password |
| `RUTRACKER_PROXY` | ❌ | HTTP/SOCKS proxy URL (e.g. `socks5://127.0.0.1:1080`) |

---

## Running the Server

### stdio (Claude Desktop)

```bash
RUTRACKER_LOGIN=user RUTRACKER_PASSWORD=pass python server.py --transport stdio
```

### Streamable HTTP (remote / web clients)

```bash
RUTRACKER_LOGIN=user RUTRACKER_PASSWORD=pass python server.py --transport streamable-http --host 0.0.0.0 --port 8000
```

### SSE (legacy HTTP streaming)

```bash
RUTRACKER_LOGIN=user RUTRACKER_PASSWORD=pass python server.py --transport sse --host 0.0.0.0 --port 8000
```

---

## Self-Hosting with Docker

### Build locally

```bash
docker build -t rutracker-mcp .
docker run -e RUTRACKER_LOGIN=user -e RUTRACKER_PASSWORD=pass -p 8000:8000 rutracker-mcp
```

Point your MCP client at `http://localhost:8000/mcp`.

### GitHub Actions — automated builds

The repository ships a GitHub Actions workflow (`.github/workflows/docker-build.yml`) that automatically builds and pushes the Docker image to the **GitHub Container Registry (GHCR)** on every push to `main`.

The image is published at:

```
ghcr.io/<your-github-username>/rutracker-mcp-server:latest
```

To pull and run the pre-built image on your server:

```bash
# Pull the latest image
docker pull ghcr.io/<your-github-username>/rutracker-mcp-server:latest

# Run it
docker run -d \
  -e RUTRACKER_LOGIN=user \
  -e RUTRACKER_PASSWORD=pass \
  -p 8000:8000 \
  ghcr.io/<your-github-username>/rutracker-mcp-server:latest
```

The workflow tags each image with `latest` (on the default branch) and the short commit SHA, so you can pin to a specific build if needed.

---

## Claude Desktop Integration

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "rutracker": {
      "command": "python",
      "args": ["/absolute/path/to/server.py", "--transport", "stdio"],
      "env": {
        "RUTRACKER_LOGIN": "your_username",
        "RUTRACKER_PASSWORD": "your_password"
      }
    }
  }
}
```

---

## Tool Reference

### `search_torrents`

Search for torrents on page N.

**Parameters:**
- `query` (str) — search keywords
- `page` (int, default `1`) — page number (50 results per page)

**Returns:** list of torrent objects with `topic_id`, `title`, `category`, `author`, `size`, `unit`, `download_url`, `seedmed`, `leechmed`, `download_counter`, `added`.

---

### `search_all_pages`

Parallel multi-page search with real-time progress streaming.

**Parameters:**
- `query` (str) — search keywords
- `max_pages` (int, default `10`) — number of pages to fetch in parallel (max 10)

**Returns:** combined list of all matching torrent objects.

---

### `download_torrent`

Download a `.torrent` file.

**Parameters:**
- `topic_id` (int) — RuTracker topic/torrent ID

**Returns:** `{"content_base64": "...", "size_bytes": 12345, "filename": "rutracker_12345.torrent"}`

The Base64 content can be decoded by the client or passed directly to a BitTorrent client API.

---

### `get_torrent_info`

Fetch full metadata from a torrent's topic page, including quality/codec/resolution details typically found in the uploader's description.

**Parameters:**
- `topic_id` (int) — RuTracker topic/torrent ID

**Returns:**
- `title` — full torrent title
- `category` — forum category
- `poster` / `poster_url` — uploader name and profile link
- `description` — full post text (technical specs, quality notes, plot, etc.)
- `description_html` — raw HTML of the post body
- `magnet_link` — magnet URI if present
- `topic_url` / `download_url` — page and direct download URLs

---

### `get_topic_url`

Get page and download URLs for a topic.

**Parameters:**
- `topic_id` (int) — RuTracker topic/torrent ID

**Returns:** `{"topic_id": 12345, "topic_url": "https://...", "download_url": "https://..."}`

---

## Notes

- RuTracker may require solving a CAPTCHA on first login or after inactivity. If authentication fails, log in via a browser on the same IP first.
- This server downloads **only `.torrent` metadata files**, not the actual torrent content. Use a BitTorrent client (qBittorrent, Transmission, etc.) to download the content.
- The server authenticates once at startup and reuses the session for the lifetime of the process.

