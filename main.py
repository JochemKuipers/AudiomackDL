import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests
from tqdm import tqdm
from dotenv import load_dotenv
from platformdirs import user_downloads_dir


load_dotenv()

RAPIDAPI_BASE = "https://audiomack-scraper.p.rapidapi.com"


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v else default


def _rapidapi_headers() -> dict[str, str]:
    key = _env("RAPIDAPI_KEY")
    if not key:
        raise SystemExit(
            "Missing RAPIDAPI_KEY env var. Example (PowerShell):\n"
            '$env:RAPIDAPI_KEY="YOUR_KEY"\n'
        )
    host = _env("RAPIDAPI_HOST", "audiomack-scraper.p.rapidapi.com") or "audiomack-scraper.p.rapidapi.com"
    return {
        "x-rapidapi-key": key,
        "x-rapidapi-host": host,
        "Content-Type": "application/json",
    }


def _safe_name(s: str, max_len: int = 180) -> str:
    s = (s or "").strip()
    s = re.sub(r"[\\/:*?\"<>|]", "_", s)  # Windows-illegal characters
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        s = "unknown"
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip()
    return s


def _guess_url_kind(url: str) -> str | None:
    # Audiomack URLs often look like:
    # - /song/
    # - /album/
    # - /playlist/
    lowered = url.lower()
    if "/song/" in lowered:
        return "song"
    if "/album/" in lowered:
        return "album"
    if "/playlist/" in lowered:
        return "playlist"
    return None


def _unwrap_results(data: Any) -> Any:
    """
    Some RapidAPI responses wrap the payload like:
      {"results": {...}, ...}
    while others return the payload directly.
    """
    if isinstance(data, dict) and "results" in data and data["results"] is not None:
        return data["results"]
    return data


def _rapidapi_get(path: str, *, params: dict[str, str] | None = None, timeout_s: int = 60) -> Any:
    url = f"{RAPIDAPI_BASE}{path}"
    r = requests.get(url, headers=_rapidapi_headers(), params=params, timeout=timeout_s)
    if r.status_code >= 400:
        body = ""
        try:
            body = str(r.json())
        except Exception:
            body = (r.text or "").strip()

        hint = ""
        if r.status_code == 401:
            hint = "\nHint: RapidAPI key is missing/invalid (check RAPIDAPI_KEY)."
        elif r.status_code == 403:
            hint = (
                "\nHint: RapidAPI returned Forbidden. Common causes are: endpoint not enabled on your plan, "
                "quota exceeded, key restrictions, or the API blocking this content."
            )
        raise requests.HTTPError(
            f"{r.status_code} {r.reason} for {r.url}{hint}\n{body}".strip(),
            response=r,
        )

    try:
        return _unwrap_results(r.json())
    except Exception as e:
        raise RuntimeError(f"Non-JSON response from API for {r.url}") from e


def _extract_song_ids(obj: Any) -> set[int]:
    """
    Heuristic extraction for playlist payloads (which may differ from album).
    - Always trust explicit `song_id`
    - For dicts that look like a "song", also accept `id`
    """
    ids: set[int] = set()

    def looks_like_song(d: dict[str, Any]) -> bool:
        t = str(d.get("type") or "").lower()
        if t == "song":
            return True
        # fallback: typical song fields
        return all(k in d for k in ("title", "artist", "duration"))

    stack: list[Any] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if "song_id" in cur and isinstance(cur["song_id"], int):
                ids.add(cur["song_id"])
            if "id" in cur and isinstance(cur["id"], int) and looks_like_song(cur):
                ids.add(cur["id"])
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return ids


@dataclass(frozen=True)
class SongToDownload:
    song_id: int
    title: str
    artist: str


def _get_song_from_url(song_url: str) -> SongToDownload:
    data = _rapidapi_get("/audiomack/song", params={"url": song_url})
    song_id = int(data["id"])
    return SongToDownload(
        song_id=song_id,
        title=str(data.get("title") or f"song-{song_id}"),
        artist=str(data.get("artist") or "unknown-artist"),
    )


def _get_album_songs(album_url: str) -> tuple[str, str, list[SongToDownload]]:
    data = _rapidapi_get("/audiomack/album", params={"url": album_url})
    album_title = str(data.get("title") or "unknown-album")
    album_artist = str(data.get("artist") or "unknown-artist")
    tracks = data.get("tracks") or []

    songs: list[SongToDownload] = []
    for t in tracks:
        if not isinstance(t, dict):
            continue
        sid = t.get("song_id")
        if isinstance(sid, int):
            songs.append(
                SongToDownload(
                    song_id=sid,
                    title=str(t.get("title") or f"song-{sid}"),
                    artist=str(t.get("artist") or album_artist),
                )
            )
    if not songs:
        raise SystemExit("No tracks found in album response.")
    return album_title, album_artist, songs


def _get_playlist_songs(playlist_url: str) -> tuple[str, str, list[SongToDownload]]:
    data = _rapidapi_get("/audiomack/playlist", params={"url": playlist_url})

    playlist_title = str(data.get("title") or data.get("name") or "unknown-playlist")
    playlist_owner = ""
    if isinstance(data.get("uploader"), dict):
        playlist_owner = str(data["uploader"].get("name") or data["uploader"].get("url_slug") or "")

    # Try to build rich entries if we can find track dicts; otherwise fall back to ids.
    tracks = data.get("tracks")
    songs: list[SongToDownload] = []

    if isinstance(tracks, list):
        for t in tracks:
            if not isinstance(t, dict):
                continue
            sid = t.get("song_id")
            if not isinstance(sid, int):
                # sometimes playlist tracks store `id` as the song id
                if t.get("type") == "song" and isinstance(t.get("id"), int):
                    sid = t["id"]
            if isinstance(sid, int):
                songs.append(
                    SongToDownload(
                        song_id=sid,
                        title=str(t.get("title") or f"song-{sid}"),
                        artist=str(t.get("artist") or "unknown-artist"),
                    )
                )

    if not songs:
        ids = sorted(_extract_song_ids(data))
        if not ids:
            raise SystemExit("No tracks found in playlist response.")
        songs = [SongToDownload(song_id=i, title=f"song-{i}", artist="unknown-artist") for i in ids]

    owner_part = f" ({playlist_owner})" if playlist_owner else ""
    return playlist_title, f"playlist{owner_part}", songs


def _get_signed_url(song_id: int) -> str:
    data = _rapidapi_get(f"/audiomack/song/{song_id}/play")
    signed = data.get("signedUrl")
    if not signed or not isinstance(signed, str):
        raise RuntimeError(f"No signedUrl for song_id={song_id}")
    return signed


def _download_file(url: str, dest: Path, *, timeout_s: int = 60, retries: int = 3) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    for attempt in range(1, retries + 1):
        try:
            with requests.get(url, stream=True, timeout=timeout_s) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                bar = tqdm(
                    total=total if total > 0 else None,
                    unit="B",
                    unit_scale=True,
                    desc=dest.name,
                    leave=False,
                )
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        f.write(chunk)
                        bar.update(len(chunk))
                bar.close()
            tmp.replace(dest)
            return
        except Exception:
            if attempt >= retries:
                raise
            time.sleep(1.25 * attempt)


def _plan_downloads(input_url: str) -> tuple[str, list[SongToDownload]]:
    kind = _guess_url_kind(input_url)
    if kind == "song":
        s = _get_song_from_url(input_url)
        title = f"{s.artist} - {s.title}"
        return title, [s]
    if kind == "album":
        album_title, album_artist, songs = _get_album_songs(input_url)
        return f"{album_artist} - {album_title}", songs
    if kind == "playlist":
        pl_title, pl_owner, songs = _get_playlist_songs(input_url)
        return f"{pl_title} - {pl_owner}", songs

    # Unknown URL shape: try all endpoints in a safe order.
    try:
        s = _get_song_from_url(input_url)
        return f"{s.artist} - {s.title}", [s]
    except Exception:
        pass
    try:
        album_title, album_artist, songs = _get_album_songs(input_url)
        return f"{album_artist} - {album_title}", songs
    except Exception:
        pass
    try:
        pl_title, pl_owner, songs = _get_playlist_songs(input_url)
        return f"{pl_title} - {pl_owner}", songs
    except Exception:
        pass
    raise SystemExit("Could not determine URL type (song/album/playlist) or API call failed.")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        prog="audiomackdl",
        description="Download Audiomack track/album/playlist using RapidAPI audiomack-scraper.",
    )
    p.add_argument("url", help="Audiomack URL (song/album/playlist)")
    p.add_argument(
        "--out",
        default=None,
        help='Output directory. Default: your OS "Downloads/AudiomackDL".',
    )
    p.add_argument("--retries", type=int, default=3, help="Download retries per track (default: 3)")
    args = p.parse_args(argv)

    title, songs = _plan_downloads(args.url)
    root_out = Path(args.out) if args.out else Path(user_downloads_dir()) / "AudiomackDL"
    base_dir = root_out / _safe_name(title)
    base_dir.mkdir(parents=True, exist_ok=True)

    print(f"Saving to: {base_dir}")
    print(f"Tracks: {len(songs)}")

    failures: list[tuple[int, str]] = []
    for i, s in enumerate(songs, start=1):
        prefix = f"{i:02d} - " if len(songs) > 1 else ""
        filename = _safe_name(f"{prefix}{s.artist} - {s.title}.m4a")
        dest = base_dir / filename
        if dest.exists() and dest.stat().st_size > 0:
            continue
        try:
            signed = _get_signed_url(s.song_id)
            _download_file(signed, dest, retries=args.retries)
        except Exception as e:
            failures.append((s.song_id, str(e)))

    if failures:
        print("\nSome downloads failed:")
        for sid, msg in failures:
            print(f"- {sid}: {msg}")
        return 2

    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
