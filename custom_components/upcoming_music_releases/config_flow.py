"""Config flow for the Upcoming Music Releases integration."""

from __future__ import annotations

from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import SERVER_SOFTWARE, async_get_clientsession

from .const import (
    CONF_ALLOWED_GENRES,
    CONF_DISCOGS_TOKEN,
    CONF_DISCOGS_USERNAME,
    CONF_INCLUDE_FOLLOWED,
    CONF_INCLUDE_RECENT_DAYS,
    CONF_LOOKAHEAD_MONTHS,
    CONF_MARKET,
    CONF_SPOTIFY_PLAYER_ENTITY_ID,
    CONF_SPOTIFY_PLAYLIST_ID,
    CONF_SPOTIFY_PLAYLIST_NAME,
    DEFAULT_ALLOWED_GENRES,
    DEFAULT_INCLUDE_FOLLOWED,
    DEFAULT_INCLUDE_RECENT_DAYS,
    DEFAULT_LOOKAHEAD_MONTHS,
    DEFAULT_MARKET,
    DISCOGS_API_BASE_URL,
    DOMAIN,
    NAME,
    SERVICE_GET_PLAYLIST_FAVORITES,
    SPOTIFY_DOMAIN,
)

_TIMEOUT = aiohttp.ClientTimeout(connect=5, total=15)


async def _async_get_identity(hass, token: str) -> str | None:
    """Return the Discogs username for a personal access token, or None on failure."""
    session = async_get_clientsession(hass)
    headers = {"Authorization": f"Discogs token={token}", "User-Agent": SERVER_SOFTWARE}
    async with session.get(
        f"{DISCOGS_API_BASE_URL}/oauth/identity", headers=headers, timeout=_TIMEOUT
    ) as response:
        response.raise_for_status()
        data = await response.json(content_type=None)
        return data.get("username")


def _spotifyplus_available(hass) -> bool:
    """Return True when SpotifyPlus is running with a usable service."""
    if not hass.data.get(SPOTIFY_DOMAIN):
        return False
    return hass.services.has_service(SPOTIFY_DOMAIN, SERVICE_GET_PLAYLIST_FAVORITES)


def _spotify_player_entity_id(hass) -> str | None:
    """Return the first SpotifyPlus media player entity id, if any."""
    for instance in hass.data.get(SPOTIFY_DOMAIN, {}).values():
        player = getattr(instance, "media_player", None)
        if player is not None and player.entity_id:
            return player.entity_id
    return None


async def _async_get_playlists(hass, player_entity_id: str) -> dict[str, str]:
    """Return the user's Spotify playlists as {id: name}."""
    response = await hass.services.async_call(
        SPOTIFY_DOMAIN,
        SERVICE_GET_PLAYLIST_FAVORITES,
        {"limit_total": 200},
        target={"entity_id": player_entity_id},
        blocking=True,
        return_response=True,
    )
    result = (response or {}).get("result") or {}
    items = result.get("items")
    if not isinstance(items, list):
        items = (result.get("playlists") or {}).get("items")
    playlists: dict[str, str] = {}
    for item in items or []:
        name = item.get("name")
        playlist_id = item.get("id")
        if name and playlist_id:
            playlists[playlist_id] = name
    return dict(sorted(playlists.items(), key=lambda pair: pair[1].lower()))


def _normalize_selection(value: Any, playlists: dict[str, str]) -> dict[str, str]:
    """Return the selected playlists as {id: name}, whatever shape the UI sent."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, (list, set, tuple, str)):
        values = value if isinstance(value, str) else list(value)
        if isinstance(values, str):
            values = [values] if values else []
        return {item: playlists.get(item, item) for item in values}
    return {}


class UpcomingMusicReleasesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Upcoming Music Releases."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> UpcomingMusicReleasesOptionsFlow:
        """Return the options flow for this handler."""
        return UpcomingMusicReleasesOptionsFlow(config_entry)

    def __init__(self) -> None:
        """Initialize the flow."""
        self._token: str | None = None
        self._username: str | None = None
        self._player_entity_id: str | None = None
        self._playlists: dict[str, str] = {}
        self._selected_playlists: dict[str, str] = {}
        self._include_followed = DEFAULT_INCLUDE_FOLLOWED

    async def _async_create_entry(self) -> ConfigFlowResult:
        """Create the config entry with the selected artist sources."""
        data: dict[str, Any] = {
            CONF_DISCOGS_TOKEN: self._token,
            CONF_DISCOGS_USERNAME: self._username,
            CONF_INCLUDE_FOLLOWED: self._include_followed,
        }
        if self._selected_playlists:
            data.update(
                {
                    CONF_SPOTIFY_PLAYLIST_ID: list(self._selected_playlists),
                    CONF_SPOTIFY_PLAYLIST_NAME: dict(self._selected_playlists),
                    CONF_SPOTIFY_PLAYER_ENTITY_ID: self._player_entity_id,
                }
            )
        return self.async_create_entry(title=NAME, data=data)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the Discogs token step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            token = user_input[CONF_DISCOGS_TOKEN]
            try:
                username = await _async_get_identity(self.hass, token)
            except aiohttp.ClientResponseError as err:
                if err.status == 401:
                    errors["base"] = "invalid_token"
                else:
                    errors["base"] = "cannot_connect"
            except aiohttp.ClientError:
                errors["base"] = "cannot_connect"
            else:
                if username is None:
                    errors["base"] = "invalid_token"
                else:
                    await self.async_set_unique_id(DOMAIN)
                    self._abort_if_unique_id_configured()
                    self._token = token
                    self._username = username
                    self._player_entity_id = _spotify_player_entity_id(self.hass)
                    if _spotifyplus_available(self.hass) and self._player_entity_id:
                        try:
                            self._playlists = await _async_get_playlists(
                                self.hass, self._player_entity_id
                            )
                        except HomeAssistantError:
                            self._playlists = {}
                        if self._playlists:
                            return await self.async_step_spotify()
                    return await self._async_create_entry()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_DISCOGS_TOKEN): str}),
            errors=errors,
        )

    async def async_step_spotify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the playlist selection and followed-artist toggle."""
        if user_input is not None:
            self._selected_playlists = _normalize_selection(
                user_input.get(CONF_SPOTIFY_PLAYLIST_ID), self._playlists
            )
            self._include_followed = bool(
                user_input.get(CONF_INCLUDE_FOLLOWED, DEFAULT_INCLUDE_FOLLOWED)
            )
            return await self._async_create_entry()

        return self.async_show_form(
            step_id="spotify",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SPOTIFY_PLAYLIST_ID, default=[]
                    ): cv.multi_select(self._playlists),
                    vol.Optional(
                        CONF_INCLUDE_FOLLOWED, default=DEFAULT_INCLUDE_FOLLOWED
                    ): bool,
                }
            ),
        )


class UpcomingMusicReleasesOptionsFlow(OptionsFlow):
    """Handle an options flow for Upcoming Music Releases."""

    VERSION = 1

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the options flow."""
        self._entry = entry
        self._player_entity_id: str | None = None
        self._playlists: dict[str, str] = {}

    def _current_playlists(self) -> list[str]:
        """Return the playlists currently configured on the entry."""
        value = self._entry.data.get(CONF_SPOTIFY_PLAYLIST_ID)
        if isinstance(value, str):
            return [value] if value else []
        if isinstance(value, list):
            return [item for item in value if item]
        return []

    async def _load_playlists(self) -> None:
        """Load fresh playlists when possible, else fall back to stored names."""
        self._player_entity_id = self._entry.data.get(CONF_SPOTIFY_PLAYER_ENTITY_ID)
        if self._player_entity_id and _spotifyplus_available(self.hass):
            try:
                self._playlists = await _async_get_playlists(
                    self.hass, self._player_entity_id
                )
                return
            except HomeAssistantError:
                pass
        stored = self._entry.data.get(CONF_SPOTIFY_PLAYLIST_NAME)
        if isinstance(stored, dict):
            self._playlists = dict(stored)
        elif isinstance(stored, str):
            current = self._current_playlists()
            self._playlists = {playlist_id: stored for playlist_id in current}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the artist pool and release window."""
        if user_input is None:
            await self._load_playlists()
            current_playlists = [
                playlist_id
                for playlist_id in self._current_playlists()
                if playlist_id in self._playlists
            ]
            return self.async_show_form(
                step_id="init",
                data_schema=vol.Schema(
                    {
                        vol.Optional(
                            CONF_SPOTIFY_PLAYLIST_ID, default=current_playlists
                        ): cv.multi_select(self._playlists or {}),
                        vol.Optional(
                            CONF_INCLUDE_FOLLOWED,
                            default=self._entry.data.get(
                                CONF_INCLUDE_FOLLOWED, DEFAULT_INCLUDE_FOLLOWED
                            ),
                        ): bool,
                        vol.Optional(
                            CONF_MARKET,
                            default=self._entry.data.get(CONF_MARKET, DEFAULT_MARKET),
                        ): str,
                        vol.Optional(
                            CONF_LOOKAHEAD_MONTHS,
                            default=self._entry.data.get(
                                CONF_LOOKAHEAD_MONTHS, DEFAULT_LOOKAHEAD_MONTHS
                            ),
                        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=24)),
                        vol.Optional(
                            CONF_INCLUDE_RECENT_DAYS,
                            default=self._entry.data.get(
                                CONF_INCLUDE_RECENT_DAYS, DEFAULT_INCLUDE_RECENT_DAYS
                            ),
                        ): vol.All(vol.Coerce(int), vol.Range(min=0, max=30)),
                        vol.Optional(
                            CONF_ALLOWED_GENRES,
                            default=self._entry.data.get(
                                CONF_ALLOWED_GENRES, DEFAULT_ALLOWED_GENRES
                            ),
                        ): str,
                    }
                ),
            )

        data: dict[str, Any] = dict(self._entry.data)
        selected = _normalize_selection(
            user_input.get(CONF_SPOTIFY_PLAYLIST_ID), self._playlists
        )
        if selected:
            data[CONF_SPOTIFY_PLAYLIST_ID] = list(selected)
            data[CONF_SPOTIFY_PLAYLIST_NAME] = dict(selected)
            if not data.get(CONF_SPOTIFY_PLAYER_ENTITY_ID):
                data[CONF_SPOTIFY_PLAYER_ENTITY_ID] = self._player_entity_id
        else:
            data.pop(CONF_SPOTIFY_PLAYLIST_ID, None)
            data.pop(CONF_SPOTIFY_PLAYLIST_NAME, None)
            data.pop(CONF_SPOTIFY_PLAYER_ENTITY_ID, None)

        data[CONF_INCLUDE_FOLLOWED] = bool(
            user_input.get(CONF_INCLUDE_FOLLOWED, DEFAULT_INCLUDE_FOLLOWED)
        )
        market = str(user_input.get(CONF_MARKET) or DEFAULT_MARKET).strip().upper()
        data[CONF_MARKET] = market or DEFAULT_MARKET
        data[CONF_LOOKAHEAD_MONTHS] = int(
            user_input.get(CONF_LOOKAHEAD_MONTHS) or DEFAULT_LOOKAHEAD_MONTHS
        )
        data[CONF_INCLUDE_RECENT_DAYS] = int(
            user_input.get(CONF_INCLUDE_RECENT_DAYS) or DEFAULT_INCLUDE_RECENT_DAYS
        )
        allowed = user_input.get(CONF_ALLOWED_GENRES)
        data[CONF_ALLOWED_GENRES] = (
            str(allowed).strip() if allowed is not None else DEFAULT_ALLOWED_GENRES
        )

        self.hass.config_entries.async_update_entry(self._entry, data=data)
        return self.async_create_entry(title="", data=None)