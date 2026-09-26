"""The Upcoming Music Releases integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant, callback

from .const import DOMAIN
from .coordinator import UpcomingMusicReleasesCoordinator

PLATFORMS = ["calendar", "sensor", "button"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Upcoming Music Releases from a config entry."""
    coordinator = UpcomingMusicReleasesCoordinator(hass, entry)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _startup_refresh() -> None:
        """Run the first refresh once Home Assistant has fully started."""
        await coordinator.async_refresh()

    if hass.state == CoreState.running:
        entry.async_create_background_task(
            hass, _startup_refresh(), "upcoming_music_releases_startup"
        )
    else:
        listener_fired = False

        async def _on_start(event: Event) -> None:
            nonlocal listener_fired
            listener_fired = True
            await _startup_refresh()

        unsub = hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED, _on_start
        )

        @callback
        def _remove_listener() -> None:
            if not listener_fired:
                unsub()

        entry.async_on_unload(_remove_listener)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok