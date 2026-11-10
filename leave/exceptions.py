from __future__ import annotations


class LeaveError(Exception):
    """Base class for rule violations raised by the leave services."""


class NotAllowed(LeaveError):
    """The actor is not permitted to perform this action."""


class InvalidRequest(LeaveError):
    """The request itself is invalid (dates, leave type, user setup)."""


class InvalidState(LeaveError):
    """The request is not in a state that allows this action."""


class NoWorkingDaysError(InvalidRequest):
    """The date range contains no working days (weekends or holidays only)."""


class OverlapError(InvalidRequest):
    """The user already has leave on one or more of these days."""


class FloorViolationError(LeaveError):
    """Booking would drop a resource role below its floor.

    Carries dates and numbers only, never the names of colleagues.
    """

    def __init__(self, violations) -> None:
        self.violations = list(violations)
        dates = ", ".join(v.date.isoformat() for v in self.violations)
        super().__init__(f"Resource floor would be breached on: {dates}")
