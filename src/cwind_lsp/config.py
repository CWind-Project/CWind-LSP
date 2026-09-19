"""Server configuration derived from ``initializationOptions``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Any, Mapping, Optional

ENV_STD_PATH = "CWIND_HOME"


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Settings:
    """User tunable settings.

    ``std_path`` points at the CWind installation owning ``libs/`` (the
    frontend's ``CWIND_HOME``).  Target keys pin ``#[cfg]`` evaluation to a
    platform other than the host.  ``entry_overrides`` maps file paths to the
    entry file that should be analysed for them.
    """

    std_path: Optional[str] = None
    target_os: Optional[str] = None
    target_arch: Optional[str] = None
    target_vendor: Optional[str] = None
    target_pointer_width: Optional[str] = None
    no_std: bool = False
    debounce_ms: int = 350
    diagnostics: bool = True
    semantic_tokens: bool = True
    completion: bool = True
    hover: bool = True
    max_problems: int = 500
    trace: bool = False
    entry_overrides: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_options(cls, options: Any) -> "Settings":
        if not isinstance(options, Mapping):
            return cls()
        nested = options.get("cwind")
        table: Mapping[str, Any] = nested if isinstance(nested, Mapping) else options
        known = {f.name for f in fields(cls)}
        settings = cls()
        for key in known:
            if key not in table:
                continue
            value = table[key]
            if key == "entry_overrides" and isinstance(value, Mapping):
                settings.entry_overrides = {str(k): str(v) for k, v in value.items()}
            elif key == "target_pointer_width" and value is not None:
                settings.target_pointer_width = str(value)
            elif key in ("debounce_ms", "max_problems"):
                parsed = _as_int(value)
                if parsed is not None:
                    setattr(settings, key, max(0, parsed))
            elif key in (
                "no_std",
                "diagnostics",
                "semantic_tokens",
                "completion",
                "hover",
                "trace",
            ):
                setattr(settings, key, bool(value))
            else:
                setattr(settings, key, value)
        settings.apply_environment()
        return settings

    def apply_environment(self) -> None:
        if self.std_path:
            os.environ[ENV_STD_PATH] = self.std_path

    def entry_override(self, path: str) -> Optional[str]:
        return self.entry_overrides.get(path)
