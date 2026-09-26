"""Coordinator for the Upcoming Music Releases integration."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
import logging
import re
import unicodedata
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import SERVER_SOFTWARE, async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    APPLE_ALBUM_DATE_WINDOW_DAYS,
    APPLE_ALBUM_TITLE_RATIO,
    APPLE_NEGATIVE_RETRY_DAYS,
    APPLE_RATE_LIMIT_CODES,
    APPLE_SEARCH_LIMIT,
    APPLE_SEARCH_URL,
    APPLE_UNRESOLVED_STORE_VERSION,
    COLLECTION_FOLDER_ID,
    COLLECTION_PER_PAGE,
    CONF_ALLOWED_GENRES,
    CONF_DISCOGS_TOKEN,
    CONF_DISCOGS_USERNAME,
    CONF_INCLUDE_FOLLOWED,
    CONF_INCLUDE_RECENT_DAYS,
    CONF_LOOKAHEAD_MONTHS,
    CONF_MARKET,
    CONF_SPOTIFY_PLAYER_ENTITY_ID,
    CONF_SPOTIFY_PLAYLIST_ID,
    DEFAULT_ALLOWED_GENRES,
    DEFAULT_INCLUDE_FOLLOWED,
    DEFAULT_INCLUDE_RECENT_DAYS,
    DEFAULT_LOOKAHEAD_MONTHS,
    DEFAULT_MARKET,
    DISCOGS_API_BASE_URL,
    DOMAIN,
    HTTP_TIMEOUT_SECONDS,
    MAX_LOOKAHEAD_DAYS,
    MB_BROWSE_LIMIT,
    MB_BROWSE_URL,
    MB_MAX_ATTEMPTS,
    MB_MAX_SPLIT_DEPTH,
    MB_MIN_INTERVAL_SECONDS,
    MB_NOT_START,
    MB_RETRIEVAL_CAP,
    PAGE_DELAY_SECONDS,
    PLAYLIST_CACHE_STORE_VERSION,
    SCAN_INTERVAL,
    SERVICE_GET_ARTISTS_FOLLOWED,
    SERVICE_GET_PLAYLIST,
    SERVICE_GET_PLAYLIST_ITEMS,
    SPOTIFY_DOMAIN,
    SPOTIFY_MAX_ARTISTS,
    SPOTIFY_PAGE_ITEMS,
    SPOTIFY_PLAYLIST_ITEMS_FIELDS,
    SPOTIFY_PLAYLIST_META_FIELDS,
    SPOTIFY_SERVICE_TIMEOUT_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

_DISAMBIG_RE = re.compile(r"^(.*?)\s*\(\d+\)\s*$")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")
_MIN_NAME_LENGTH = 3
_STATUS_UPDATE_EVERY = 25


def _normalize(name: str) -> str:
    """Canonical artist-name form for pool dedup and Apple matching.

    Lower-cases, strips Discogs '(N)' disambiguation, ASCII-folds accents
    (Beyoncé -> Beyonce, Mötley Crüe -> Motley Crue) and drops a leading or
    Discogs-style trailing article ("The Cure" == "Cure, The").
    """
    name = (name or "").strip().lower()
    if match := _DISAMBIG_RE.match(name):
        name = match.group(1).strip()
    name = (
        unicodedata.normalize("NFKD", name)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    # Discogs puts the article last after a comma: "Cure, The" -> "Cure".
    name = re.sub(r",\s*the$", "", name)
    # Leading article: "The Cure" -> "Cure".
    name = re.sub(r"^the\s+", "", name)
    return "".join(_NON_ALNUM_RE.sub(" ", name).split())


def _usable(name: str) -> bool:
    """Ignore names too short to be trustworthy."""
    return len(_normalize(name)) >= _MIN_NAME_LENGTH


def _best_apple_album(
    items: list[dict[str, Any]],
    artist: str,
    title: str,
    release_date: date | None = None,
) -> dict[str, Any] | None:
    """Pick the Apple album matching (artist, title) best, or None.

    Artist identity is strict (normalized equality): a matched release must
    never be annotated with another artist's album. Title uses similarity so
    Apple suffixes ("Monumental - EP") still match, with a close release date
    breaking ties between editions. The genre allowlist is applied by the
    caller after the pick, so the matched release's own genre decides.
    """
    wanted_artist = _normalize(artist)
    wanted_title = _normalize(title)
    if not wanted_artist or len(wanted_artist) < _MIN_NAME_LENGTH:
        return None
    best: dict[str, Any] | None = None
    best_score = -1.0
    for item in items:
        if not isinstance(item, dict) or item.get("wrapperType") != "collection":
            continue
        if not isinstance(item.get("collectionName"), str):
            continue
        cand_artist = _normalize(item.get("artistName") or "")
        if not cand_artist or cand_artist != wanted_artist:
            continue
        cand_title = _normalize(item.get("collectionName") or "")
        ratio = SequenceMatcher(None, cand_title, wanted_title).ratio()
        if ratio < APPLE_ALBUM_TITLE_RATIO:
            continue
        score = ratio
        if release_date is not None:
            cand_date = _release_date(item.get("releaseDate"))
            if cand_date is not None:
                delta = abs((cand_date - release_date).days)
                if delta == 0:
                    score += 0.1
                elif delta <= APPLE_ALBUM_DATE_WINDOW_DAYS:
                    score += 0.05
        if score > best_score:
            best_score = score
            best = item
    return best


def _release_date(value: Any) -> date | None:
    """Parse an iTunes releaseDate string into a date, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _playlist_pairs(value: Any) -> list[list[str]] | None:
    """Validate the persisted per-playlist artist list.

    Accepts a list of [artist_id, name] pairs, tolerating None/tuples, and
    returns None when nothing usable is present.
    """
    if not isinstance(value, list):
        return None
    pairs: list[list[str]] = []
    for item in value:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            artist_id = item[0]
            name = item[1]
            if isinstance(artist_id, str) and isinstance(name, str):
                pairs.append([artist_id, name])
    return pairs or None


class UpcomingMusicReleasesCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Build the artist pool and fetch upcoming/recent releases.

    The pool (Spotify followed + playlists + Discogs) is intersected with a
    genre-unfiltered MusicBrainz browse of the upcoming window by strict
    artist name; matched releases are then annotated against Apple for the
    genre allowlist, link and album id.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)
        self._entry = entry
        self._session = async_get_clientsession(hass)
        self._timeout = aiohttp.ClientTimeout(connect=5, total=HTTP_TIMEOUT_SECONDS)
        # Per-playlist fingerprint + artist cache, keyed by playlist id, each
        # entry {"snapshot_id", "total", "artists": [[spotify_artist_id, name]]}.
        self._playlist_cache: dict[str, dict[str, Any]] = {}
        self._playlist_store = Store(
            hass, PLAYLIST_CACHE_STORE_VERSION, f"{DOMAIN}.playlists.{entry.entry_id}"
        )
        self._playlist_cache_loaded = False
        self._playlist_cache_dirty = False
        # Last-failure cache for Apple album annotation, each {key: {"reason",
        # "tried_at"}} with keys "mb:<release-group id>". Stops runs re-searching
        # releases Apple has no match for (or whose genre the allowlist blocks).
        self._apple_unresolved: dict[str, dict[str, Any]] = {}
        self._apple_unresolved_store = Store(
            hass,
            APPLE_UNRESOLVED_STORE_VERSION,
            f"{DOMAIN}.apple_unresolved.{entry.entry_id}",
        )
        self._apple_unresolved_loaded = False
        self._apple_unresolved_dirty = False
        # Live scan progress, published to entity listeners as the run advances.
        self._status: dict[str, Any] = {
            "stage": "idle",
            "message": "Waiting",
            "pool_size": 0,
            "resolved": 0,
            "unresolved": 0,
            "albums_done": 0,
            "albums_total": 0,
            "upcoming": 0,
            "recent": 0,
            "started_at": None,
            "finished_at": None,
            "rate_limited": False,
        }

    # -- status helpers -----------------------------------------------------

    @property
    def status(self) -> dict[str, Any]:
        """Live copy of the current scan status."""
        return dict(self._status)

    def _set_status(self, stage: str, message: str, **updates: Any) -> None:
        """Publish a live status update to entity listeners."""
        self._status["stage"] = stage
        self._status["message"] = message
        self._status.update(updates)
        self.async_update_listeners()

    # -- config helpers -----------------------------------------------------

    def _market(self) -> str:
        """Return the configured Apple/iTunes store country."""
        market = str(self._entry.data.get(CONF_MARKET) or "").strip().upper()
        return market or DEFAULT_MARKET

    def _allowed_genres(self) -> set[str] | None:
        """Return the normalized Apple genre allowlist, or None when disabled.

        An explicitly empty string means "no genre filtering" (someone cleared
        the field on purpose); a missing key falls back to the default list so
        the genre guard is live from the first scan after a store reset.
        """
        raw = self._entry.data.get(CONF_ALLOWED_GENRES)
        if raw is None:
            raw = DEFAULT_ALLOWED_GENRES
        if not isinstance(raw, str) or not raw.strip():
            return None
        genres = {
            _normalize(name)
            for name in (part.strip() for part in raw.split(","))
            if name
        }
        return genres or None

    def _lookahead_days(self) -> int:
        """Return the lookahead window in days (months x 31, capped)."""
        months = int(
            self._entry.data.get(CONF_LOOKAHEAD_MONTHS, DEFAULT_LOOKAHEAD_MONTHS)
        )
        return min(months * 31, MAX_LOOKAHEAD_DAYS)

    def _recent_days(self) -> int:
        """Return how many days back to also show released albums (0 = off)."""
        return int(
            self._entry.data.get(CONF_INCLUDE_RECENT_DAYS, DEFAULT_INCLUDE_RECENT_DAYS)
        )

    def _playlist_ids(self) -> list[str]:
        """Return the configured Spotify playlist ids, in any legacy shape."""
        value = self._entry.data.get(CONF_SPOTIFY_PLAYLIST_ID)
        if isinstance(value, str):
            return [value] if value else []
        if isinstance(value, list):
            return [item for item in value if item]
        return []

    def _player_entity_id(self) -> str | None:
        """Return the configured SpotifyPlus player entity id."""
        return self._entry.data.get(CONF_SPOTIFY_PLAYER_ENTITY_ID)

    # -- Spotify pooling ----------------------------------------------------

    async def _async_spotify_call(self, service: str, data: dict[str, Any]) -> Any:
        """Call a spotifyplus service on the configured player and return its result.

        Raises HomeAssistantError on missing player, missing service or failure.
        """
        player_entity_id = self._player_entity_id()
        if not player_entity_id:
            raise HomeAssistantError("No Spotify player is configured")
        if not self.hass.services.has_service(SPOTIFY_DOMAIN, service):
            raise HomeAssistantError(
                f"SpotifyPlus service {SPOTIFY_DOMAIN}.{service} is unavailable"
            )
        try:
            response = await asyncio.wait_for(
                self.hass.services.async_call(
                    SPOTIFY_DOMAIN,
                    service,
                    data,
                    target={"entity_id": player_entity_id},
                    blocking=True,
                    return_response=True,
                ),
                timeout=SPOTIFY_SERVICE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as err:
            raise HomeAssistantError(f"Spotify {service} timed out") from err
        except HomeAssistantError as err:
            raise HomeAssistantError(f"Spotify {service} failed: {err}") from err
        return (response or {}).get("result")

    async def _async_fetch_followed_artists(self) -> list[dict[str, Any]]:
        """Return the user's followed artists as raw artist objects."""
        if not self.hass.services.has_service(
            SPOTIFY_DOMAIN, SERVICE_GET_ARTISTS_FOLLOWED
        ):
            raise HomeAssistantError("SpotifyPlus followed-artists service unavailable")
        result = await self._async_spotify_call(
            SERVICE_GET_ARTISTS_FOLLOWED, {"limit_total": SPOTIFY_MAX_ARTISTS}
        )
        items = result.get("items") if isinstance(result, dict) else None
        if not isinstance(items, list):
            artists = result.get("artists") if isinstance(result, dict) else None
            items = artists.get("items") if isinstance(artists, dict) else None
        return [item for item in items or [] if isinstance(item, dict)]

    async def _async_fetch_spotify_playlist_artists(self) -> dict[str, str]:
        """Return each playlist artist as {name: spotify_artist_id or ''}.

        Each playlist is first checked with a slim metadata call (snapshot id
        plus track count); when it matches the persisted fingerprint, the
        cached artist list is reused and the track scan is skipped.
        """
        player_entity_id = self._player_entity_id()
        playlist_ids = self._playlist_ids()
        if not playlist_ids or not player_entity_id:
            return {}
        if not self.hass.services.has_service(
            SPOTIFY_DOMAIN, SERVICE_GET_PLAYLIST_ITEMS
        ):
            raise HomeAssistantError("SpotifyPlus playlist service unavailable")
        has_meta = self.hass.services.has_service(SPOTIFY_DOMAIN, SERVICE_GET_PLAYLIST)
        await self._async_ensure_playlist_cache_loaded()

        artists: dict[str, str] = {}
        for playlist_id in playlist_ids:
            cached = self._playlist_cache.get(playlist_id) or {}
            snapshot_id = None
            total = None
            if has_meta:
                meta = await self._async_get_playlist_meta(
                    playlist_id, player_entity_id
                )
                if meta is not None:
                    snapshot_id = meta["snapshot_id"]
                    total = meta["total"]
            cached_pairs = _playlist_pairs(cached.get("artists"))
            if (
                snapshot_id
                and cached.get("snapshot_id") == snapshot_id
                and cached.get("total") == total
                and cached_pairs
            ):
                _LOGGER.debug(
                    "Playlist %s unchanged (snapshot %s) - reusing %d cached artist(s)",
                    playlist_id,
                    snapshot_id,
                    len(cached_pairs),
                )
                for artist_id, name in cached_pairs:
                    artists[name] = artist_id or ""
                continue
            found = await self._async_fetch_playlist_artists(
                playlist_id, total, player_entity_id
            )
            if found is None:
                if cached_pairs:
                    _LOGGER.warning(
                        "Playlist %s scan failed; keeping %d cached artist(s)",
                        playlist_id,
                        len(cached_pairs),
                    )
                    for artist_id, name in cached_pairs:
                        artists[name] = artist_id or ""
                continue
            self._playlist_cache[playlist_id] = {
                "snapshot_id": snapshot_id,
                "total": total,
                "artists": found,
            }
            self._playlist_cache_dirty = True
            for artist_id, name in found:
                artists[name] = artist_id or ""
        if self._playlist_cache_dirty:
            await self._playlist_store.async_save(self._playlist_cache)
            self._playlist_cache_dirty = False
        return artists

    async def _async_get_playlist_meta(
        self, playlist_id: str, player: str
    ) -> dict[str, Any] | None:
        """Fetch a playlist's Spotify snapshot id and track count (one call)."""
        try:
            response = await asyncio.wait_for(
                self.hass.services.async_call(
                    SPOTIFY_DOMAIN,
                    SERVICE_GET_PLAYLIST,
                    {
                        "playlist_id": playlist_id,
                        "fields": SPOTIFY_PLAYLIST_META_FIELDS,
                    },
                    target={"entity_id": player},
                    blocking=True,
                    return_response=True,
                ),
                timeout=SPOTIFY_SERVICE_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, HomeAssistantError) as err:
            _LOGGER.warning(
                "Spotify playlist metadata fetch failed for %s: %s", playlist_id, err
            )
            return None
        result = (response or {}).get("result") or {}
        snapshot = result.get("snapshotId") or result.get("snapshot_id")
        total = (result.get("tracks") or {}).get("total")
        return {
            "snapshot_id": str(snapshot) if snapshot else None,
            "total": int(total) if isinstance(total, int) else None,
        }

    async def _async_fetch_playlist_artists(
        self, playlist_id: str, total: int | None, player: str
    ) -> list[list[str]] | None:
        """Scan one playlist's tracks and return [[artist_id, name], ...] pairs
        deduplicated by Spotify artist id (name as fallback).

        Pages at SPOTIFY_PAGE_ITEMS (50) - spotifyplus rejects larger limits
        when `fields` is used. Returns None on failure.
        """
        artists: dict[str, str] = {}
        offset = 0
        while True:
            if total is not None and offset >= total:
                break
            try:
                response = await asyncio.wait_for(
                    self.hass.services.async_call(
                        SPOTIFY_DOMAIN,
                        SERVICE_GET_PLAYLIST_ITEMS,
                        {
                            "playlist_id": playlist_id,
                            "limit": SPOTIFY_PAGE_ITEMS,
                            "offset": offset,
                            "fields": SPOTIFY_PLAYLIST_ITEMS_FIELDS,
                        },
                        target={"entity_id": player},
                        blocking=True,
                        return_response=True,
                    ),
                    timeout=SPOTIFY_SERVICE_TIMEOUT_SECONDS,
                )
            except (asyncio.TimeoutError, HomeAssistantError) as err:
                _LOGGER.warning(
                    "Spotify playlist fetch failed for %s: %s", playlist_id, err
                )
                return None
            result = (response or {}).get("result") or {}
            items = result.get("items") or []
            for item in items:
                track = item.get("track") or item.get("item") or {}
                for artist in track.get("artists") or []:
                    artist_id = artist.get("id")
                    name = artist.get("name")
                    if not artist_id and not name:
                        continue
                    artists[artist_id or name] = name or artist_id
            if len(items) < SPOTIFY_PAGE_ITEMS:
                break
            offset += SPOTIFY_PAGE_ITEMS
            if offset >= 10000:
                break
        if not artists:
            return None
        return [[key, value] for key, value in sorted(artists.items())]

    async def _async_ensure_playlist_cache_loaded(self) -> None:
        """Load the persisted per-playlist fingerprint/artist cache once."""
        if self._playlist_cache_loaded:
            return
        loaded = await self._playlist_store.async_load()
        if isinstance(loaded, dict):
            self._playlist_cache = loaded
        self._playlist_cache_loaded = True

    # -- Discogs pooling ----------------------------------------------------

    def _discogs_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Discogs token={self._entry.data[CONF_DISCOGS_TOKEN]}",
            "User-Agent": SERVER_SOFTWARE,
        }

    async def _async_discogs_get(self, url: str) -> dict[str, Any]:
        """GET a Discogs endpoint and return the decoded JSON object."""
        async with self._session.get(
            url, headers=self._discogs_headers(), timeout=self._timeout
        ) as response:
            if response.status == 401:
                raise HomeAssistantError(
                    "Discogs authentication failed - check token"
                )
            if response.status == 429:
                raise HomeAssistantError("Discogs rate limit reached - try again later")
            response.raise_for_status()
            return await response.json(content_type=None)

    async def _async_fetch_discogs_artists(self) -> list[str]:
        """Fetch every artist name across the user's collection."""
        username = self._entry.data.get(CONF_DISCOGS_USERNAME)
        if not username:
            raise HomeAssistantError("Discogs username is missing")
        artists: set[str] = set()
        page = 1
        while True:
            data = await self._async_discogs_get(
                f"{DISCOGS_API_BASE_URL}/users/{username}/collection/"
                f"folders/{COLLECTION_FOLDER_ID}/releases"
                f"?per_page={COLLECTION_PER_PAGE}&page={page}"
            )
            for release in data.get("releases", []):
                for artist in release.get("basic_information", {}).get("artists", []):
                    if name := artist.get("name"):
                        artists.add(name)
            pages = int(data.get("pagination", {}).get("pages") or 1)
            if page >= pages:
                break
            page += 1
            await asyncio.sleep(PAGE_DELAY_SECONDS)
        return sorted(artists)

    # -- MusicBrainz browse + Apple annotation ------------------------------

    async def _async_ensure_apple_unresolved_loaded(self) -> None:
        """Load the persisted failed-resolution cache once."""
        if self._apple_unresolved_loaded:
            return
        loaded = await self._apple_unresolved_store.async_load()
        if isinstance(loaded, dict):
            self._apple_unresolved = {
                str(key): value for key, value in loaded.items()
            }
        self._apple_unresolved_loaded = True

    def _apple_unresolved_retry_due(self, key: str) -> bool:
        """Whether a previously failed artist should be searched again.

        Genuine no-matches are only retried after APPLE_NEGATIVE_RETRY_DAYS;
        transient and rate-limited failures are never recorded here, so they
        are always retried next scan.
        """
        entry = self._apple_unresolved.get(key)
        if not entry:
            return True
        tried = entry.get("tried_at")
        if isinstance(tried, str):
            tried_dt = dt_util.parse_datetime(tried)
            if tried_dt is not None and dt_util.utcnow() - tried_dt < timedelta(
                days=APPLE_NEGATIVE_RETRY_DAYS
            ):
                return False
        return True

    @staticmethod
    def _apple_unresolved_key(rg: dict[str, Any]) -> str:
        """Cache key for one matched release: its MusicBrainz release-group id."""
        return f"mb:{rg.get('id')}"

    @staticmethod
    def _mb_headers() -> dict[str, str]:
        """MusicBrainz etiquette: a descriptive User-Agent."""
        return {
            "User-Agent": (
                f"{SERVER_SOFTWARE} "
                "(upcoming_music_releases Home Assistant integration)"
            ),
            "Accept": "application/json",
        }

    @staticmethod
    def _mb_window_chunks(lo: date, hi: date) -> list[tuple[str, str]]:
        """Split [lo..hi] into month-granular chunks (day-aligned at edges)."""
        chunks: list[tuple[str, str]] = []
        start = lo
        while start <= hi:
            next_month = date(start.year, start.month, 1) + timedelta(days=32)
            end = date(next_month.year, next_month.month, 1) - timedelta(days=1)
            if end > hi:
                end = hi
            chunks.append((start.isoformat(), end.isoformat()))
            start = end + timedelta(days=1)
        return chunks

    def _mb_chunk_query(self, lo: str, hi: str, offset: int) -> dict[str, Any]:
        """Query params for one chunk, with the NOT-clause exclusion.

        MB's daterange-overlap semantics make a year-only first-release-date
        match every day query in its year, which would flood the 500-row
        retrieval cap with year-only junk and drop exact-date releases. The
        NOT-clause excludes anything whose daterange starts before the chunk.
        """
        exclude_until = (date.fromisoformat(lo) - timedelta(days=1)).isoformat()
        query = (
            f"firstreleasedate:[{lo} TO {hi}] AND primarytype:album "
            f"AND NOT firstreleasedate:[{MB_NOT_START} TO {exclude_until}]"
        )
        return {
            "query": query,
            "fmt": "json",
            "limit": MB_BROWSE_LIMIT,
            "offset": offset,
        }

    async def _mb_get(self, params: dict[str, Any]) -> dict[str, Any]:
        """One MusicBrainz release-group query, paced and backoff-retried.

        MB etiquette: at most 1 request per second with a real User-Agent;
        503s and "too many results" 400s get extra backoff. Raises
        HomeAssistantError after MB_MAX_ATTEMPTS.
        """
        last_error: Exception | None = None
        for attempt in range(MB_MAX_ATTEMPTS):
            await asyncio.sleep(MB_MIN_INTERVAL_SECONDS)
            try:
                async with self._session.get(
                    MB_BROWSE_URL,
                    params=params,
                    headers=self._mb_headers(),
                    timeout=self._timeout,
                ) as response:
                    if response.status == 503:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    if response.status == 400:
                        body = await response.text()
                        if "too many results" in body.lower():
                            _LOGGER.warning("MB query too broad, backing off")
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                    response.raise_for_status()
                    return await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                last_error = err
                await asyncio.sleep(2)
        raise HomeAssistantError(
            f"MusicBrainz browse failed after {MB_MAX_ATTEMPTS} attempts"
            + (f": {last_error}" if last_error else "")
        )

    async def _mb_fetch_pages(
        self, lo: str, hi: str, depth: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (rows, count) for a chunk; split when count exceeds the cap.

        MB caps retrieval at offset+limit <= 500, so a chunk reporting more
        hits than MB_RETRIEVAL_CAP is split in half (single-day floor as a
        safety net) rather than losing exact-date rows to the cap.
        """
        first = await self._mb_get(self._mb_chunk_query(lo, hi, 0))
        count = int(first.get("count") or 0)
        if count == 0:
            return [], 0
        if count > MB_RETRIEVAL_CAP and lo != hi and depth < MB_MAX_SPLIT_DEPTH:
            first_date = date.fromisoformat(lo)
            last_date = date.fromisoformat(hi)
            mid = first_date + (last_date - first_date) // 2
            rows_left, _n_left = await self._mb_fetch_pages(
                lo, mid.isoformat(), depth + 1
            )
            rows_right, _n_right = await self._mb_fetch_pages(
                (mid + timedelta(days=1)).isoformat(), hi, depth + 1
            )
            return rows_left + rows_right, count
        rows = list(first.get("release-groups") or [])
        offset = MB_BROWSE_LIMIT
        while offset + MB_BROWSE_LIMIT <= MB_RETRIEVAL_CAP and offset < count:
            page = await self._mb_get(self._mb_chunk_query(lo, hi, offset))
            rows.extend(page.get("release-groups") or [])
            offset += MB_BROWSE_LIMIT
        if count > MB_RETRIEVAL_CAP:
            _LOGGER.warning(
                "MB chunk %s..%s reports %d hits (>%d cap); captured %d rows",
                lo,
                hi,
                count,
                MB_RETRIEVAL_CAP,
                len(rows),
            )
        return rows, count

    async def _async_browse_mb(self, lo: date, hi: date) -> list[dict[str, Any]]:
        """Browse MusicBrainz for album release-groups with dates in [lo..hi].

        Genre-unfiltered: the pool intersection decides what is ours, the
        Apple genre allowlist gates afterwards. Month-granular chunks with the
        NOT-clause exclusion keep year-only dateranges from flooding the
        500-row retrieval cap and hiding exact-date releases. Deduplicated by
        release-group id.
        """
        chunks = self._mb_window_chunks(lo, hi)
        by_id: dict[str, dict[str, Any]] = {}
        total_chunks = len(chunks)
        for index, (chunk_lo, chunk_hi) in enumerate(chunks, 1):
            self._set_status(
                "browsing",
                f"Browsing MusicBrainz… {index}/{total_chunks}",
            )
            rows, _count = await self._mb_fetch_pages(chunk_lo, chunk_hi, 0)
            for rg in rows:
                if isinstance(rg, dict) and rg.get("id"):
                    by_id.setdefault(rg["id"], rg)
        return list(by_id.values())

    @staticmethod
    def _rg_artist_names(rg: dict[str, Any]) -> list[str]:
        """Every credited artist name on a release-group, incl. join entries."""
        names: list[str] = []
        for credit in rg.get("artist-credit") or []:
            if not isinstance(credit, dict):
                continue
            if isinstance(credit.get("name"), str) and credit["name"]:
                names.append(credit["name"])
                continue
            artist = credit.get("artist")
            if (
                isinstance(artist, dict)
                and isinstance(artist.get("name"), str)
                and artist["name"]
            ):
                names.append(artist["name"])
        return names

    @staticmethod
    def _rg_release_date(rg: dict[str, Any]) -> date | None:
        """Exact first-release-date of a release-group, or None when partial."""
        raw = rg.get("first-release-date") or rg.get("first_release_date")
        if not isinstance(raw, str) or len(raw) < 10:
            return None
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None

    def _intersect_pool(
        self,
        rgs: list[dict[str, Any]],
        pool: dict[str, dict[str, Any]],
        window_lo: date,
        window_hi: date,
    ) -> list[tuple[date, dict[str, Any], str]]:
        """Return browse results whose artist credits strictly match the pool.

        Each match is (release_date, release_group, credited_artist_name).
        The intersection is the whole filter at this stage: browse is
        genre-unfiltered, strict _normalize name equality decides what is
        ours, and Apple's genre allowlist gates the result only afterwards.
        """
        norm_pool = set(pool)
        matches: list[tuple[date, dict[str, Any], str]] = []
        for rg in rgs:
            release_date = self._rg_release_date(rg)
            if release_date is None:
                continue
            if release_date < window_lo or release_date > window_hi:
                continue
            hit: str | None = None
            for name in self._rg_artist_names(rg):
                if not _usable(name):
                    continue
                if _normalize(name) in norm_pool:
                    hit = name
                    break
            if hit is not None:
                matches.append((release_date, rg, hit))
        matches.sort(key=lambda item: (item[0], item[1].get("title") or ""))
        return matches

    async def _async_search_apple_album(
        self, artist: str, title: str, release_date: date
    ) -> tuple[dict[str, Any] | None, str]:
        """Resolve one matched release to its Apple album record.

        Returns (album, status): "ok", "no_match" (Apple answered but nothing
        matched), "genre_blocked" (the matched album's genre is outside the
        allowlist), "error" (transient failure, retry next run) or
        "rate_limited" (give up on this run).
        """
        try:
            async with self._session.get(
                APPLE_SEARCH_URL,
                params={
                    "term": f"{artist} {title}",
                    "entity": "album",
                    "limit": APPLE_SEARCH_LIMIT,
                    "country": self._market(),
                },
                timeout=self._timeout,
            ) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            if err.status in APPLE_RATE_LIMIT_CODES:
                _LOGGER.warning("Apple album search rate limited (HTTP %s)", err.status)
                return None, "rate_limited"
            _LOGGER.warning(
                "Apple album search failed for %r - %r: %s", artist, title, err
            )
            return None, "error"
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.warning(
                "Apple album search failed for %r - %r: %s", artist, title, err
            )
            return None, "error"
        if not isinstance(data, dict):
            return None, "error"
        album = _best_apple_album(
            data.get("results") or [], artist, title, release_date
        )
        if album is None:
            return None, "no_match"
        allowed = self._allowed_genres()
        if allowed and _normalize(album.get("primaryGenreName") or "") not in allowed:
            return None, "genre_blocked"
        return album, "ok"

    async def _async_annotate_matches(
        self, matches: list[tuple[date, dict[str, Any], str]]
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Resolve matched releases to Apple albums (genre-gated).

        Returns (albums, rejected, rate_limited). Albums are dicts in the
        shape _build_events expects; rejected counts matched releases that
        produced no event (no Apple match, genre-blocked, or error). When a
        rate limit stops the run early the leftovers are left for a later
        scan. Genuine no-match/genre-blocked releases are not re-searched for
        APPLE_NEGATIVE_RETRY_DAYS.
        """
        await self._async_ensure_apple_unresolved_loaded()
        albums: list[dict[str, Any]] = []
        rejected = 0
        rate_limited = False
        total = len(matches)
        for index, (release_date, rg, artist) in enumerate(matches, 1):
            key = self._apple_unresolved_key(rg)
            if not self._apple_unresolved_retry_due(key):
                rejected += 1
                continue
            title = rg.get("title") or "Unknown Release"
            album, status = await self._async_search_apple_album(
                artist, title, release_date
            )
            if status == "rate_limited":
                rate_limited = True
                break
            if album is not None:
                albums.append(
                    {
                        "artist_id": album.get("artistId"),
                        "artist_name": album.get("artistName") or artist,
                        "collection_id": album.get("collectionId"),
                        "collection_name": title,
                        "release_date": release_date,
                        "url": album.get("collectionViewUrl"),
                        "genre": album.get("primaryGenreName"),
                        "tracks": album.get("trackCount"),
                    }
                )
                self._apple_unresolved.pop(key, None)
                self._apple_unresolved_dirty = True
            elif status in ("no_match", "genre_blocked"):
                self._apple_unresolved[key] = {
                    "reason": status,
                    "tried_at": dt_util.utcnow().isoformat(),
                }
                self._apple_unresolved_dirty = True
                rejected += 1
            else:  # "error": not recorded, retried next scan.
                rejected += 1
            await asyncio.sleep(PAGE_DELAY_SECONDS)
            if index % _STATUS_UPDATE_EVERY == 0 or index == total:
                self._set_status(
                    "annotating",
                    f"Checking genres… {index}/{total}",
                    resolved=len(albums),
                    unresolved=index - len(albums),
                    albums_done=index,
                    albums_total=total,
                )
        if self._apple_unresolved_dirty:
            await self._apple_unresolved_store.async_save(self._apple_unresolved)
            self._apple_unresolved_dirty = False
        return albums, rejected, rate_limited

    # -- event building -----------------------------------------------------

    def _build_events(self, albums: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Turn fetched albums within the window into calendar event dicts."""
        today = dt_util.now().date()
        start_cutoff = today - timedelta(days=self._recent_days())
        end_cutoff = today + timedelta(days=self._lookahead_days())
        events: dict[str, dict[str, Any]] = {}
        for album in albums:
            release_date = album["release_date"]
            if release_date < start_cutoff or release_date > end_cutoff:
                continue
            artist = album.get("artist_name") or "Unknown Artist"
            title = album.get("collection_name") or "Unknown Release"
            uid = str(
                album.get("collection_id")
                or f"{artist}|{title}|{release_date.isoformat()}"
            )
            if uid in events:
                continue
            summary = f"{artist} \u2013 {title}"
            description_parts = [
                f"Genre: {album.get('genre') or 'Unknown'}",
                f"Tracks: {album.get('tracks') or '?'}",
            ]
            if album.get("url"):
                description_parts.append(album["url"])
            events[uid] = {
                "summary": summary,
                "artist": artist,
                "album": title,
                "start": release_date,
                "end": release_date + timedelta(days=1),
                "description": "\n".join(description_parts),
                "recent": release_date < today,
                "url": album.get("url"),
                "genre": album.get("genre"),
                "tracks": album.get("tracks"),
                "uid": uid,
            }
        return sorted(events.values(), key=lambda event: event["start"])

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        """Return an all-empty data payload."""
        return {
            "events": [],
            "upcoming": 0,
            "recent": 0,
            "artists": 0,
            "sources": {},
            "apple_resolved": 0,
            "apple_unresolved": 0,
            "status": {"stage": "done", "message": "Pool empty - no artists to check"},
        }

    # -- update loop --------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the pool, browse MusicBrainz, annotate matches and build events."""
        self._set_status("pooling", "Fetching artists…", started_at=dt_util.now())
        followed: list[dict[str, Any]] = []
        playlist_artists: dict[str, str] = {}
        discogs_artists: list[str] = []
        source_errors: list[str] = []

        if self._entry.data.get(CONF_INCLUDE_FOLLOWED, DEFAULT_INCLUDE_FOLLOWED):
            try:
                followed = await self._async_fetch_followed_artists()
            except HomeAssistantError as err:
                source_errors.append(f"followed artists: {err}")
                _LOGGER.warning("Followed artists unavailable: %s", err)

        try:
            playlist_artists = await self._async_fetch_spotify_playlist_artists()
        except HomeAssistantError as err:
            source_errors.append(f"playlists: {err}")
            _LOGGER.warning("Playlist artists unavailable: %s", err)

        try:
            discogs_artists = await self._async_fetch_discogs_artists()
        except HomeAssistantError as err:
            source_errors.append(f"Discogs: {err}")
            _LOGGER.warning("Discogs artists unavailable: %s", err)

        pool: dict[str, dict[str, Any]] = {}
        for artist in followed:
            name = artist.get("name")
            if not _usable(name):
                continue
            entry_ = pool.setdefault(
                _normalize(name),
                {"name": name, "spotify_ids": set(), "sources": set()},
            )
            entry_["name"] = name
            if artist.get("id"):
                entry_["spotify_ids"].add(artist["id"])
            entry_["sources"].add("followed")
        for name, spotify_id in playlist_artists.items():
            if not _usable(name):
                continue
            entry_ = pool.setdefault(
                _normalize(name),
                {"name": name, "spotify_ids": set(), "sources": set()},
            )
            entry_["name"] = name
            if spotify_id:
                entry_["spotify_ids"].add(spotify_id)
            entry_["sources"].add("playlist")
        for name in discogs_artists:
            if not _usable(name):
                continue
            entry_ = pool.setdefault(
                _normalize(name),
                {"name": name, "spotify_ids": set(), "sources": set()},
            )
            entry_["name"] = name
            entry_["sources"].add("discogs")

        if not pool:
            if len(source_errors) == 3:
                raise UpdateFailed(
                    "All artist sources failed: " + "; ".join(source_errors)
                )
            _LOGGER.info("New release pool is empty; nothing to check")
            self._set_status(
                "done",
                "Pool empty - no artists to check",
                pool_size=0,
                finished_at=dt_util.now(),
            )
            return self._empty_data()

        today = dt_util.now().date()
        window_lo = today - timedelta(days=self._recent_days())
        window_hi = today + timedelta(days=self._lookahead_days())
        self._set_status(
            "browsing",
            "Browsing MusicBrainz…",
            pool_size=len(pool),
            resolved=0,
            unresolved=0,
        )
        browse_failed = False
        try:
            rgs = await self._async_browse_mb(window_lo, window_hi)
        except HomeAssistantError as err:
            _LOGGER.warning("MusicBrainz browse failed: %s", err)
            browse_failed = True
            rgs = []
        matches = self._intersect_pool(rgs, pool, window_lo, window_hi)
        albums, _rejected, rate_limited = await self._async_annotate_matches(matches)
        events = self._build_events(albums)
        resolved = len(albums)
        unresolved = len(matches) - len(albums)

        upcoming = sum(1 for event in events if not event["recent"])
        recent = sum(1 for event in events if event["recent"])
        if browse_failed:
            message = "Browse failed - no releases checked"
        elif rate_limited:
            message = (
                f"Rate limited - partial scan: {upcoming} upcoming, {recent} recent"
            )
        else:
            message = f"Scan complete: {upcoming} upcoming, {recent} recent"
        self._set_status(
            "done",
            message,
            pool_size=len(pool),
            resolved=resolved,
            unresolved=unresolved,
            upcoming=upcoming,
            recent=recent,
            finished_at=dt_util.now(),
            rate_limited=rate_limited,
        )

        return {
            "events": events,
            "upcoming": upcoming,
            "recent": recent,
            "artists": len(pool),
            "sources": {
                source: sum(
                    1
                    for entry_ in pool.values()
                    if source in entry_["sources"]
                )
                for source in ("followed", "playlist", "discogs")
            },
            "apple_resolved": resolved,
            "apple_unresolved": unresolved,
            "rate_limited": rate_limited,
            "status": self.status,
        }