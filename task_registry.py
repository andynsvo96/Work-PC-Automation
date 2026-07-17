"""Serializable automation task registry used by the shared queue."""

from __future__ import annotations

import inspect
import re
import threading
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple


TASK_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")


class TaskRegistryError(RuntimeError):
    pass


class UnknownTaskType(TaskRegistryError):
    pass


class InvalidTaskArguments(TaskRegistryError):
    pass


def normalize_task_result(result: Any) -> Tuple[bool, str]:
    if isinstance(result, tuple):
        if len(result) >= 2:
            return bool(result[0]), str(result[1])
        if len(result) == 1:
            return bool(result[0]), str(result[0])
    if isinstance(result, Mapping):
        return bool(result.get("success")), str(result.get("message") or "Task finished.")
    if result is None:
        return True, "Task finished."
    return bool(result), str(result)


class TaskRegistry:
    def __init__(self):
        self._tasks: Dict[str, Callable[..., Any]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def validate_task_type(task_type: str) -> str:
        value = str(task_type or "").strip().lower()
        if not TASK_TYPE_PATTERN.fullmatch(value):
            raise ValueError(
                "Task type must start with a letter and contain only lowercase letters, "
                "numbers, dots, underscores, or hyphens."
            )
        return value

    def register(self, task_type: str, executor: Optional[Callable[..., Any]] = None):
        normalized = self.validate_task_type(task_type)

        def decorator(fn: Callable[..., Any]):
            if not callable(fn):
                raise TypeError("Task executor must be callable.")
            with self._lock:
                if normalized in self._tasks and self._tasks[normalized] is not fn:
                    raise TaskRegistryError(f"Task type is already registered: {normalized}")
                self._tasks[normalized] = fn
            return fn

        return decorator(executor) if executor is not None else decorator

    def unregister(self, task_type: str) -> None:
        with self._lock:
            self._tasks.pop(self.validate_task_type(task_type), None)

    def task_types(self) -> Iterable[str]:
        with self._lock:
            return tuple(sorted(self._tasks))

    def has(self, task_type: str) -> bool:
        try:
            normalized = self.validate_task_type(task_type)
        except ValueError:
            return False
        with self._lock:
            return normalized in self._tasks

    def execute(self, task_type: str, arguments: Optional[Mapping[str, Any]] = None) -> Tuple[bool, str]:
        normalized = self.validate_task_type(task_type)
        with self._lock:
            executor = self._tasks.get(normalized)
        if executor is None:
            raise UnknownTaskType(f"This app version cannot execute task type: {normalized}")
        if arguments is None:
            kwargs: Dict[str, Any] = {}
        elif isinstance(arguments, Mapping):
            kwargs = dict(arguments)
        else:
            raise InvalidTaskArguments("Task arguments must be an object.")
        try:
            inspect.signature(executor).bind(**kwargs)
        except TypeError as exc:
            raise InvalidTaskArguments(f"Invalid arguments for {normalized}: {exc}") from exc
        return normalize_task_result(executor(**kwargs))
