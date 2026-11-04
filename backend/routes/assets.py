"""Serve locally cached HLTV images (team logos, player photos) to the UI."""

import re
from email.utils import formatdate

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from backend.services.image_cache import (
    backfill_images,
    cache_status,
    image_path,
    sniff_content_type,
    start_backfill,
)

router = APIRouter()

# Images are stable, versioned by the UI (?v=N) and only change when the
# harvester re-fetches a better copy. Cache them for an hour and hand out an
# ETag so anything past that is a cheap 304 instead of a full re-download —
# before this every page switch re-fetched 40-90 images (~1 MB) because the
# old `no-cache` response carried no validator at all.
_MAX_AGE_SECONDS = 3600


def _image_response(kind: str, key: int, request: Request) -> Response:
    path = image_path(kind, key)
    if not path:
        raise HTTPException(status_code=404, detail=f"No cached image for that {kind}")
    stat = path.stat()
    etag = f'W/"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    headers = {
        "Cache-Control": f"public, max-age={_MAX_AGE_SECONDS}",
        "ETag": etag,
        "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    data = path.read_bytes()
    return Response(content=data, media_type=sniff_content_type(data), headers=headers)


def _asset_id(value: str) -> int:
    """The numeric id of an asset path segment, with or without a file
    extension: the app asks for /assets/player/123.jpg so Cloudflare caches
    the image at its edge (it caches by extension, and cached requests do not
    count against the rate-limit rule)."""
    m = re.match(r"^(\d+)(?:\.[A-Za-z0-9]{1,5})?$", str(value or ""))
    if not m:
        raise HTTPException(status_code=404, detail="Unknown asset")
    return int(m.group(1))


@router.get("/team/{hltv_team_id}")
def team_logo(hltv_team_id: str, request: Request):
    return _image_response("team", _asset_id(hltv_team_id), request)


@router.get("/player/{player_id}")
def player_photo(player_id: str, request: Request):
    return _image_response("player", _asset_id(player_id), request)


@router.get("/status")
def assets_status():
    return cache_status()


@router.post("/backfill")
def assets_backfill(payload: dict | None = None):
    body = payload or {}
    if body.get("wait"):
        return backfill_images(force=bool(body.get("force")))
    return start_backfill(force=bool(body.get("force")))
