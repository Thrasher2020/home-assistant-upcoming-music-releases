"""Button platform for the Upcoming Music Releases integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import UpcomingMusicReleasesCoordinator


class UpcomingMusicReleasesRefreshButton(
    CoordinatorEntity[UpcomingMusicReleasesCoordinator], ButtonEntity
):
    """A button that forces an immediate release scan."""

    _attr_icon = "mdi:refresh"
    _attr_name = "Refresh"

    def __init__(
        self,
        coordinator: UpcomingMusicReleasesCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_refresh"

    async def async_press(self) -> None:
        """Trigger a coordinator refresh."""
        await self.coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([UpcomingMusicReleasesRefreshButton(coordinator, entry)])