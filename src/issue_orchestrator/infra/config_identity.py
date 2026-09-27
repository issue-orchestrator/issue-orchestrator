"""Identity helpers for one fully resolved engine configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..domain.repository_launch_selection import RepositoryLaunchSelection
from ..domain.runtime_config import RuntimeConfigReference
from .config_paths import (
    require_engine_launch_config_path,
    selection_from_config_path,
)

EXPECTED_CONFIG_FINGERPRINT_ENV = "ISSUE_ORCHESTRATOR_EXPECTED_CONFIG_FINGERPRINT"


class ConfigurationFingerprintMismatch(RuntimeError):
    """Raised when config bytes changed after startup preflight."""



def effective_config_fingerprint(data: dict[str, Any]) -> str:
    """Hash one canonical JSON document (sorted keys, compact, ``str`` fallback)."""
    encoded = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def assert_expected_config_fingerprint(
    observed: str,
    expected: str | None,
) -> None:
    """Fail startup when observed config bytes differ from the preflight snapshot."""
    if expected is not None and observed != expected:
        raise ConfigurationFingerprintMismatch(
            "Configuration changed after preflight: "
            f"expected={expected} observed={observed}"
        )


#: Fields that are identity or input bookkeeping, never runtime state.
_NOT_RUNTIME_STATE = frozenset({
    "launch_selection", "config_fingerprint", "session_binding_fingerprint",
    "loaded_effective_state", "config_path", "raw_data", "raw_agents",
})

#: Marks a runtime-override path that a mutation removed.
_REMOVED = "<removed>"


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """``{"a": {"b": 1}}`` -> ``{"a.b": 1}``; lists and scalars are leaves."""
    if isinstance(value, dict) and value:
        flat: dict[str, Any] = {}
        for key, item in value.items():
            flat.update(_flatten(item, f"{prefix}{key}."))
        return flat
    return {prefix[:-1]: value}


def _without_path(data: Any, path: str) -> Any:
    """``data`` minus the value at dotted ``path`` (a copy; absent paths no-op)."""
    head, _, rest = path.partition(".")
    if not isinstance(data, dict) or head not in data:
        return data
    trimmed = dict(data)
    if rest:
        trimmed[head] = _without_path(data[head], rest)
    else:
        del trimmed[head]
    return trimmed


def _under(path: str, roots: frozenset[str]) -> bool:
    return any(path == root or path.startswith(root + ".") for root in roots)


@dataclass
class ConfigLaunchIdentity:
    """Reusable typed view of the launch identity carried by Config.

    Both fingerprints hash what the OPERATOR configured - the effective YAML
    (after CLI ``path=value`` overrides and ``${VAR}`` expansion, kept as
    ``raw_data``) plus every runtime override applied to the loaded config
    since (CLI flags, test mode) - never the full dataclass state. The code's
    schema and defaults are not configuration: before this, adding a defaulted
    field or changing a default changed the fingerprint of every unchanged
    YAML, so upgrading with live sessions refused to restore them (#7347
    follow-up).

    ``session_binding_fingerprint`` is the part a restored session is bound to:
    the same input minus every setting a running engine applies live (the
    settings schema's non-``restart_required`` fields), since changing one of
    those cannot make a live session wrong.
    """

    launch_selection: RepositoryLaunchSelection = field(
        default_factory=RepositoryLaunchSelection.default
    )
    config_fingerprint: str = ""
    session_binding_fingerprint: str = ""
    #: The runtime state as loaded, before runtime overrides; empty when the
    #: configuration was built in code rather than loaded (then the code's
    #: defaults are the baseline).
    loaded_effective_state: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def configuration_mode(self) -> str:
        """Return the selected mode name for diagnostics and child processes."""
        return self.launch_selection.mode.value

    @property
    def config_name(self) -> str:
        """Return the selected config filename."""
        return self.launch_selection.config.value

    def launch_identity_dict(self) -> dict[str, str]:
        """Return public mode/config/fingerprint attribution."""
        return {
            **self.launch_selection.to_dict(),
            "config_fingerprint": self.config_fingerprint,
        }

    def record_loaded_state(self) -> None:
        """Mark the just-loaded state as the baseline runtime overrides differ from."""
        self.loaded_effective_state = self._runtime_state()

    def refresh_config_fingerprint(self) -> str:
        """Recompute both fingerprints from the operator's input (see class doc)."""
        from .settings_schema import TAB_DEFINITIONS
        from .settings_schema_support import collect_live_applied_settings

        operator_input: dict[str, Any] = dict(getattr(self, "raw_data", {}) or {})
        overrides = self._runtime_overrides()
        self.config_fingerprint = effective_config_fingerprint(
            {"input": operator_input, "runtime_overrides": overrides}
        )
        live = collect_live_applied_settings(TAB_DEFINITIONS)
        binding_input: Any = operator_input
        for path in live.yaml_paths:
            binding_input = _without_path(binding_input, path)
        self.session_binding_fingerprint = effective_config_fingerprint({
            "input": binding_input,
            "runtime_overrides": {
                path: value for path, value in overrides.items()
                if not _under(path, live.config_attrs)
            },
        })
        return self.config_fingerprint

    def _runtime_state(self) -> dict[str, Any]:
        state = {
            key: value for key, value in asdict(self).items()
            if key not in _NOT_RUNTIME_STATE
        }
        return _flatten(json.loads(json.dumps(state, sort_keys=True, default=str)))

    def _runtime_overrides(self) -> dict[str, Any]:
        """Every runtime value changed since load (or from the code's defaults)."""
        baseline = self.loaded_effective_state or type(self)()._runtime_state()
        current = self._runtime_state()
        changed: dict[str, Any] = {
            path: value for path, value in current.items()
            if path not in baseline or baseline[path] != value
        }
        changed.update({path: _REMOVED for path in baseline if path not in current})
        return changed


class RuntimeConfigReferenceOwner:
    """Construct a verified runtime reference from loaded configuration state."""

    config_path: Path | None
    launch_selection: RepositoryLaunchSelection

    def runtime_config_reference(self) -> RuntimeConfigReference:
        """Validate file existence and storage-derived mode at the infra boundary."""
        if self.config_path is None:
            raise ValueError("Runtime config reference requires config_path")
        config_path = require_engine_launch_config_path(self.config_path)
        if not config_path.is_file():
            raise ValueError(
                f"config_path must point to an existing file: {config_path}"
            )
        path_selection = selection_from_config_path(config_path)
        if path_selection != self.launch_selection:
            raise ValueError("config_path and launch selection must match")
        return RuntimeConfigReference(
            config_path=config_path,
            selection=self.launch_selection,
        )


__all__ = [
    "ConfigurationFingerprintMismatch",
    "ConfigLaunchIdentity",
    "EXPECTED_CONFIG_FINGERPRINT_ENV",
    "RuntimeConfigReferenceOwner",
    "assert_expected_config_fingerprint",
    "effective_config_fingerprint",
]
