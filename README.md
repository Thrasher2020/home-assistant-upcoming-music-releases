# Upcoming Music Releases for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Never miss a new album from the bands you actually listen to. **Upcoming Music
Releases** builds a calendar of upcoming and recent album releases by the
artists in your **Discogs collection**, your **Spotify playlists** and the
artists you **follow on Spotify** — then:

- discovers the releases with the **MusicBrainz** release-group browse (no
  per-artist lookup, so scans are fast and rate-limit friendly);
- matches them against your artist pool with a strict, normalized
  artist-name intersection;
- annotates each matched release with its **Apple Music** album (cover,
  genre, exact title) so every calendar entry says what is actually coming out;
- gates entries on an **Apple genre allowlist** so only the genres you care
  about (default: the rock/metal family) reach your calendar;
- exposes everything as a **calendar**, two **sensors** and a **refresh
  button**.

## Prerequisites

| Requirement | Why | Link |
|---|---|---|
| **Discogs personal access token** | Reads your collection to build the artist pool | discogs.com → Settings → Developers |
| **SpotifyPlus** *(optional)* | Only needed for matching against Spotify playlists and followed artists | `spotifyplus` player integration |

## Installation

### Via HACS (recommended)

1. Open HACS in your Home Assistant sidebar.
2. Go to **Integrations → ⋮ → Custom repositories**.
3. Add `https://github.com/Thrasher2020/home-assistant-upcoming-music-releases` with category **Integration**.
4. Click **Explore & download repositories**, search for **Upcoming Music Releases**, and download it.
5. Restart Home Assistant.

### Manual

1. Copy the `custom_components/upcoming_music_releases` folder into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.

## Setup

1. Go to **Settings → Devices & Services → Add Integration**.
2. Search for **Upcoming Music Releases**.
3. Enter your **Discogs personal access token**.
4. *(Optional)* pick any of your **Spotify playlists** whose artists should also match.
5. *(Optional)* keep the **Include followed artists** toggle on to add the artists you follow on Spotify.

Then open **Options** to tune everything:

| Option | Description |
|---|---|
| Spotify playlists | Multi-select of playlists whose artists should match upcoming releases |
| Include followed artists | Also scan the artists you follow on Spotify |
| Market | Apple Music store country used for album lookups (default `GB`) |
| Lookahead months | How far into the future to scan for releases (1–24, default 12) |
| Include recent days | How many days of already-released albums to keep on the calendar (0–30, default 7) |
| Allowed genres | Comma-separated Apple genres allowed into the calendar (clear to disable the filter) |

## Entities

| Entity | Type | Description |
|---|---|---|
| `sensor.new_releases_upcoming` | Sensor | Number of upcoming releases, with diagnostics in attributes |
| `sensor.new_releases_status` | Sensor | Live scan status: stage, pool size, resolved/unresolved, rate-limited |
| `calendar.upcoming_music_releases` | Calendar | Calendar of upcoming and recent releases (all-day events) |
| `button.<entry>_refresh` | Button | Force an immediate release scan |

## How it works

1. Build the **artist pool**: Discogs collection artists + Spotify playlist
   artists + followed artists.
2. **MusicBrainz browse** finds release groups in the configured window
   (recent days back to lookahead months ahead).
3. A strict `_normalize` **artist-name intersection** keeps only pool artists'
   releases.
4. Each matched release group is looked up on **Apple Music** (market-aware) to
   pin down the exact album and its genre.
5. The **genre allowlist** gates entries; matched albums become calendar events.

The first scan runs automatically once Home Assistant finishes starting, then
every 12 hours.

## Troubleshooting

- **No calendar entries** — check `sensor.new_releases_status`: `pool_size` 0
  means the Discogs token or Spotify sources aren't producing artists, and the
  resolved/unresolved counters show how far the Apple annotation got.
- **Genre-blocked releases** — if expected albums are missing, check the
  `Allowed genres` option; Apple's `primaryGenreName` must match an entry
  (e.g. `Death Metal/Black Metal`, `Rock & Roll`).
- **Rate limiting** — the status sensor exposes `rate_limited`; MusicBrainz and
  Apple both throttle, and the integration backs off and retries.
- Enable debug logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.upcoming_music_releases: debug
```

## License

Apache-2.0. See [LICENSE](LICENSE).