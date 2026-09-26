# Changelog

## v0.1.0 — Initial release

- Artist pool from Discogs collection, Spotify playlists and followed artists.
- MusicBrainz release-group browse for upcoming/recent releases, with
  split-chunk queries and the NOT-clause so exact-date releases are not lost.
- Strict normalized artist-name intersection between the browse results and
  the artist pool.
- Apple Music album annotation (title, genre, cover) per matched release group.
- Apple genre allowlist gate, defaulting to the rock/metal family, configurable
  via the options flow.
- Calendar, two sensors and a refresh button; first scan on Home Assistant
  start, then every 12 hours.