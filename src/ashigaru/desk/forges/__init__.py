"""Forge adapters: the only code that knows a forge's headers, signatures and payloads.

A forge adapter verifies its own webhook signature, names the delivery, and maps
its events into the Desk's normalized shapes: a CIUpdate (one check of one
commit) and an IntakeItem (one issue, PR or ticket for the queue). The Desk
picks the adapter whose configured secret verifies a delivery, so one Desk
serves several forges, and nothing outside this package names one.

A forge posts to /webhook/<name>. A new forge is a new module here and a line
in FORGES.
"""

from . import github
from .base import CIUpdate, Delivery, Forge, IntakeItem

__all__ = ["DEFAULT", "FORGES", "CIUpdate", "Delivery", "Forge", "IntakeItem"]

FORGES: dict[str, Forge] = {forge.name: forge for forge in (github.FORGE,)}

DEFAULT = github.FORGE.name
"""The forge the bare ASHIGARU_WEBHOOK_SECRET belongs to, the name the first deploy used."""
