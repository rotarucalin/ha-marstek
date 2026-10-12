"""Tests for debug logging activation announcements."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import EVENT_LOGGING_CHANGED

from custom_components.marstek import async_setup


@pytest.mark.parametrize("initial_level", [logging.WARNING, logging.DEBUG])
async def test_debug_announcement_once_per_activation(
    hass, caplog, monkeypatch, initial_level
):
    """Announce startup debug and later activations without duplicates."""
    logger = logging.getLogger("custom_components.marstek")
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(logger, "level", initial_level)
    logger.setLevel(initial_level)
    with patch(
        "custom_components.marstek.async_get_integration",
        new=AsyncMock(return_value=SimpleNamespace(version="9.8.7")),
    ):
        assert await async_setup(hass, {})

    message = "Debug info active - Marstek integration version 9.8.7"

    def announcement_count():
        return sum(record.message == message for record in caplog.records)

    assert announcement_count() == (initial_level == logging.DEBUG)
    for level, expected in (
        (logging.DEBUG, 1),
        (logging.DEBUG, 1),  # Unrelated logging changes do not repeat the line.
        (logging.WARNING, 1),
        (logging.DEBUG, 2),
    ):
        logger.setLevel(level)
        hass.bus.async_fire(EVENT_LOGGING_CHANGED)
        await hass.async_block_till_done()
        assert announcement_count() == expected
