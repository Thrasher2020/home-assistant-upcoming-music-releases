"""Constants for the Upcoming Music Releases integration."""

from datetime import timedelta

DOMAIN = "upcoming_music_releases"
NAME = "Upcoming Music Releases"

CONF_DISCOGS_TOKEN = "discogs_token"
CONF_DISCOGS_USERNAME = "discogs_username"

CONF_SPOTIFY_PLAYER_ENTITY_ID = "spotify_player_entity_id"
CONF_SPOTIFY_PLAYLIST_ID = "spotify_playlist_id"
CONF_SPOTIFY_PLAYLIST_NAME = "spotify_playlist_name"

CONF_INCLUDE_FOLLOWED = "include_followed"
CONF_MARKET = "market"
CONF_LOOKAHEAD_MONTHS = "lookahead_months"
CONF_INCLUDE_RECENT_DAYS = "include_recent_days"
# Comma-separated Apple artist genres allowed to enter the calendar. When the
# entry has no value, the coordinator falls back to DEFAULT_ALLOWED_GENRES so
# the genre guard is live from the first scan after a store reset.
CONF_ALLOWED_GENRES = "allowed_genres"

DEFAULT_INCLUDE_FOLLOWED = True
DEFAULT_MARKET = "GB"
DEFAULT_LOOKAHEAD_MONTHS = 12
DEFAULT_INCLUDE_RECENT_DAYS = 7
# Default Apple artist-genre allowlist (the rock/metal family). Matched after
# _normalize, so spelling must follow Apple's primaryGenreName ("Death
# Metal/Black Metal", "Rock & Roll", ...). Clearing the field disables the
# filter; leaving it unset uses this list.
DEFAULT_ALLOWED_GENRES = (
    "Metal, Rock, Hard Rock, Heavy Metal, Death Metal/Black Metal, Alternative, "
    "Grunge, Punk, Industrial, Gothic, Progressive, Psychedelic, Rock & Roll, "
    "Classic Rock, New Wave, Post-Punk, Indie Rock, Emo, Garage Rock, Blues-Rock"
)

MAX_LOOKAHEAD_DAYS = 730

SPOTIFY_DOMAIN = "spotifyplus"
SERVICE_GET_ARTISTS_FOLLOWED = "get_artists_followed"
SERVICE_GET_PLAYLIST = "get_playlist"
SERVICE_GET_PLAYLIST_FAVORITES = "get_playlist_favorites"
SERVICE_GET_PLAYLIST_ITEMS = "get_playlist_items"

SPOTIFY_MAX_ARTISTS = 2000
SPOTIFY_PAGE_ITEMS = 50  # spotifyplus get_playlist_items limit max = 50
SPOTIFY_PLAYLIST_META_FIELDS = "id,name,snapshot_id,tracks(total)"
SPOTIFY_PLAYLIST_ITEMS_FIELDS = "items(track(artists(id,name)))"
SPOTIFY_SERVICE_TIMEOUT_SECONDS = 60

DISCOGS_API_BASE_URL = "https://api.discogs.com"
COLLECTION_FOLDER_ID = 0
COLLECTION_PER_PAGE = 100

APPLE_SEARCH_URL = "https://itunes.apple.com/search"
APPLE_SEARCH_LIMIT = 5
# Apple Search API answers these when its per-window quota is spent; runs
# stop annotating early on them and pick up the leftovers next scan.
APPLE_RATE_LIMIT_CODES = (403, 429)
# No-match / genre-blocked releases are only re-searched after this many days
# (Apple's catalog grows), so runs keep their burst budget for fresh lookups.
APPLE_NEGATIVE_RETRY_DAYS = 7
# Apple album annotation: title similarity threshold (Apple appends suffixes
# like "- EP" to album names) and the release-date window that breaks ties
# between editions of the same album.
APPLE_ALBUM_TITLE_RATIO = 0.8
APPLE_ALBUM_DATE_WINDOW_DAYS = 45

# MusicBrainz browse (release list first). The browse is genre-unfiltered; the
# pool intersection + Apple allowlist filter the results. MB date fields use
# daterange-overlap semantics, so a year-only first-release-date ("2026",
# daterange [2026-01-01..2026-12-31]) matches EVERY day query in its year.
# The NOT-clause in _mb_chunk_query excludes anything whose daterange starts
# before the chunk, so year-only rows cannot flood the 500-result retrieval
# cap and hide the exact-date releases the calendar needs.
MB_BROWSE_URL = "https://musicbrainz.org/ws/2/release-group"
MB_BROWSE_LIMIT = 100
MB_RETRIEVAL_CAP = 500  # MB caps retrieval at offset+limit <= 500
MB_MAX_SPLIT_DEPTH = 12
MB_MAX_ATTEMPTS = 8
MB_MIN_INTERVAL_SECONDS = 1.1  # MB etiquette: at most 1 request per second
MB_NOT_START = "1000-01-01"  # safely before any first-release-date

SCAN_INTERVAL = timedelta(hours=12)
HTTP_TIMEOUT_SECONDS = 30
PAGE_DELAY_SECONDS = 0.25

APPLE_UNRESOLVED_STORE_VERSION = 1
PLAYLIST_CACHE_STORE_VERSION = 1