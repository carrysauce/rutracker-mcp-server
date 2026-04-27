"""
RuTracker MCP Server

A Model Context Protocol server for searching and downloading torrents
from RuTracker using the py-rutracker-client library and FastMCP.
"""

import asyncio
import base64
import os
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastmcp import FastMCP, Context
from bs4 import BeautifulSoup
from fastmcp.tools.base import ToolResult
from mcp.types import BlobResourceContents, EmbeddedResource, TextContent
from pydantic import AnyUrl, TypeAdapter, UrlConstraints

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

RESOURCE_URI_ADAPTER = TypeAdapter(Annotated[AnyUrl, UrlConstraints(host_required=False)])


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
        "Use search_torrents to find content, get_torrent_info to read the description and quality/codec details, "
        "then download_torrent to get the .torrent file as an MCP file attachment. "
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
      - title_url: topic page URL
      - magnet_url: magnet URI when provided by the library
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


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "topic_id": {"type": "integer"},
            "filename": {"type": "string"},
            "size_bytes": {"type": "integer"},
            "mime_type": {"type": "string"},
            "delivery": {"type": "string"},
        },
        "required": ["topic_id", "filename", "size_bytes", "mime_type", "delivery"],
        "additionalProperties": False,
    }
)
async def download_torrent(
    topic_id: Annotated[int, "RuTracker topic/torrent ID (integer)"],
    ctx: Context = None,
) -> ToolResult:
    """
    Download a .torrent file from RuTracker by topic ID.

    Returns:
      - embedded MCP file attachment with the .torrent bytes
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

    size_bytes = len(torrent_bytes)
    base_name = f"rutracker_{topic_id}"
    filename = f"{base_name}.torrent"
    mime_type = "application/x-bittorrent"
    uri = RESOURCE_URI_ADAPTER.validate_python(
        f"rutracker://download/{topic_id}/{filename}"
    )

    if ctx:
        await ctx.report_progress(100, 100, "Complete")
        await ctx.info(f"Torrent ready ({size_bytes} bytes).")

    return ToolResult(
        content=[
            TextContent(type="text", text=f"Attached {filename} as an MCP file resource."),
            EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri=uri,
                    mimeType=mime_type,
                    blob=base64.b64encode(torrent_bytes).decode("ascii"),
                ),
            ),
        ],
        structured_content={
            "topic_id": topic_id,
            "filename": filename,
            "size_bytes": size_bytes,
            "mime_type": mime_type,
            "delivery": "embedded_resource",
        },
    )


@mcp.tool()
async def get_torrent_info(
    topic_id: Annotated[int, "RuTracker topic/torrent ID (integer)"],
    ctx: Context = None,
) -> dict[str, Any]:
    """
    Fetch detailed information about a torrent from its RuTracker topic page.

    Parses the forum topic to extract:
      - title: full torrent title
      - topic_id: the topic ID
      - topic_url: URL to the forum topic page
      - download_url: direct .torrent download URL
      - category: forum category name
      - description: full post body text with technical specs, quality notes, etc.
      - description_html: raw HTML of the post body (preserves formatting)
      - poster: username of the uploader
      - poster_url: profile URL of the uploader
      - magnet_link: magnet URI if present in the post

    This is the primary tool for getting quality/codec/resolution info before downloading.
    """
    if ctx:
        await ctx.info(f"Fetching topic info for ID {topic_id}…")
        await ctx.report_progress(0, 100, "Fetching topic page")

    client = _client(ctx)
    base = "https://rutracker.org/forum"
    topic_url = f"{base}/viewtopic.php?t={topic_id}"
    download_url = f"{base}/dl.php?t={topic_id}"

    try:
        async with client.session.get(
            topic_url,
            ssl=client._ssl_context,
            proxy=client.proxy,
        ) as response:
            if response.status != 200:
                raise RuTrackerRequestError(
                    f"Topic page returned HTTP {response.status}"
                )
            html = await response.text(encoding="utf-8", errors="replace")
    except RuTrackerException:
        raise
    except Exception as exc:
        raise RuTrackerRequestError(f"Failed to fetch topic page: {exc}") from exc

    if ctx:
        await ctx.report_progress(70, 100, "Parsing page")

    soup = BeautifulSoup(html, "html.parser")

    # Title
    title_tag = soup.find("h1", class_="maintitle") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    # Category
    category = ""
    cat_tag = soup.find("td", id="nav-top")
    if cat_tag:
        links = cat_tag.find_all("a")
        if links:
            category = links[-1].get_text(strip=True)

    # First post body — contains description, specs, quality info
    post_body = soup.find("div", class_="post_body")
    description_html = ""
    description = ""
    if post_body:
        description_html = str(post_body)
        # Convert to readable text: replace <br> with newlines, strip tags
        for br in post_body.find_all("br"):
            br.replace_with("\n")
        description = post_body.get_text(separator="\n").strip()
        # Collapse excessive blank lines
        description = re.sub(r"\n{3,}", "\n\n", description)

    # Poster (uploader)
    # Primary selector: RuTracker uses class="poster_nick" on the author link.
    # Fallback: some themes use a generic "nick" class inside the post header.
    poster = ""
    poster_url = ""
    poster_tag = soup.find("a", class_="poster_nick")
    if not poster_tag:
        poster_tag = soup.find("a", attrs={"class": re.compile(r"\bnick\b")})
    if poster_tag:
        poster = poster_tag.get_text(strip=True)
        href = poster_tag.get("href", "")
        poster_url = f"{base}/{href}" if href and not href.startswith("http") else href

    # Magnet link
    magnet_link = ""
    magnet_tag = soup.find("a", href=re.compile(r"^magnet:"))
    if magnet_tag:
        magnet_link = magnet_tag["href"]

    if ctx:
        await ctx.report_progress(100, 100, "Done")
        await ctx.info(f"Info fetched for topic {topic_id}: '{title}'")

    return {
        "topic_id": topic_id,
        "title": title,
        "topic_url": topic_url,
        "download_url": download_url,
        "category": category,
        "poster": poster,
        "poster_url": poster_url,
        "magnet_link": magnet_link,
        "description": description,
        "description_html": description_html,
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
