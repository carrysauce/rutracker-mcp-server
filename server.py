"""
RuTracker MCP Server

A Model Context Protocol server for searching and downloading torrents
from RuTracker using the py-rutracker-client library and FastMCP.
"""

import asyncio
import base64
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP, Context

# Load .env file if present (optional convenience for local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from py_rutracker import AsyncRuTrackerClient
from py_rutracker.exceptions import (
    RuTrackerAuthError,
    RuTrackerDownloadError,
    RuTrackerException,
    RuTrackerRequestError,
)


# ---------------------------------------------------------------------------
# Lifespan: create and share a single authenticated async client
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(server: FastMCP):
    """Create the RuTracker async client once per server lifetime."""
    login = os.environ.get("RUTRACKER_LOGIN")
    password = os.environ.get("RUTRACKER_PASSWORD")
    proxy = os.environ.get("RUTRACKER_PROXY")

    if not login or not password:
        raise RuntimeError(
            "RUTRACKER_LOGIN and RUTRACKER_PASSWORD environment variables must be set."
        )

    client = AsyncRuTrackerClient(login=login, password=password, proxy=proxy or None)
    await client.init()

    try:
        yield {"rutracker_client": client}
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Server definition
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="RuTracker MCP Server",
    instructions=(
        "Search and download torrents from RuTracker. "
        "Use search_torrents to find content, then download_torrent to get the .torrent file. "
        "Credentials are read from RUTRACKER_LOGIN and RUTRACKER_PASSWORD environment variables."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _client(ctx: Context) -> AsyncRuTrackerClient:
    """Retrieve the shared async client from the lifespan context."""
    return ctx.lifespan_context["rutracker_client"]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_torrents(
    query: Annotated[str, "Search keywords (title, author, etc.)"],
    page: Annotated[int, "Page number to retrieve (1-indexed, 50 results per page)"] = 1,
    ctx: Context = None,
) -> list[dict[str, Any]]:
    """
    Search RuTracker for torrents matching the given query on a specific page.

    Returns a list of torrent records with fields:
      - topic_id: unique torrent/topic ID on RuTracker
      - title: torrent title
      - category / category_url: forum category and its URL
      - author / author_url: uploader name and profile URL
      - size / unit: file size and unit (GB, MB, etc.)
      - download_url: direct URL to the .torrent file page
      - seedmed / leechmed: current seeders and leechers
      - download_counter: total number of downloads
      - added: date the torrent was added
      - approved: moderation status
    """
    if ctx:
        await ctx.info(f"Searching RuTracker for '{query}' (page {page})…")

    client = _client(ctx)

    try:
        results = await client.search(query, page=page, return_search_dict=True)
    except RuTrackerRequestError as exc:
        raise ValueError(f"Search request failed: {exc}") from exc
    except RuTrackerException as exc:
        raise ValueError(f"RuTracker error: {exc}") from exc

    if ctx:
        await ctx.info(f"Found {len(results)} result(s) on page {page}.")

    return results


@mcp.tool()
async def search_all_pages(
    query: Annotated[str, "Search keywords (title, author, etc.)"],
    max_pages: Annotated[int, "Maximum number of pages to fetch (default 10, max 10)"] = 10,
    ctx: Context = None,
) -> list[dict[str, Any]]:
    """
    Search RuTracker across multiple pages and return all matching torrents.

    Fetches pages in parallel (up to max_pages, each page returns up to 50 results).
    Reports streaming progress as each page is fetched.

    Returns a combined list of torrent records (same schema as search_torrents).
    """
    max_pages = min(max(1, max_pages), 10)

    if ctx:
        await ctx.info(f"Searching all pages for '{query}' (up to {max_pages} page(s))…")
        await ctx.report_progress(0, max_pages, "Starting multi-page search")

    client = _client(ctx)

    async def fetch_page(page: int) -> list[dict]:
        try:
            return await client.search(query, page=page, return_search_dict=True)
        except RuTrackerException:
            return []

    tasks = [fetch_page(p) for p in range(1, max_pages + 1)]

    all_results: list[dict] = []
    completed = 0
    for coro in asyncio.as_completed(tasks):
        page_results = await coro
        all_results.extend(page_results)
        completed += 1
        if ctx:
            await ctx.report_progress(
                completed, max_pages, f"Fetched {completed}/{max_pages} page(s)"
            )

    if ctx:
        await ctx.info(f"Multi-page search complete. Total results: {len(all_results)}.")

    return all_results


@mcp.tool()
async def download_torrent(
    topic_id: Annotated[int, "RuTracker topic/torrent ID (integer)"],
    save_path: Annotated[
        str,
        "Optional file path where the .torrent file should be saved. "
        "If omitted the file content is returned as a Base64-encoded string.",
    ] = "",
    ctx: Context = None,
) -> dict[str, Any]:
    """
    Download a .torrent file from RuTracker by topic ID.

    If save_path is provided, the file is written to that path and the tool
    returns a confirmation dict with 'saved_to' and 'size_bytes' keys.

    If save_path is omitted, the tool returns:
      - content_base64: Base64-encoded .torrent file bytes
      - size_bytes: size of the torrent file in bytes
      - filename: suggested filename for the torrent
    """
    if ctx:
        await ctx.info(f"Downloading torrent file for topic ID {topic_id}…")
        await ctx.report_progress(0, 100, "Initiating download")

    client = _client(ctx)

    try:
        torrent_bytes = await client.download(topic_id)
    except RuTrackerDownloadError as exc:
        raise ValueError(f"Download failed: {exc}") from exc
    except RuTrackerRequestError as exc:
        raise ValueError(f"Download request failed: {exc}") from exc
    except RuTrackerException as exc:
        raise ValueError(f"RuTracker error: {exc}") from exc

    if ctx:
        await ctx.report_progress(80, 100, "File received, processing")

    size_bytes = len(torrent_bytes)
    filename = f"rutracker_{topic_id}.torrent"

    if save_path:
        dest = Path(save_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(torrent_bytes)
        if ctx:
            await ctx.report_progress(100, 100, "Saved to disk")
            await ctx.info(f"Torrent saved to '{dest}' ({size_bytes} bytes).")
        return {"saved_to": str(dest.resolve()), "size_bytes": size_bytes, "filename": filename}

    content_b64 = base64.b64encode(torrent_bytes).decode("ascii")
    if ctx:
        await ctx.report_progress(100, 100, "Complete")
        await ctx.info(f"Torrent ready ({size_bytes} bytes).")

    return {
        "content_base64": content_b64,
        "size_bytes": size_bytes,
        "filename": filename,
    }


@mcp.tool()
async def get_topic_url(
    topic_id: Annotated[int, "RuTracker topic/torrent ID (integer)"],
    ctx: Context = None,
) -> dict[str, str]:
    """
    Return the RuTracker forum page URL and the direct download URL for a given topic ID.

    Useful for building links to share or open in a browser / torrent client.
    """
    base = "https://rutracker.org/forum"
    topic_url = f"{base}/viewtopic.php?t={topic_id}"
    download_url = f"{base}/dl.php?t={topic_id}"

    if ctx:
        await ctx.info(f"Topic URL for {topic_id}: {topic_url}")

    return {
        "topic_id": str(topic_id),
        "topic_url": topic_url,
        "download_url": download_url,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="RuTracker MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
        help="Transport mode (default: stdio for local Claude Desktop use)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP transports")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transports")
    args = parser.parse_args()

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    elif args.transport == "sse":
        mcp.run(transport="sse", host=args.host, port=args.port)
    else:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
