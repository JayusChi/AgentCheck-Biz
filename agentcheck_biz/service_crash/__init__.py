"""D24 owned process crashes and restart on the same persistent database."""

VERSION = "service-crash/1"
POINTS = ("before_service", "before_commit", "after_commit")
