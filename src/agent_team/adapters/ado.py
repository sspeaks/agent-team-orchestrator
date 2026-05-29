from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdoWorkItemRef:
    id: int
    title: str
    url: str | None = None


class AdoAdapter:
    """Placeholder for future Azure DevOps import/sync support.

    The MVP is local-first. ADO integration should be added here using read/update
    operations that work with normal work item permissions and do not require
    service hooks, custom fields, or process-template changes.
    """

    def import_ready_items(self) -> list[AdoWorkItemRef]:
        raise NotImplementedError("ADO adapter is planned but not implemented in the MVP")

