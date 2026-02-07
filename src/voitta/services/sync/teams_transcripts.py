"""Fetch Teams meeting transcripts from .url files after SharePoint sync."""

import configparser
import json
import logging
from urllib.parse import parse_qs, quote, urlparse

import httpx

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def parse_meeting_url(url_content: str) -> dict | None:
    """Parse a Windows .url (INI) file and extract Teams meeting params.

    Returns dict with 'thread_id', 'organizer_id', 'tenant_id',
    or None if not a Teams meeting URL.
    """
    parser = configparser.RawConfigParser()
    parser.read_string(url_content)

    try:
        url = parser.get("InternetShortcut", "URL")
    except (configparser.NoSectionError, configparser.NoOptionError):
        return None

    parsed = urlparse(url)

    hostname = parsed.hostname or ""
    if "teams.microsoft.com" not in hostname:
        return None

    qs = parse_qs(parsed.query)
    thread_id = qs.get("threadId", [None])[0]
    organizer_id = qs.get("organizerId", [None])[0]
    tenant_id = qs.get("tenantId", [None])[0]

    if not thread_id or not organizer_id or not tenant_id:
        return None

    return {
        "thread_id": thread_id,
        "organizer_id": organizer_id,
        "tenant_id": tenant_id,
    }


def _build_join_web_url(thread_id: str, organizer_id: str, tenant_id: str) -> str:
    """Construct a Teams meeting JoinWebUrl from extracted params.

    Format: https://teams.microsoft.com/l/meetup-join/{threadId}/0?context={"Tid":"...","Oid":"..."}
    """
    encoded_thread = quote(thread_id, safe="")
    context = json.dumps({"Tid": tenant_id, "Oid": organizer_id}, separators=(",", ":"))
    encoded_context = quote(context, safe="")
    return f"https://teams.microsoft.com/l/meetup-join/{encoded_thread}/0?context={encoded_context}"


async def fetch_transcript(token: str, meeting: dict) -> str | None:
    """Fetch VTT transcript for a Teams meeting via Graph API.

    Uses JoinWebUrl filter (supported with delegated permissions on /me/).

    1. Construct JoinWebUrl and look up the meeting
    2. List transcripts for the meeting
    3. Download the first transcript as VTT

    Returns VTT content string or None if unavailable.
    """
    headers = {"Authorization": f"Bearer {token}"}
    join_url = _build_join_web_url(
        meeting["thread_id"], meeting["organizer_id"], meeting["tenant_id"]
    )

    async with httpx.AsyncClient() as client:
        # 1. Find the meeting by JoinWebUrl
        filter_expr = f"JoinWebUrl eq '{join_url}'"
        logger.debug("Meeting filter: %s", filter_expr)
        resp = await client.get(
            f"{GRAPH_BASE}/me/onlineMeetings",
            params={"$filter": filter_expr},
            headers=headers,
        )
        if resp.status_code != 200:
            logger.warning(
                "Failed to look up meeting (joinUrl): %s %s",
                resp.status_code, resp.text[:300],
            )
            return None

        meetings = resp.json().get("value", [])
        if not meetings:
            logger.debug("No meeting found for threadId=%s", meeting["thread_id"])
            return None

        meeting_id = meetings[0]["id"]

        # 2. List transcripts
        resp = await client.get(
            f"{GRAPH_BASE}/me/onlineMeetings/{meeting_id}/transcripts",
            headers=headers,
        )
        if resp.status_code != 200:
            logger.warning(
                "Failed to list transcripts for meeting %s: %s %s",
                meeting_id, resp.status_code, resp.text[:200],
            )
            return None

        transcripts = resp.json().get("value", [])
        if not transcripts:
            logger.debug("No transcripts for meeting %s", meeting_id)
            return None

        transcript_id = transcripts[0]["id"]

        # 3. Download VTT content
        resp = await client.get(
            f"{GRAPH_BASE}/me/onlineMeetings/{meeting_id}"
            f"/transcripts/{transcript_id}/content",
            headers={**headers, "Accept": "text/vtt"},
        )
        if resp.status_code != 200:
            logger.warning(
                "Failed to download transcript %s: %s %s",
                transcript_id, resp.status_code, resp.text[:200],
            )
            return None

        return resp.text


async def fetch_transcripts_for_folder(source, fs, token: str) -> int:
    """Scan a synced folder for .url files and fetch their Teams transcripts.

    Skips .url files that already have a matching .vtt file next to them.
    Returns number of transcripts fetched.
    """
    local_root = fs._resolve_path(source.folder_path)
    fetched = 0

    for url_file in local_root.rglob("*.url"):
        vtt_file = url_file.with_suffix(".vtt")
        if vtt_file.exists():
            continue

        try:
            content = url_file.read_text(encoding="utf-8")
        except Exception:
            try:
                content = url_file.read_text(encoding="latin-1")
            except Exception:
                continue

        meeting = parse_meeting_url(content)
        if not meeting:
            continue

        logger.info(
            "Fetching transcript for %s (thread=%s)",
            url_file.name, meeting["thread_id"],
        )

        try:
            vtt = await fetch_transcript(token, meeting)
        except Exception as e:
            logger.error("Error fetching transcript for %s: %s", url_file.name, e)
            continue

        if vtt:
            vtt_file.write_text(vtt, encoding="utf-8")
            fetched += 1
            logger.info("Saved transcript: %s", vtt_file.name)
        else:
            logger.debug("No transcript available for %s", url_file.name)

    return fetched
