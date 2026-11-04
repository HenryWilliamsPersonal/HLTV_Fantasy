"""Local cache of HLTV team logos and player photos.

The event pages we already archive (page_snapshots.db) embed, for every team in
a bracket, a logo URL (``teamLogo.dayLogoURL``) and, for every lineup player, a
body-shot URL (``pictureURL``) — both on the img-cdn.hltv.org CDN, which serves
plain images without the Cloudflare gate that fronts hltv.org pages. So the
pipeline is: scrape the URLs out of the stored snapshots, download each image
once into .runtime/assets/, and serve them from the backend. The UI falls back
to initials for anything not cached.
"""

import html as _htmlmod
import logging
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / ".runtime" / "assets"
TEAM_DIR = ASSETS_DIR / "team"
PLAYER_DIR = ASSETS_DIR / "player"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
_DOWNLOAD_TIMEOUT = 20
_backfill_lock = threading.Lock()
_backfill_state: Dict[str, Any] = {"running": False, "last_result": None, "last_run_at": None}

# Team objects in the bracket JSON: the logo search is bounded by the NEXT
# team object's start — a plain length-bounded gap can slide past a team that
# has no logo of its own into its opponent's dayLogoURL (how TYLOO briefly
# wore 1win's logo). Player photos sit in a tightly-anchored id/nick/picture
# sequence, which can't cross objects.
# Atomic team-object match: id through its own teamLogo block in one pattern,
# so a team without a logo can never inherit URLs from unrelated JSON further
# down the page (which is how BIG briefly wore another logo).
_TEAM_OBJ_LOGO_RE = re.compile(
    r'"team":\{"id":(\d+),"name":"[^"]*","profileURL":"[^"]*",'
    r'"teamLogo":\{"dayLogoURL":"([^"]*)"(?:,"nightLogoURL":"([^"]*)")?'
)
_IMG_TAG_RE = re.compile(r"<img[^>]+>")
_PLAYER_PHOTO_RE = re.compile(r'"id":(\d+),"nick":"[^"]+","profileURL":"[^"]*","pictureURL":"([^"]+)"')
# HTML-side images: a CDN image is only attributed to a team/player when it
# sits INSIDE that entity's own <a href="/team|player/{id}/..."> anchor —
# nearest-preceding-link matching mis-pairs logos on layouts that interleave
# links and images (TYLOO's link followed by 1win's img). Different pages
# embed different signed size variants, so the harvester keeps the biggest
# per id, with the bracket-JSON source always outranking HTML finds.
_TEAM_ANCHOR_RE = re.compile(r'href="/team/(\d+)/')
_PLAYER_ANCHOR_RE = re.compile(r'href="/player/(\d+)/')
_SIZE_PARAM_RE = re.compile(r'[?&](?:w|h)=(\d+)')


def _url_size(url: str) -> int:
    """Effective quality of a signed CDN variant: SVG logos are vectors and
    beat any raster; otherwise the largest w=/h= param (the signature covers
    the params, so this is also the size we'd receive). 0 = unfetchable."""
    base = url.split("?", 1)[0]
    if base.lower().endswith(".svg") and "s=" in url:
        return 1000
    sizes = [int(x) for x in _SIZE_PARAM_RE.findall(url)]
    return max(sizes) if sizes else 0


def _ensure_dirs() -> None:
    TEAM_DIR.mkdir(parents=True, exist_ok=True)
    PLAYER_DIR.mkdir(parents=True, exist_ok=True)


def image_path(kind: str, key: int) -> Optional[Path]:
    """Cached image file for a team (kind='team', HLTV team id) or player
    (kind='player', HLTV player id); None when not cached."""
    base = TEAM_DIR if kind == "team" else PLAYER_DIR
    p = base / str(int(key))
    return p if p.is_file() and p.stat().st_size > 0 else None


def sniff_content_type(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if b"<svg" in data[:256].lower():
        return "image/svg+xml"
    return "application/octet-stream"


def _download(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
            data = resp.read()
        if not data:
            return False
        dest.write_bytes(data)
        return True
    except Exception as exc:
        logger.info("Image download failed for %s: %s", url, exc)
        return False


def extract_urls_from_snapshots() -> Dict[str, Dict[int, Dict[str, Any]]]:
    """Best (largest signed variant) team-logo and player-photo URL per id,
    harvested from every archived page: the bracket JSON objects plus any HTML
    <img> whose CDN url follows a /team/{id}/ or /player/{id}/ link."""
    from backend.data.page_snapshots import list_snapshot_urls, get_page_snapshot

    best: Dict[str, Dict[int, Dict[str, Any]]] = {"team": {}, "player": {}}

    def consider(kind: str, key: int, url: str, trust: int, night: int = 0) -> None:
        if "teamplaceholder" in url or "playerplaceholder" in url:
            return
        size = _url_size(url)
        if size <= 0:
            return  # unsigned/paramless variants 403 on the CDN
        cur = best[kind].get(key)
        # The UI is dark, so HLTV's night variant (inverted where a logo is
        # dark-on-transparent, e.g. BIG) beats the day variant regardless of
        # size; attribution trust still dominates everything.
        rank = (trust, night, size)
        if cur is None or rank > (cur["trust"], cur["night"], cur["size"]):
            best[kind][key] = {"url": url, "size": size, "trust": trust, "night": night}

    for page_url in list_snapshot_urls():
        snap = get_page_snapshot(page_url)
        if not snap or not snap.get("html"):
            continue
        un = _htmlmod.unescape(snap["html"])
        # Bracket-JSON objects pair id and image explicitly — highest trust.
        for m in _TEAM_OBJ_LOGO_RE.finditer(un):
            tid = int(m.group(1))
            if m.group(2):
                consider("team", tid, m.group(2), trust=2, night=0)
            if m.group(3):
                consider("team", tid, m.group(3), trust=2, night=1)
        for m in _PLAYER_PHOTO_RE.finditer(un):
            consider("player", int(m.group(1)), m.group(2), trust=2)
        # Anchored HTML pass: only images inside the entity's own <a> tag.
        # Every CDN url in the tag counts (srcset carries 2x variants at
        # double resolution), each ranked by its own size.
        for m in _TEAM_ANCHOR_RE.finditer(un):
            close = un.find("</a>", m.end(), m.end() + 2000)
            if close < 0:
                continue
            for tag_m in _IMG_TAG_RE.finditer(un, m.end(), close):
                tag = tag_m.group(0)
                night = 1 if "night-only" in tag else 0
                for u in re.findall(r'https://img-cdn\.hltv\.org/teamlogo/[^"\s,]+', tag):
                    consider("team", int(m.group(1)), u, trust=1, night=night)
        for m in _PLAYER_ANCHOR_RE.finditer(un):
            close = un.find("</a>", m.end(), m.end() + 1500)
            if close < 0:
                continue
            for tag_m in _IMG_TAG_RE.finditer(un, m.end(), close):
                for u in re.findall(r'https://img-cdn\.hltv\.org/playerbodyshot/[^"\s,]+', tag_m.group(0)):
                    consider("player", int(m.group(1)), u, trust=1)
    return best


def _manifest_path() -> Path:
    return ASSETS_DIR / "manifest.json"


def _load_manifest() -> Dict[str, Any]:
    import json

    try:
        return json.loads(_manifest_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_manifest(manifest: Dict[str, Any]) -> None:
    import json

    _manifest_path().write_text(json.dumps(manifest), encoding="utf-8")


def backfill_images(force: bool = False) -> Dict[str, Any]:
    """Scan snapshots and download the best variant of every image. A cached
    file is re-downloaded when a larger signed variant shows up (the manifest
    records which URL each file came from). Safe to re-run."""
    _ensure_dirs()
    found = extract_urls_from_snapshots()
    manifest = _load_manifest()
    result = {"teams_found": len(found["team"]), "players_found": len(found["player"]),
              "downloaded": 0, "upgraded": 0, "failed": 0, "skipped": 0, "purged": 0}
    # Purge cached files whose id has NO candidate in the current (stricter)
    # harvest — leftovers from earlier looser attribution passes; showing an
    # initials fallback beats showing another team's logo.
    for kind, base in (("team", TEAM_DIR), ("player", PLAYER_DIR)):
        for f in base.iterdir():
            if not f.is_file():
                continue
            try:
                key = int(f.name)
            except ValueError:
                continue
            if key not in found[kind]:
                f.unlink(missing_ok=True)
                manifest.pop(f"{kind}:{key}", None)
                result["purged"] += 1
    for kind, base in (("team", TEAM_DIR), ("player", PLAYER_DIR)):
        for key, cand in found[kind].items():
            dest = base / str(key)
            mkey = f"{kind}:{key}"
            cached = dest.is_file() and dest.stat().st_size > 0
            same_url = manifest.get(mkey, {}).get("url") == cand["url"]
            if cached and same_url and not force:
                result["skipped"] += 1
                continue
            if _download(cand["url"], dest):
                manifest[mkey] = {"url": cand["url"], "size": cand["size"]}
                result["upgraded" if cached else "downloaded"] += 1
            else:
                result["failed"] += 1
    _save_manifest(manifest)
    return result


def start_backfill(force: bool = False) -> Dict[str, Any]:
    """Kick a background backfill (only one at a time); returns current state."""
    with _backfill_lock:
        if _backfill_state["running"]:
            return dict(_backfill_state)
        _backfill_state["running"] = True

    def _run() -> None:
        try:
            result = backfill_images(force=force)
            with _backfill_lock:
                _backfill_state["last_result"] = result
                _backfill_state["last_run_at"] = time.time()
        except Exception as exc:
            with _backfill_lock:
                _backfill_state["last_result"] = {"error": str(exc)}
                _backfill_state["last_run_at"] = time.time()
        finally:
            with _backfill_lock:
                _backfill_state["running"] = False

    threading.Thread(target=_run, name="image-backfill", daemon=True).start()
    return dict(_backfill_state)


def cache_status() -> Dict[str, Any]:
    _ensure_dirs()
    with _backfill_lock:
        state = dict(_backfill_state)
    return {
        "teams_cached": sum(1 for p in TEAM_DIR.iterdir() if p.is_file()),
        "players_cached": sum(1 for p in PLAYER_DIR.iterdir() if p.is_file()),
        **state,
    }
