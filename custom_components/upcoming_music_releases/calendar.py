"""Calendar platform for the Upcoming Music Releases integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import UpcomingMusicReleasesCoordinator


class UpcomingMusicReleasesCalendar(
    CoordinatorEntity[UpcomingMusicReleasesCoordinator], CalendarEntity
):
    """A calendar of upcoming and recently released music from the artist pool."""

    _attr_icon = "mdi:calendar-music"
    _attr_name = "Upcoming Music Releases"

    def __init__(
        self,
        coordinator: UpcomingMusicReleasesCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the calendar."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_calendar"

    def _all_events(self) -> list[dict[str, Any]]:
        """Return all release events from the coordinator."""
        return list((self.coordinator.data or {}).get("events", []))

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming release, if any."""
        now = dt_util.now().date()
        for event in self._all_events():
            if not event.get("recent") and event["start"] >= now:
                return self._to_calendar_event(event)
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return all events within the requested window."""
        start = dt_util.as_local(start_date).date()
        end = dt_util.as_local(end_date).date()
        return [
            self._to_calendar_event(event)
            for event in self._all_events()
            if event["start"] <= end and event["end"] > start
        ]

    def _to_calendar_event(self, event: dict[str, Any]) -> CalendarEvent:
        """Convert a release event dict into a CalendarEvent (all-day)."""
        return CalendarEvent(
            summary=event["summary"],
            start=event["start"],
            end=event["end"],
            description=event.get("description"),
            uid=event.get("uid"),
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the calendar platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([UpcomingMusicReleasesCalendar(coordinator, entry)])