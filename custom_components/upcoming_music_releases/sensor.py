"""Sensor platform for the Upcoming Music Releases integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import UpcomingMusicReleasesCoordinator


class UpcomingMusicReleasesSensor(
    CoordinatorEntity[UpcomingMusicReleasesCoordinator], SensorEntity
):
    """A sensor reporting how many releases are coming up."""

    _attr_icon = "mdi:music-note-plus"
    _attr_name = "New Releases Upcoming"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: UpcomingMusicReleasesCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_upcoming"

    @property
    def native_value(self) -> int:
        """Return the count of upcoming releases."""
        return int((self.coordinator.data or {}).get("upcoming", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return diagnostic attributes for the release scan."""
        data = self.coordinator.data or {}
        events = data.get("events", [])
        future = [event for event in events if not event.get("recent")]
        next_release = future[0] if future else None
        return {
            "next_release": (
                f"{next_release['summary']} ({next_release['start'].isoformat()})"
                if next_release
                else None
            ),
            "recent": int(data.get("recent", 0)),
            "artists": int(data.get("artists", 0)),
            "sources": data.get("sources") or {},
            "apple_resolved": int(data.get("apple_resolved", 0)),
            "apple_unresolved": int(data.get("apple_unresolved", 0)),
        }


class UpcomingMusicReleasesStatusSensor(
    CoordinatorEntity[UpcomingMusicReleasesCoordinator], SensorEntity
):
    """A sensor reporting live progress of the release scan."""

    _attr_icon = "mdi:calendar-clock"
    _attr_name = "New Releases Status"

    def __init__(
        self,
        coordinator: UpcomingMusicReleasesCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str:
        """Return the current scan status text."""
        return str(self.coordinator.status.get("message") or "Waiting")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the live scan-progress counters."""
        status = self.coordinator.status
        return {
            "stage": status.get("stage"),
            "pool_size": int(status.get("pool_size", 0)),
            "apple_resolved": int(status.get("resolved", 0)),
            "apple_unresolved": int(status.get("unresolved", 0)),
            "albums_fetched": int(status.get("albums_done", 0)),
            "albums_total": int(status.get("albums_total", 0)),
            "upcoming": int(status.get("upcoming", 0)),
            "recent": int(status.get("recent", 0)),
            "started_at": status.get("started_at"),
            "finished_at": status.get("finished_at"),
            "rate_limited": bool(status.get("rate_limited")),
        }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            UpcomingMusicReleasesSensor(coordinator, entry),
            UpcomingMusicReleasesStatusSensor(coordinator, entry),
        ]
    )