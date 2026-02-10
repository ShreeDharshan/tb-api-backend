class ThingsBoardBackendError(Exception):
    """Base exception for backend domain failures."""


class ThingsBoardAuthError(ThingsBoardBackendError):
    """Raised when authentication to ThingsBoard fails."""


class ThingsBoardApiError(ThingsBoardBackendError):
    """Raised when a ThingsBoard API call fails."""
