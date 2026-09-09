"""Provider-neutral source objects; snapshots must fail on partial reads."""
from dataclasses import dataclass
from typing import Protocol, Iterable


@dataclass(frozen=True)
class Task:
    id: str
    collection_id: str
    title: str
    notes: str
    completed: bool
    due: str | None = None
    completed_at: str | None = None
    recurring: bool = False
    revision: str = ''


@dataclass(frozen=True)
class Collection:
    id: str
    name: str
    writable: bool = True


class Provider(Protocol):
    name: str
    namespace: str

    def collections(self) -> Iterable[Collection]: ...
    def tasks(self, selected_ids: set[str]) -> Iterable[Task]: ...
    def get_task(self, task_id: str) -> Task | None: ...
    def complete(self, task_id: str, expected_revision: str, collection_id: str) -> Task: ...
