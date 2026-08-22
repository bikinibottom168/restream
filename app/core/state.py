"""Channel state machine.

    DISABLED / CONFIG_REQUIRED / UNSUPPORTED
                    |
                  START
                    v
                 STARTING  -> ONLINE -> DEGRADED -> RECONNECTING -> ONLINE
                                                        |
                                                        v
                                                     OFFLINE  (watchdog keeps
                                                               retrying with
                                                               backoff)
"""

from __future__ import annotations

from enum import Enum


class ChannelState(str, Enum):
    """Every state a channel can be in."""

    #: Turned off by the operator; the watchdog ignores it entirely.
    DISABLED = "DISABLED"
    #: Enabled but missing an RTMP destination, so FFmpeg must not start.
    CONFIG_REQUIRED = "CONFIG_REQUIRED"
    #: The source needs a decryption capability this project does not provide.
    UNSUPPORTED = "UNSUPPORTED"
    #: Enabled and configured, but not currently running.
    STOPPED = "STOPPED"
    #: Resolving the source / validating / launching FFmpeg.
    STARTING = "STARTING"
    #: FFmpeg is running and progress is advancing.
    ONLINE = "ONLINE"
    #: Running, but a health signal has failed at least once.
    DEGRADED = "DEGRADED"
    #: Confirmed broken; the supervisor is refreshing and restarting.
    RECONNECTING = "RECONNECTING"
    #: Recovery has failed for now; retried with backoff.
    OFFLINE = "OFFLINE"
    #: A configuration or environment error a retry will not fix.
    ERROR = "ERROR"

    # ------------------------------------------------------------------ #
    @property
    def is_running(self) -> bool:
        """States in which an FFmpeg process is expected to exist."""
        return self in {ChannelState.ONLINE, ChannelState.DEGRADED}

    @property
    def is_active(self) -> bool:
        """States in which the supervisor task should be alive."""
        return self in {
            ChannelState.STARTING,
            ChannelState.ONLINE,
            ChannelState.DEGRADED,
            ChannelState.RECONNECTING,
            ChannelState.OFFLINE,
        }

    @property
    def is_down(self) -> bool:
        """States that count as an outage for notification purposes."""
        return self in {
            ChannelState.RECONNECTING,
            ChannelState.OFFLINE,
            ChannelState.ERROR,
        }

    @property
    def badge(self) -> str:
        """Bootstrap contextual class used by the dashboard."""
        return _BADGES[self]

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


_BADGES: dict[ChannelState, str] = {
    ChannelState.ONLINE: "success",
    ChannelState.DEGRADED: "warning",
    ChannelState.RECONNECTING: "orange",
    ChannelState.STARTING: "info",
    ChannelState.OFFLINE: "danger",
    ChannelState.ERROR: "danger",
    ChannelState.STOPPED: "secondary",
    ChannelState.DISABLED: "secondary",
    ChannelState.CONFIG_REQUIRED: "config",
    ChannelState.UNSUPPORTED: "config",
}


#: Allowed transitions. Anything not listed here is a bug and is logged.
ALLOWED_TRANSITIONS: dict[ChannelState, frozenset[ChannelState]] = {
    ChannelState.DISABLED: frozenset(
        {ChannelState.STOPPED, ChannelState.CONFIG_REQUIRED, ChannelState.STARTING}
    ),
    ChannelState.CONFIG_REQUIRED: frozenset(
        {ChannelState.STOPPED, ChannelState.DISABLED, ChannelState.STARTING}
    ),
    ChannelState.UNSUPPORTED: frozenset(
        {ChannelState.STOPPED, ChannelState.DISABLED, ChannelState.STARTING}
    ),
    ChannelState.STOPPED: frozenset(
        {
            ChannelState.STARTING,
            ChannelState.DISABLED,
            ChannelState.CONFIG_REQUIRED,
            ChannelState.UNSUPPORTED,
            ChannelState.ERROR,
        }
    ),
    ChannelState.STARTING: frozenset(
        {
            ChannelState.ONLINE,
            ChannelState.RECONNECTING,
            ChannelState.OFFLINE,
            ChannelState.STOPPED,
            ChannelState.DISABLED,
            ChannelState.ERROR,
            ChannelState.CONFIG_REQUIRED,
            ChannelState.UNSUPPORTED,
        }
    ),
    ChannelState.ONLINE: frozenset(
        {
            ChannelState.DEGRADED,
            ChannelState.RECONNECTING,
            ChannelState.STOPPED,
            ChannelState.DISABLED,
            ChannelState.OFFLINE,
            ChannelState.ERROR,
        }
    ),
    ChannelState.DEGRADED: frozenset(
        {
            ChannelState.ONLINE,
            ChannelState.RECONNECTING,
            ChannelState.STOPPED,
            ChannelState.DISABLED,
            ChannelState.OFFLINE,
            ChannelState.ERROR,
        }
    ),
    ChannelState.RECONNECTING: frozenset(
        {
            ChannelState.STARTING,
            ChannelState.ONLINE,
            ChannelState.OFFLINE,
            ChannelState.STOPPED,
            ChannelState.DISABLED,
            ChannelState.ERROR,
            ChannelState.UNSUPPORTED,
        }
    ),
    ChannelState.OFFLINE: frozenset(
        {
            ChannelState.STARTING,
            ChannelState.RECONNECTING,
            ChannelState.ONLINE,
            ChannelState.STOPPED,
            ChannelState.DISABLED,
            ChannelState.ERROR,
            ChannelState.UNSUPPORTED,
        }
    ),
    ChannelState.ERROR: frozenset(
        {
            ChannelState.STARTING,
            ChannelState.STOPPED,
            ChannelState.DISABLED,
            ChannelState.CONFIG_REQUIRED,
            ChannelState.OFFLINE,
        }
    ),
}


def can_transition(current: ChannelState, target: ChannelState) -> bool:
    """Return True when ``current -> target`` is a legal move."""
    if current == target:
        return True
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())


def parse_state(value: str | ChannelState | None) -> ChannelState:
    """Coerce a stored string into a :class:`ChannelState`, defaulting safely."""
    if isinstance(value, ChannelState):
        return value
    if not value:
        return ChannelState.STOPPED
    try:
        return ChannelState(str(value).upper())
    except ValueError:
        return ChannelState.STOPPED


#: Filters offered by the dashboard's status dropdown.
FILTER_GROUPS: dict[str, tuple[ChannelState, ...]] = {
    "all": tuple(ChannelState),
    "online": (ChannelState.ONLINE, ChannelState.DEGRADED),
    "offline": (ChannelState.OFFLINE, ChannelState.ERROR, ChannelState.STOPPED),
    "reconnecting": (ChannelState.RECONNECTING, ChannelState.STARTING),
    "disabled": (
        ChannelState.DISABLED,
        ChannelState.CONFIG_REQUIRED,
        ChannelState.UNSUPPORTED,
    ),
}
