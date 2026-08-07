"""Repository layer: thin async persistence, no business logic.

Every repository takes an `AsyncSession` and returns ORM objects. None of them
open transactions, decide status, or write events on their own -- that is the
services' and jobs' job. Keeping the split strict is what makes the ordered
health rules testable without a database.
"""

from __future__ import annotations

from app.repositories.cluster_repo import ClusterRepository
from app.repositories.collection_repo import CollectionSnapshotRepository
from app.repositories.component_repo import ComponentStatusRepository
from app.repositories.event_repo import EventRepository, EventType, Severity
from app.repositories.health_repo import HealthCheckRepository

__all__ = [
    "ClusterRepository",
    "CollectionSnapshotRepository",
    "ComponentStatusRepository",
    "EventRepository",
    "EventType",
    "HealthCheckRepository",
    "Severity",
]
