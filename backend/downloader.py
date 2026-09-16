"""Download remote videos (YouTube / Dropbox / direct URL) for analysis.

Everything is fetched **trimmed** to the first ``MAX_DOWNLOAD_SECONDS`` and, for
YouTube, as a **video-only** stream (detection needs no audio, and this avoids
YouTube's separate audio-merge step which requires a JS runtime that isn't
installed here). The bundled imageio-ffmpeg binary is reused for trimming, so no
system ffmpeg is required.

Search-based sources (a "url" of the form ``ytsearch1:<query>``, as used by
config.SEED_VIDEOS) are treated stricter than a plain search: rather than just
downloading YouTube's single top-ranked hit, we pull a pool of candidates and
only download the first one whose *title* actually contains "cctv" — so a
dashcam or compilation clip that merely ranks well for the query can't slip
through as a stand-in for real fixed-camera CCTV footage.

Public API:
    download_video(url, dest_base, max_seconds) -> (title, actual_path)
    extract_playlist_urls(url) -> list[str]
    is_playlist(url) -> bool
"""
from __future__ import annotations

import glob
import os
import re
import shutil
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from . import config

# ---------------------------------------------------------------------------
# TLS / certificate handling
# ---------------------------------------------------------------------------
# On networks with an SSL-inspection proxy (self-signed root CA), downloads fail
# with CERTIFICATE_VERIFY_FAILED. We start verified; on the first such failure we
# flip this process-wide flag and retry without verification. Public video payload
# => acceptable risk. INSECURE_SSL=1 starts insecure from the outset.
_insecure_ssl = bool(config.INSECURE_SSL)


def _is_ssl_error(exc: Exception) -> bool:
    text = str(exc).upper()
    return "CERTIFICATE_VERIFY" in text or "SSL" in text or "SELF-SIGNED" in text or "SELF SIGNED" in text


def _unverified_ssl_context():
    import ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
# ---------------------------------------------------------------------------
# yt-dlp's partial-download check (FFmpegFD.available) looks for a binary literally
# named ``ffmpeg`` on PATH and ignores ``ffmpeg_location``. imageio-ffmpeg's binary
# has a versioned name (ffmpeg-win64-vX.Y.Z.exe), so we copy it once into a small
# shim dir as ``ffmpeg.exe`` and prepend that dir to PATH. This makes every yt-dlp
# code path find ffmpeg without requiring a system install.
_SHIM_DIR = config.STORAGE_DIR / ".bin"
_ffmpeg_ready = False


def _ensure_ffmpeg_on_path() -> str | None:
    """Copy the bundled ffmpeg to a shim dir named ffmpeg.exe and add it to PATH.

    Returns the shim directory path (for use as ``ffmpeg_location``), or None if
    no bundled ffmpeg could be found.
    """
    global _ffmpeg_ready
    src = config.FFMPEG_EXE
    if not src:
        try:
            import imageio_ffmpeg

            src = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001
            return None

    _SHIM_DIR.mkdir(parents=True, exist_ok=True)
    ext = ".exe" if os.name == "nt" else ""
    target = _SHIM_DIR / f"ffmpeg{ext}"
    try:
        if not target.exists() or target.stat().st_size != Path(src).stat().st_size:
            shutil.copy2(src, target)
    except OSError:
        # If copy fails, fall back to pointing directly at the source dir.
        return str(Path(src).parent)

    shim_str = str(_SHIM_DIR.resolve())
    if shim_str not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = shim_str + os.pathsep + os.environ.get("PATH", "")
    _ffmpeg_ready = True
    return shim_str


def _ffmpeg_location() -> str | None:
    """Directory yt-dlp should use for ffmpeg (shim dir with ffmpeg.exe)."""
    loc = _ensure_ffmpeg_on_path()
    return loc


def _ffmpeg_exe() -> str | None:
    """Full path to an ffmpeg executable, for direct subprocess calls."""
    _ensure_ffmpeg_on_path()  # make sure the shim exists
    ext = ".exe" if os.name == "nt" else ""
    shim = _SHIM_DIR / f"ffmpeg{ext}"
    if shim.exists():
        return str(shim)
    return config.FFMPEG_EXE


def _is_dropbox(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "dropbox.com" in host or "dropboxusercontent.com" in host


def _normalize_dropbox(url: str) -> str:
    """Turn a Dropbox *share* link into a *direct-download* link.

    ``www.dropbox.com/s/xxx/file.mp4?dl=0`` -> forces ``dl=1`` so the raw file is
    served rather than the HTML preview page.
    """
    url = url.replace("www.dropbox.com", "dl.dropboxusercontent.com")
    if "dl=0" in url:
        url = url.replace("dl=0", "dl=1")
    elif "dl=1" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}dl=1"
    return url


def is_playlist(url: str) -> bool:
    """Heuristic: does this URL point at a YouTube playlist rather than one video?"""
    low = url.lower()
    if "list=" in low and "watch?v=" not in low:
        return True
    return "/playlist" in low


def extract_playlist_urls(url: str, limit: int = 25) -> list[str]:
    """Expand a playlist URL into individual video URLs (flat, no download)."""
    import yt_dlp

    def _run() -> list[str]:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "playlistend": limit,
        }
        if _insecure_ssl:
            opts["nocheckcertificate"] = True
        urls: list[str] = []
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            for entry in (info or {}).get("entries", []) or []:
                if not entry:
                    continue
                vid = entry.get("url") or entry.get("id")
                if vid and not str(vid).startswith("http"):
                    vid = f"https://www.youtube.com/watch?v={vid}"
                if vid:
                    urls.append(vid)
        return urls

    global _insecure_ssl
    try:
        return _run()
    except Exception as exc:  # noqa: BLE001
        if not _insecure_ssl and _is_ssl_error(exc):
            _insecure_ssl = True
            return _run()
        raise


_SEARCH_PREFIX_RE = re.compile(r"^ytsearch(\d*):(.+)$", re.IGNORECASE)


def _find_cctv_video_url(query: str, pool: int, keyword: str) -> tuple[str, str] | None:
    """Search YouTube for ``query`` and return (video_url, title) for the first
    result whose title contains ``keyword`` (case-insensitive), or None if no
    candidate in the pool qualifies.
    """
    import yt_dlp

    global _insecure_ssl

    def _run():
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
        }
        if _insecure_ssl:
            opts["nocheckcertificate"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"ytsearch{pool}:{query}", download=False)

    try:
        info = _run()
    except Exception as exc:  # noqa: BLE001
        if not _insecure_ssl and _is_ssl_error(exc):
            _insecure_ssl = True
            info = _run()
        else:
            raise

    for entry in (info or {}).get("entries", []) or []:
        if not entry:
            continue
        title = (entry.get("title") or "")
        if keyword.lower() not in title.lower():
            continue
        vid = entry.get("url") or entry.get("id")
        if not vid:
            continue
        video_url = vid if str(vid).startswith("http") else f"https://www.youtube.com/watch?v={vid}"
        return video_url, title

    return None


class DownloadCancelled(RuntimeError):
    """Raised when a download is aborted because the job was cancelled mid-flight."""


def _download_direct(
    url: str, dest_base: Path, cancel_check: Callable[[], bool] | None = None
) -> tuple[str, str]:
    """Download a plain video file (Dropbox / direct link) via urllib, then trim."""
    import urllib.request

    global _insecure_ssl
    raw_path = str(dest_base) + ".src"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    def _fetch(context=None):
        with urllib.request.urlopen(req, timeout=60, context=context) as resp, open(raw_path, "wb") as out:
            # Stream in chunks; avoids loading the whole file into memory.
            while True:
                if cancel_check and cancel_check():
                    raise DownloadCancelled("cancelled by user")
                chunk = resp.read(1 << 20)  # 1 MiB
                if not chunk:
                    break
                out.write(chunk)

    try:
        _fetch(_unverified_ssl_context() if _insecure_ssl else None)
    except DownloadCancelled:
        try:
            os.remove(raw_path)
        except OSError:
            pass
        raise
    except Exception as exc:  # noqa: BLE001
        if not _insecure_ssl and _is_ssl_error(exc):
            _insecure_ssl = True
            _fetch(_unverified_ssl_context())
        else:
            raise

    # Trim to the first N seconds and normalise to H.264 mp4 using bundled ffmpeg.
    out_path = str(dest_base) + ".mp4"
    _trim_with_ffmpeg(raw_path, out_path, config.MAX_DOWNLOAD_SECONDS)
    try:
        os.remove(raw_path)
    except OSError:
        pass

    title = Path(urlparse(url).path).name or "video"
    return title, out_path


def _trim_with_ffmpeg(src: str, dst: str, max_seconds: int) -> None:
    """Re-encode the first ``max_seconds`` of ``src`` into a browser-friendly mp4."""
    import subprocess

    ff = _ffmpeg_exe()
    if not ff:
        raise RuntimeError("ffmpeg binary not available for trimming.")
    cmd = [
        ff, "-y",
        "-i", src,
        "-t", str(max_seconds),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-an",                # drop audio
        "-movflags", "+faststart",
        dst,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not os.path.exists(dst):
        tail = (proc.stderr or "")[-500:]
        raise RuntimeError(f"ffmpeg trim failed: {tail}")


def _download_ytdlp(
    url: str,
    dest_base: Path,
    max_seconds: int,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    """Download a trimmed, video-only mp4 via yt-dlp (YouTube + many other hosts)."""
    import yt_dlp
    from yt_dlp.utils import download_range_func

    global _insecure_ssl
    ff = _ffmpeg_location()
    outtmpl = str(dest_base) + ".%(ext)s"

    def _progress_hook(_status: dict) -> None:
        if cancel_check and cancel_check():
            raise DownloadCancelled("cancelled by user")

    def _run():
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # Prefer video-only mp4 (no audio merge needed), fall back progressively.
            "format": (
                f"bv*[height<={config.MAX_DOWNLOAD_HEIGHT}][ext=mp4]"
                f"/bv*[ext=mp4]/b[ext=mp4]/bv*/b"
            ),
            "outtmpl": outtmpl,
            "download_ranges": download_range_func(None, [(0, max_seconds)]),
            "force_keyframes_at_cuts": True,
            "merge_output_format": "mp4",
            "overwrites": True,
        }
        if cancel_check:
            opts["progress_hooks"] = [_progress_hook]
        if ff:
            opts["ffmpeg_location"] = ff
        if _insecure_ssl:
            opts["nocheckcertificate"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=True)

    try:
        info = _run()
    except DownloadCancelled:
        for leftover in glob.glob(str(dest_base) + ".*"):
            try:
                os.remove(leftover)
            except OSError:
                pass
        raise
    except Exception as exc:  # noqa: BLE001
        if not _insecure_ssl and _is_ssl_error(exc):
            _insecure_ssl = True
            info = _run()
        else:
            raise

    title = (info or {}).get("title") or "video"

    # yt-dlp chooses the final extension; find whatever it produced.
    produced = sorted(glob.glob(str(dest_base) + ".*"))
    # Prefer an mp4 if present.
    mp4s = [p for p in produced if p.lower().endswith(".mp4")]
    actual = (mp4s or produced or [None])[0]
    if not actual or not os.path.exists(actual):
        raise RuntimeError("Download produced no output file.")
    return title, actual


def download_video(
    url: str,
    dest_base: Path,
    max_seconds: int | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    """Download a remote video into ``dest_base.<ext>``, trimmed to ``max_seconds``.

    Returns ``(title, actual_path)``. Routing:
      * ``ytsearchN:<query>``  -> resolve to a concrete CCTV-titled video first
                                   (see ``_find_cctv_video_url``), then download
                                   it like any other YouTube URL below.
      * Dropbox share link      -> normalise to direct link, urllib download + ffmpeg trim
      * everything else         -> yt-dlp (handles YouTube and many generic hosts, and
                                    trims via download_ranges)

    If ``cancel_check`` is given, it's polled periodically during the download
    (yt-dlp progress callbacks / urllib chunk reads); when it returns True, the
    download is aborted and ``DownloadCancelled`` is raised (not wrapped in
    RuntimeError, so callers can tell a deliberate cancel apart from a real
    failure).

    Raises RuntimeError with a readable message on failure.
    """
    if max_seconds is None:
        max_seconds = config.MAX_DOWNLOAD_SECONDS

    dest_base = Path(dest_base)
    dest_base.parent.mkdir(parents=True, exist_ok=True)

    if cancel_check and cancel_check():
        raise DownloadCancelled("cancelled by user")

    search_match = _SEARCH_PREFIX_RE.match(url.strip())
    if search_match:
        n_str, query = search_match.groups()
        requested_n = int(n_str) if n_str else 1
        pool = max(requested_n, 10)  # widen the candidate pool so a title filter has room to work
        found = _find_cctv_video_url(query, pool, keyword="cctv")
        if not found:
            raise RuntimeError(
                f"No CCTV-titled video found among the top {pool} results for search: {query!r}"
            )
        url, _matched_title = found

    if cancel_check and cancel_check():
        raise DownloadCancelled("cancelled by user")

    try:
        if _is_dropbox(url):
            return _download_direct(_normalize_dropbox(url), dest_base, cancel_check)
        return _download_ytdlp(url, dest_base, max_seconds, cancel_check)
    except DownloadCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a clean message to the caller
        # If yt-dlp choked on a plain file URL, try a direct download as a fallback.
        if not _is_dropbox(url):
            try:
                return _download_direct(url, dest_base, cancel_check)
            except DownloadCancelled:
                raise
            except Exception:
                pass
        raise RuntimeError(str(exc)) from exc
