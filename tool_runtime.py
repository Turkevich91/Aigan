from __future__ import annotations

import inspect
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from system_log import sanitize_details, sanitize_text


class ToolAdapter(Protocol):
    def health_summary(self) -> dict[str, Any]:
        ...


ToolEventCallback = Callable[..., None]
ToolOperation = Callable[[], Any | Awaitable[Any]]


@dataclass(frozen=True)
class ToolHealth:
    name: str
    enabled: bool
    adapter: str
    status: str = "ok"
    error_count: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "name": self.name,
            "enabled": self.enabled,
            "adapter": self.adapter,
            "status": self.status,
            "error_count": self.error_count,
        }
        data.update(self.details)
        return data


class NullToolAdapter:
    def __init__(self, name: str = "null") -> None:
        self.name = name

    def health_summary(self) -> dict[str, Any]:
        return ToolHealth(
            name=self.name,
            enabled=False,
            adapter="null",
            status="disabled",
            error_count=0,
        ).to_dict()

    async def cleanup(self) -> None:
        return None


class ToolRuntime:
    def __init__(self, *, event_callback: ToolEventCallback | None = None) -> None:
        self._adapters: dict[str, ToolAdapter] = {}
        self._error_counts: dict[str, int] = {}
        self._event_callback = event_callback

    def set_event_callback(self, event_callback: ToolEventCallback | None) -> None:
        self._event_callback = event_callback

    def register(self, name: str, adapter: ToolAdapter) -> None:
        clean_name = self._clean_name(name)
        self._adapters[clean_name] = adapter
        self._error_counts.setdefault(clean_name, 0)

    def get(self, name: str) -> ToolAdapter | None:
        return self._adapters.get(self._clean_name(name))

    def clear_error_counts(self) -> None:
        self._error_counts.clear()

    async def safe_call(
        self,
        name: str,
        operation: str,
        callback: ToolOperation,
        *,
        default: Any = None,
        details: dict[str, Any] | None = None,
        event_context: dict[str, Any] | None = None,
    ) -> Any:
        clean_name = self._clean_name(name)
        started = time.monotonic()
        try:
            result = callback()
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as exc:
            self._error_counts[clean_name] = self._error_counts.get(clean_name, 0) + 1
            duration_ms = int((time.monotonic() - started) * 1000)
            self._emit_failure(
                clean_name,
                operation,
                exc,
                duration_ms=duration_ms,
                details=details,
                event_context=event_context,
            )
            return default

    async def cleanup(self) -> None:
        for name, adapter in list(self._adapters.items()):
            cleanup = getattr(adapter, "cleanup", None)
            if cleanup is None:
                continue
            cleanup_func = cleanup
            await self.safe_call(name, "cleanup", lambda cleanup_func=cleanup_func: cleanup_func())

    def health_summary(self) -> dict[str, Any]:
        adapters = [self._adapter_health(name, adapter) for name, adapter in sorted(self._adapters.items())]
        error_count = sum(int(item.get("error_count", 0)) for item in adapters)
        degraded = any(item.get("status") in {"degraded", "error"} for item in adapters)
        return {
            "status": "degraded" if degraded else "ok",
            "adapter_count": len(adapters),
            "error_count": error_count,
            "adapters": adapters,
        }

    def _adapter_health(self, name: str, adapter: ToolAdapter) -> dict[str, Any]:
        try:
            raw = dict(adapter.health_summary() or {})
        except Exception as exc:
            runtime_errors = self._error_counts.get(name, 0) + 1
            self._error_counts[name] = runtime_errors
            return ToolHealth(
                name=name,
                enabled=False,
                adapter=type(adapter).__name__,
                status="error",
                error_count=runtime_errors,
                details={"health_error": sanitize_text(f"{type(exc).__name__}: {exc}", 200)},
            ).to_dict()

        runtime_errors = self._error_counts.get(name, 0)
        adapter_errors = int(raw.get("error_count", 0) or 0)
        status = str(raw.get("status") or "").strip().lower()
        enabled = bool(raw.get("enabled", False))
        if not status:
            status = "ok" if enabled else "disabled"
        if runtime_errors and status == "ok":
            status = "degraded"

        raw["name"] = str(raw.get("name") or name)
        raw["enabled"] = enabled
        raw["adapter"] = str(raw.get("adapter") or type(adapter).__name__)
        raw["status"] = status
        raw["runtime_error_count"] = runtime_errors
        raw["error_count"] = adapter_errors + runtime_errors
        return raw

    def _emit_failure(
        self,
        name: str,
        operation: str,
        exc: Exception,
        *,
        duration_ms: int,
        details: dict[str, Any] | None,
        event_context: dict[str, Any] | None,
    ) -> None:
        if self._event_callback is None:
            return
        clean_details = {
            "tool": name,
            "operation": operation,
            "exception_type": type(exc).__name__,
            "exception_message": sanitize_text(str(exc), 300),
        }
        if details:
            clean_details.update(sanitize_details(details))
        kwargs = {
            "level": "warning",
            "component": "tool_runtime",
            "event_type": "tool_operation_failed",
            "duration_ms": duration_ms,
            "message": sanitize_text(f"{name}.{operation}: {type(exc).__name__}: {exc}", 300),
            "details": clean_details,
        }
        if event_context:
            kwargs.update(event_context)
        try:
            self._event_callback(**kwargs)
        except Exception:
            return

    def _clean_name(self, name: str) -> str:
        clean = str(name or "").strip()
        if not clean:
            raise ValueError("Tool adapter name must not be empty")
        return clean
