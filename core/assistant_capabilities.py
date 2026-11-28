"""Explicit exclusions for account-only browser operations."""
from tenders.assistant_tools import register_capability_exclusion


register_capability_exclusion("core.avatar.optimize_avatar", "Binary account avatar upload requires the authenticated browser form.")
register_capability_exclusion("core.avatar.optimize_background", "Binary account background upload requires the authenticated browser form.")
