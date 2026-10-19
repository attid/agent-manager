"""Canonical host-side resolver for model/provider runtime defaults.

A secretary execution needs a provider and a model, but that selection must
not live in the durable agent registry: the same logical agent may be run on
whatever model the host currently offers. This module resolves the runtime
defaults for one execution from three external sources, in precedence order:

1. explicit ``provider``/``model`` (and optional ``pi_auth_provider`` /
   ``models_file``) values in the supplied trusted role configuration;
2. an optional caller-selected TOML file with a ``[default]`` table;
3. the host global Pi settings
   (``~/.pi/agent/settings.json`` ``defaultProvider``/``defaultModel``).

The result is an *inspectable* snapshot: a copied role configuration with the
resolved fields, the actual selected values, and the source of every field.
The supplied role configuration (and therefore the stored registry policy) is
never modified. The resolver performs no model, network, database or
credential access.

Resolution is deliberately late
-------------------------------

The resolver object holds only the configured paths. Every :meth:`resolve`
call re-reads the selected sources, so editing a default file takes effect for
the next execution without rebuilding the resolver, restarting the process or
touching the database. A malformed selected source is a typed
:class:`RuntimeDefaultsConfigurationError`, not a silent fallback.

Precedence and partial overrides
--------------------------------

Fields are resolved independently. The first source that defines a field wins,
so a role configuration may override ``provider`` while the remaining fields
are inherited. A role configuration that already defines both ``provider`` and
``model`` is a *complete* override: the default files are not read at all, so
a complete fixed profile keeps working even when those files are absent or
malformed.

The authentication reference ``pi_auth_provider`` is an opaque reference that
the existing Runner resolves; this module never reads, copies or validates
credentials. An explicitly configured ``pi_auth_provider`` (in the role
configuration or the selected app TOML) is used as supplied. Otherwise it
follows the resolved ``provider``, so an inherited default tracks the provider
that was actually selected. If a ``model`` is selected but neither a
``provider`` nor an explicit ``pi_auth_provider`` can be determined, the
resolver raises a typed error instead of guessing: the data needed to
authenticate the selected provider is missing.

Only the four managed fields are ever read from a selected file. Unrelated Pi
settings are never exported or echoed, and unknown provider/model identifier
strings are preserved verbatim.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import pathlib
import tomllib
from collections.abc import Mapping
from typing import Any

__all__ = [
    "DEFAULT_GLOBAL_SETTINGS_PATH",
    "FieldSelection",
    "ResolvedRuntimeDefaults",
    "RuntimeDefaultsConfigurationError",
    "RuntimeDefaultsError",
    "RuntimeDefaultsResolver",
    "resolve_runtime_defaults",
]

#: Host global Pi settings location. Interpreted relative to the *host* home.
DEFAULT_GLOBAL_SETTINGS_PATH = "~/.pi/agent/settings.json"

#: Fields owned by the resolver. Nothing else is read from a selected file.
PROVIDER = "provider"
MODEL = "model"
PI_AUTH_PROVIDER = "pi_auth_provider"
MODELS_FILE = "models_file"
_MANAGED_FIELDS = (PROVIDER, MODEL, PI_AUTH_PROVIDER, MODELS_FILE)

#: Selection sources recorded in the inspectable evidence.
SOURCE_ROLE_CONFIG = "role_config"
SOURCE_APP_FILE = "app_file"
SOURCE_GLOBAL_PI = "global_pi"
SOURCE_DERIVED_PROVIDER = "derived_from_provider"
SOURCE_ABSENT = "absent"


class RuntimeDefaultsError(Exception):
    """Base class for runtime-default resolution failures."""


class RuntimeDefaultsConfigurationError(RuntimeDefaultsError, ValueError):
    """A selected source is malformed or lacks data needed for a selection.

    Subclasses :class:`ValueError` so existing callers that validate trusted
    configuration keep working. It never contains credentials.
    """


@dataclasses.dataclass(frozen=True)
class FieldSelection:
    """Where one resolved field came from and the value that was selected."""

    field: str
    source: str
    value: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "value": self.value}


@dataclasses.dataclass(frozen=True)
class ResolvedRuntimeDefaults:
    """One late-bound resolution result for a single execution.

    Attributes:
        role_config: Copied role configuration with the resolved fields set;
            the caller's mapping is never mutated.
        provider: Actual selected provider, or ``None``.
        model: Actual selected model, or ``None``.
        pi_auth_provider: Auth reference following the selected provider, or
            the explicitly configured alias.
        models_file: Custom-provider definition file reference, or ``None``.
        selection: Per-field :class:`FieldSelection` evidence.
        app_config_path: The selected TOML path, or ``None``.
        global_settings_path: The host settings path that was consulted.
    """

    role_config: dict[str, Any]
    provider: str | None
    model: str | None
    pi_auth_provider: str | None
    models_file: str | None
    selection: Mapping[str, FieldSelection]
    app_config_path: str | None
    global_settings_path: str | None

    @property
    def values(self) -> dict[str, str | None]:
        """The actual selected values, JSON-ready."""
        return {
            PROVIDER: self.provider,
            MODEL: self.model,
            PI_AUTH_PROVIDER: self.pi_auth_provider,
            MODELS_FILE: self.models_file,
        }

    def selection_dict(self) -> dict[str, dict[str, Any]]:
        """Per-field source/value evidence, JSON-ready."""
        return {
            field: self.selection[field].to_dict()
            for field in _MANAGED_FIELDS
            if field in self.selection
        }

    def to_provenance(self) -> dict[str, Any]:
        """Return the source/value/final-config evidence for export.

        The returned mapping is JSON-serializable and contains only managed
        runtime-default data; unrelated settings are never included.
        """
        return {
            "values": self.values,
            "selection": self.selection_dict(),
            "app_config_path": self.app_config_path,
            "global_settings_path": self.global_settings_path,
            "role_config": copy.deepcopy(self.role_config),
        }


class RuntimeDefaultsResolver:
    """Late-binding resolver bound to an optional app TOML and a settings path.

    Constructing the resolver performs no I/O. Paths are injectable for tests
    and explicit host use; when no global path is supplied the host global Pi
    settings location is resolved from the host home on each call. The
    resolver holds no resolved values, so editing a source affects the next
    :meth:`resolve` call on the same object.
    """

    def __init__(
        self,
        *,
        app_config_path: str | os.PathLike[str] | None = None,
        global_settings_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._app_config_path = (
            pathlib.Path(app_config_path)
            if app_config_path is not None
            else None
        )
        self._global_settings_path = (
            pathlib.Path(global_settings_path)
            if global_settings_path is not None
            else None
        )

    @property
    def app_config_path(self) -> pathlib.Path | None:
        """The selected app TOML path, if any."""
        return self._app_config_path

    @property
    def global_settings_path(self) -> pathlib.Path:
        """The host settings path, expanding the host home when not injected."""
        if self._global_settings_path is not None:
            return self._global_settings_path
        return pathlib.Path(DEFAULT_GLOBAL_SETTINGS_PATH).expanduser()

    def resolve(
        self, role_config: Mapping[str, Any]
    ) -> ResolvedRuntimeDefaults:
        """Resolve the runtime defaults for ``role_config`` as of right now.

        Args:
            role_config: Trusted role configuration. It is copied, never
                mutated. Its ``provider``/``model``/``pi_auth_provider``/
                ``models_file`` entries are the highest-precedence source.

        Returns:
            An inspectable :class:`ResolvedRuntimeDefaults` with the copied
            resolved role configuration, the actual values and the selection
            evidence.

        Raises:
            RuntimeDefaultsConfigurationError: when a selected source is
                malformed or a selected model lacks the data needed to follow
                its provider authentication.
        """
        if not isinstance(role_config, Mapping):
            raise RuntimeDefaultsConfigurationError(
                "role_config must be a mapping"
            )
        base = copy.deepcopy(dict(role_config))
        role_values = _read_role_fields(base)

        complete_override = (
            role_values[PROVIDER] is not None
            and role_values[MODEL] is not None
        )

        app_values: dict[str, str | None] = {}
        global_values: dict[str, str | None] = {}
        if not complete_override:
            app_values = self._read_app_fields()
            global_values = self._read_global_fields()

        provider, provider_source = _select(
            (
                (SOURCE_ROLE_CONFIG, role_values[PROVIDER]),
                (SOURCE_APP_FILE, app_values.get(PROVIDER)),
                (SOURCE_GLOBAL_PI, global_values.get(PROVIDER)),
            )
        )
        model, model_source = _select(
            (
                (SOURCE_ROLE_CONFIG, role_values[MODEL]),
                (SOURCE_APP_FILE, app_values.get(MODEL)),
                (SOURCE_GLOBAL_PI, global_values.get(MODEL)),
            )
        )
        models_file, models_file_source = _select(
            (
                (SOURCE_ROLE_CONFIG, role_values[MODELS_FILE]),
                (SOURCE_APP_FILE, app_values.get(MODELS_FILE)),
            )
        )

        explicit_auth, explicit_auth_source = _select(
            (
                (SOURCE_ROLE_CONFIG, role_values[PI_AUTH_PROVIDER]),
                (SOURCE_APP_FILE, app_values.get(PI_AUTH_PROVIDER)),
            )
        )

        if explicit_auth is not None:
            pi_auth_provider = explicit_auth
            pi_auth_source = explicit_auth_source
        elif provider is not None:
            # Inherited defaults follow the provider that was selected.
            pi_auth_provider = provider
            pi_auth_source = SOURCE_DERIVED_PROVIDER
        else:
            pi_auth_provider = None
            pi_auth_source = SOURCE_ABSENT

        if model is not None and pi_auth_provider is None:
            raise RuntimeDefaultsConfigurationError(
                "a model was selected without a provider or an explicit "
                "pi_auth_provider; data needed to follow the selected "
                "provider authentication is missing"
            )

        selection = {
            PROVIDER: FieldSelection(PROVIDER, provider_source, provider),
            MODEL: FieldSelection(MODEL, model_source, model),
            PI_AUTH_PROVIDER: FieldSelection(
                PI_AUTH_PROVIDER, pi_auth_source, pi_auth_provider
            ),
            MODELS_FILE: FieldSelection(
                MODELS_FILE, models_file_source, models_file
            ),
        }

        resolved = copy.deepcopy(base)
        for field, value in (
            (PROVIDER, provider),
            (MODEL, model),
            (PI_AUTH_PROVIDER, pi_auth_provider),
            (MODELS_FILE, models_file),
        ):
            if value is not None:
                resolved[field] = value

        return ResolvedRuntimeDefaults(
            role_config=resolved,
            provider=provider,
            model=model,
            pi_auth_provider=pi_auth_provider,
            models_file=models_file,
            selection=selection,
            app_config_path=(
                str(self._app_config_path)
                if self._app_config_path is not None
                else None
            ),
            global_settings_path=str(self.global_settings_path),
        )

    def _read_app_fields(self) -> dict[str, str | None]:
        """Read only the ``[default]`` managed fields from the selected TOML."""
        path = self._app_config_path
        if path is None:
            return {}
        resolved = path.expanduser()
        if not resolved.exists():
            return {}
        try:
            with open(resolved, "rb") as handle:
                document = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise RuntimeDefaultsConfigurationError(
                f"app config {resolved} is not valid TOML "
                f"({type(error).__name__})"
            ) from error
        if not isinstance(document, dict):
            raise RuntimeDefaultsConfigurationError(
                f"app config {resolved} must be a TOML table"
            )
        section = document.get("default", {})
        if section is None:
            section = {}
        if not isinstance(section, dict):
            raise RuntimeDefaultsConfigurationError(
                f"app config {resolved} [default] must be a table"
            )
        return {
            field: _optional_string(section, field, f"app config {resolved}")
            for field in _MANAGED_FIELDS
        }

    def _read_global_fields(self) -> dict[str, str | None]:
        """Read only ``defaultProvider``/``defaultModel`` from host settings."""
        path = self.global_settings_path
        if not path.exists():
            return {}
        try:
            with open(path, encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeDefaultsConfigurationError(
                f"global Pi settings {path} are not valid JSON "
                f"({type(error).__name__})"
            ) from error
        if not isinstance(document, dict):
            raise RuntimeDefaultsConfigurationError(
                f"global Pi settings {path} must be a JSON object"
            )
        return {
            PROVIDER: _optional_string(
                document, "defaultProvider", f"global Pi settings {path}"
            ),
            MODEL: _optional_string(
                document, "defaultModel", f"global Pi settings {path}"
            ),
        }


def resolve_runtime_defaults(
    role_config: Mapping[str, Any],
    *,
    app_config_path: str | os.PathLike[str] | None = None,
    global_settings_path: str | os.PathLike[str] | None = None,
) -> ResolvedRuntimeDefaults:
    """Resolve runtime defaults once without retaining a resolver object."""
    return RuntimeDefaultsResolver(
        app_config_path=app_config_path,
        global_settings_path=global_settings_path,
    ).resolve(role_config)


def _read_role_fields(base: Mapping[str, Any]) -> dict[str, str | None]:
    """Read and validate the managed fields of the trusted role config."""
    return {
        field: _optional_string(base, field, "role_config")
        for field in _MANAGED_FIELDS
    }


def _optional_string(
    mapping: Mapping[str, Any], field: str, source: str
) -> str | None:
    """Return a managed field as a string, or ``None`` when absent.

    Any interfered shape is a typed configuration error; arbitrary non-empty
    identifier strings are preserved without an allowlist or format check.
    """
    if field not in mapping:
        return None
    value = mapping[field]
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeDefaultsConfigurationError(
            f"{source} field {field!r} must be a string"
        )
    return value


def _select(
    candidates: tuple[tuple[str, str | None], ...],
) -> tuple[str | None, str]:
    """Return the first defined value and its source, else absent."""
    for source, value in candidates:
        if value is not None:
            return value, source
    return None, SOURCE_ABSENT
