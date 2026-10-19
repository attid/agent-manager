"""Acceptance tests for the standalone runtime-defaults resolver.

Every source is a temporary JSON or TOML file under ``tmp_path``. No real host
settings, credential, database, network or model call is involved. The frozen
integration tests use the real migrated file database and the existing
deterministic ``fake_pi_runner.py`` child.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

from agent_manager.profile_runtime import freeze_profile_handler
from agent_manager.runtime_defaults import (
    RuntimeDefaultsConfigurationError,
    RuntimeDefaultsResolver,
    resolve_runtime_defaults,
)
from agent_manager.secretary import TurnInput
from agent_manager.storage import AgentProfileRepository, Database

FAKE_RUNNER = pathlib.Path(__file__).with_name("fake_pi_runner.py")


def _write_json(path: pathlib.Path, payload: object) -> pathlib.Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_toml(path: pathlib.Path, text: str) -> pathlib.Path:
    path.write_text(text, encoding="utf-8")
    return path


def _global_settings(
    tmp_path: pathlib.Path,
    *,
    provider: str | None = "global-provider",
    model: str | None = "global-model",
    **extra: object,
) -> pathlib.Path:
    payload: dict[str, object] = dict(extra)
    if provider is not None:
        payload["defaultProvider"] = provider
    if model is not None:
        payload["defaultModel"] = model
    return _write_json(tmp_path / "pi-settings.json", payload)


# -- source precedence ---------------------------------------------------


def test_global_pi_inheritance_and_auth_follows_provider(
    tmp_path: pathlib.Path,
) -> None:
    settings = _global_settings(tmp_path)
    resolver = RuntimeDefaultsResolver(global_settings_path=settings)

    resolved = resolver.resolve({"mode": "success"})

    assert resolved.provider == "global-provider"
    assert resolved.model == "global-model"
    # No explicit alias: the auth reference follows the selected provider.
    assert resolved.pi_auth_provider == "global-provider"
    assert resolved.role_config["provider"] == "global-provider"
    assert resolved.role_config["model"] == "global-model"
    assert resolved.role_config["pi_auth_provider"] == "global-provider"
    assert resolved.role_config["mode"] == "success"
    assert resolved.selection["provider"].source == "global_pi"
    assert resolved.selection["model"].source == "global_pi"
    assert resolved.selection["pi_auth_provider"].source == (
        "derived_from_provider"
    )
    assert resolved.app_config_path is None


def test_app_file_overrides_global_and_supplies_models_file_and_auth_alias(
    tmp_path: pathlib.Path,
) -> None:
    settings = _global_settings(tmp_path)
    app = _write_toml(
        tmp_path / "app.toml",
        "\n".join(
            [
                "[default]",
                'provider = "app-provider"',
                'model = "app-model"',
                'pi_auth_provider = "app-auth-alias"',
                'models_file = "/trusted/custom-models.json"',
                "",
            ]
        ),
    )
    resolver = RuntimeDefaultsResolver(
        app_config_path=app, global_settings_path=settings
    )

    resolved = resolver.resolve({})

    assert resolved.provider == "app-provider"
    assert resolved.model == "app-model"
    assert resolved.pi_auth_provider == "app-auth-alias"
    assert resolved.models_file == "/trusted/custom-models.json"
    assert resolved.role_config["models_file"] == "/trusted/custom-models.json"
    assert resolved.selection["provider"].source == "app_file"
    assert resolved.selection["pi_auth_provider"].source == "app_file"
    assert resolved.selection["models_file"].source == "app_file"
    assert resolved.app_config_path == str(app)


def test_global_pi_source_is_read_when_no_app_file_is_configured(
    tmp_path: pathlib.Path,
) -> None:
    settings = _global_settings(tmp_path, provider="only-global", model="m")
    resolver = RuntimeDefaultsResolver(global_settings_path=settings)

    resolved = resolver.resolve({"mode": "x"})

    assert resolved.selection["provider"].source == "global_pi"
    assert resolved.role_config["provider"] == "only-global"


def test_partial_role_provider_inherits_model_and_derives_auth(
    tmp_path: pathlib.Path,
) -> None:
    app = _write_toml(
        tmp_path / "app.toml",
        '[default]\nmodel = "inherited-model"\n',
    )
    resolver = RuntimeDefaultsResolver(
        app_config_path=app,
        global_settings_path=tmp_path / "absent-settings.json",
    )

    resolved = resolver.resolve({"provider": "role-provider"})

    assert resolved.provider == "role-provider"
    assert resolved.model == "inherited-model"
    # Auth follows the selected (role) provider, not the model's source.
    assert resolved.pi_auth_provider == "role-provider"
    assert resolved.role_config["provider"] == "role-provider"
    assert resolved.role_config["model"] == "inherited-model"
    assert resolved.selection["provider"].source == "role_config"
    assert resolved.selection["model"].source == "app_file"


def test_complete_role_override_skips_broken_defaults(
    tmp_path: pathlib.Path,
) -> None:
    broken_app = _write_toml(tmp_path / "app.toml", "[default\nnot toml")
    broken_settings = tmp_path / "pi-settings.json"
    broken_settings.write_text("this is not json", encoding="utf-8")
    resolver = RuntimeDefaultsResolver(
        app_config_path=broken_app, global_settings_path=broken_settings
    )

    resolved = resolver.resolve(
        {"provider": "fixed-provider", "model": "fixed-model"}
    )

    assert resolved.provider == "fixed-provider"
    assert resolved.model == "fixed-model"
    assert resolved.pi_auth_provider == "fixed-provider"
    assert resolved.selection["provider"].source == "role_config"
    assert resolved.selection["model"].source == "role_config"


def test_original_role_config_is_not_modified(tmp_path: pathlib.Path) -> None:
    settings = _global_settings(tmp_path)
    resolver = RuntimeDefaultsResolver(global_settings_path=settings)
    original = {"mode": "success", "nested": {"keep": [1, 2]}}

    resolved = resolver.resolve(original)

    assert original == {"mode": "success", "nested": {"keep": [1, 2]}}
    assert resolved.role_config["mode"] == "success"
    assert resolved.role_config["provider"] == "global-provider"
    assert resolved.role_config["nested"] == {"keep": [1, 2]}
    # The resolved copy is independent of the caller's mapping.
    assert resolved.role_config is not original
    assert resolved.role_config["nested"] is not original["nested"]


# -- late binding --------------------------------------------------------


def test_same_resolver_reloads_manual_edits(tmp_path: pathlib.Path) -> None:
    settings = _global_settings(tmp_path, provider="p1", model="m1")
    resolver = RuntimeDefaultsResolver(global_settings_path=settings)

    first = resolver.resolve({})
    assert (first.provider, first.model) == ("p1", "m1")

    # Manual edit on disk; the same resolver object sees it.
    _write_json(
        settings, {"defaultProvider": "p2", "defaultModel": "m2"}
    )
    second = resolver.resolve({})
    assert (second.provider, second.model) == ("p2", "m2")
    assert second.pi_auth_provider == "p2"


def test_inherited_auth_tracks_provider_changes(tmp_path: pathlib.Path) -> None:
    settings = _global_settings(tmp_path, provider="alpha", model="x")
    resolver = RuntimeDefaultsResolver(global_settings_path=settings)

    assert resolver.resolve({}).pi_auth_provider == "alpha"

    _write_json(settings, {"defaultProvider": "beta", "defaultModel": "y"})
    resolved = resolver.resolve({})
    assert resolved.pi_auth_provider == "beta"
    assert resolved.selection["pi_auth_provider"].source == (
        "derived_from_provider"
    )


def test_invalid_app_config_is_a_typed_error_then_corrected(
    tmp_path: pathlib.Path,
) -> None:
    app = _write_toml(tmp_path / "app.toml", "[default\nbroken")
    resolver = RuntimeDefaultsResolver(
        app_config_path=app,
        global_settings_path=tmp_path / "absent.json",
    )

    with pytest.raises(RuntimeDefaultsConfigurationError):
        resolver.resolve({})

    _write_toml(
        app, '[default]\nprovider = "fixed"\nmodel = "fixed-model"\n'
    )
    resolved = resolver.resolve({})
    assert resolved.provider == "fixed"
    assert resolved.model == "fixed-model"


def test_invalid_global_settings_is_a_typed_error_then_corrected(
    tmp_path: pathlib.Path,
) -> None:
    settings = tmp_path / "pi-settings.json"
    settings.write_text("{not json", encoding="utf-8")
    resolver = RuntimeDefaultsResolver(global_settings_path=settings)

    with pytest.raises(RuntimeDefaultsConfigurationError):
        resolver.resolve({})

    _write_json(
        settings, {"defaultProvider": "ok", "defaultModel": "ok-model"}
    )
    assert resolver.resolve({}).provider == "ok"


def test_non_string_managed_field_is_a_typed_error(
    tmp_path: pathlib.Path,
) -> None:
    app = _write_toml(
        tmp_path / "app.toml", "[default]\nmodel = 123\n"
    )
    resolver = RuntimeDefaultsResolver(
        app_config_path=app,
        global_settings_path=tmp_path / "absent.json",
    )
    with pytest.raises(RuntimeDefaultsConfigurationError):
        resolver.resolve({})


def test_model_without_provider_or_auth_is_a_typed_error(
    tmp_path: pathlib.Path,
) -> None:
    resolver = RuntimeDefaultsResolver(
        global_settings_path=tmp_path / "absent.json"
    )
    with pytest.raises(RuntimeDefaultsConfigurationError):
        resolver.resolve({"model": "orphan-model"})


def test_explicit_auth_alias_without_provider_is_accepted(
    tmp_path: pathlib.Path,
) -> None:
    resolver = RuntimeDefaultsResolver(
        global_settings_path=tmp_path / "absent.json"
    )
    resolved = resolver.resolve(
        {"model": "m", "pi_auth_provider": "credential-alias"}
    )
    assert resolved.provider is None
    assert resolved.pi_auth_provider == "credential-alias"
    assert resolved.selection["pi_auth_provider"].source == "role_config"


def test_unknown_provider_and_model_strings_are_preserved(
    tmp_path: pathlib.Path,
) -> None:
    resolver = RuntimeDefaultsResolver(
        global_settings_path=tmp_path / "absent.json"
    )
    resolved = resolver.resolve(
        {
            "provider": "custom:vendor/v2",
            "model": "weird model  spaces + punctuation",
        }
    )
    assert resolved.provider == "custom:vendor/v2"
    assert resolved.model == "weird model  spaces + punctuation"
    assert resolved.pi_auth_provider == "custom:vendor/v2"


def test_unrelated_global_settings_are_not_exported(
    tmp_path: pathlib.Path,
) -> None:
    settings = _global_settings(
        tmp_path, theme="dark", secret="do-not-leak"
    )
    resolver = RuntimeDefaultsResolver(global_settings_path=settings)

    resolved = resolver.resolve({})

    assert "theme" not in resolved.role_config
    assert "secret" not in resolved.role_config
    serialized = json.dumps(resolved.to_provenance())
    assert "dark" not in serialized
    assert "do-not-leak" not in serialized


def test_resolve_runtime_defaults_convenience(tmp_path: pathlib.Path) -> None:
    settings = _global_settings(tmp_path, provider="conv", model="conv-m")
    resolved = resolve_runtime_defaults(
        {"provider": "explicit"},
        global_settings_path=settings,
    )
    assert resolved.provider == "explicit"
    assert resolved.model == "conv-m"


# -- frozen-profile integration -----------------------------------------


def _profile(
    repository: AgentProfileRepository, *, runner_config: dict[str, object]
):
    return repository.create(
        agent_id="triage",
        name="Triage",
        role="worker",
        system_prompt="prompt",
        runner_config=runner_config,
        skills=[],
        knowledge={},
    )


def _turn_input() -> TurnInput:
    return TurnInput(
        turn_id="turn-1",
        conversation_id="conv-1",
        input_message_id="message-1",
        role="user",
        text="ping",
        metadata={},
        attempt_count=1,
    )


def test_freeze_with_runtime_defaults_exports_resolved_evidence(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    settings = _global_settings(
        tmp_path, provider="inherited-provider", model="inherited-model"
    )
    resolver = RuntimeDefaultsResolver(global_settings_path=settings)
    original = {"mode": "success", "summary_text": "resolved answer"}

    database = Database.from_path(migrated_db)
    try:
        repository = AgentProfileRepository(database)
        _profile(repository, runner_config=original)
        frozen = freeze_profile_handler(
            database,
            "triage",
            runner_argv_prefix=[sys.executable, str(FAKE_RUNNER)],
            artifact_root=tmp_path / "exports",
            runtime_defaults=resolver,
        )

        role = json.loads(frozen.role_config_path.read_text(encoding="utf-8"))
        assert role["provider"] == "inherited-provider"
        assert role["model"] == "inherited-model"
        assert role["pi_auth_provider"] == "inherited-provider"
        assert role["mode"] == "success"

        snapshot = json.loads(
            frozen.snapshot_path.read_text(encoding="utf-8")
        )
        # Original profile snapshot is preserved untouched.
        assert snapshot["runner_config"] == original
        # Actual selection provenance is recorded in the export.
        evidence = snapshot["runtime_defaults"]
        assert evidence["values"]["provider"] == "inherited-provider"
        assert evidence["values"]["model"] == "inherited-model"
        assert evidence["selection"]["provider"]["source"] == "global_pi"
        assert evidence["selection"]["model"]["source"] == "global_pi"
        assert evidence["role_config"]["provider"] == "inherited-provider"

        # Stored registry policy is unchanged.
        stored = repository.get_revision("triage", 1)
        assert stored is not None
        assert stored.runner_config == original

        assert frozen(_turn_input()) == "resolved answer"
        record = frozen.last_execution
        assert record is not None
        assert record.runtime_defaults is not None
        assert record.runtime_defaults["values"]["provider"] == (
            "inherited-provider"
        )
        on_disk = json.loads(
            record.provenance_path.read_text(encoding="utf-8")
        )
        assert on_disk["runtime_defaults"]["values"]["model"] == (
            "inherited-model"
        )
    finally:
        database.dispose()


def test_freeze_without_runtime_defaults_ignores_host_settings(
    tmp_path: pathlib.Path,
    migrated_db: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A host settings file that would change the selection if it were read.
    home = tmp_path / "home"
    (home / ".pi" / "agent").mkdir(parents=True)
    _write_json(
        home / ".pi" / "agent" / "settings.json",
        {"defaultProvider": "host-provider", "defaultModel": "host-model"},
    )
    monkeypatch.setenv("HOME", str(home))

    database = Database.from_path(migrated_db)
    try:
        repository = AgentProfileRepository(database)
        original = {"mode": "success", "summary_text": "legacy answer"}
        _profile(repository, runner_config=original)
        frozen = freeze_profile_handler(
            database,
            "triage",
            runner_argv_prefix=[sys.executable, str(FAKE_RUNNER)],
            artifact_root=tmp_path / "exports",
        )

        role = json.loads(frozen.role_config_path.read_text(encoding="utf-8"))
        assert "provider" not in role
        assert "model" not in role
        assert "pi_auth_provider" not in role
        snapshot = json.loads(
            frozen.snapshot_path.read_text(encoding="utf-8")
        )
        assert "runtime_defaults" not in snapshot
        assert repository.get_revision("triage", 1).runner_config == original
        assert frozen(_turn_input()) == "legacy answer"
        assert frozen.last_execution is not None
        assert frozen.last_execution.runtime_defaults is None
    finally:
        database.dispose()


def test_freeze_with_runtime_defaults_does_not_mutate_stored_policy(
    tmp_path: pathlib.Path, migrated_db: pathlib.Path
) -> None:
    app = _write_toml(
        tmp_path / "app.toml",
        '[default]\nprovider = "app-p"\nmodel = "app-m"\n',
    )
    resolver = RuntimeDefaultsResolver(
        app_config_path=app,
        global_settings_path=tmp_path / "absent.json",
    )
    database = Database.from_path(migrated_db)
    try:
        repository = AgentProfileRepository(database)
        original = {"mode": "success", "provider": "role-p"}
        _profile(repository, runner_config=original)
        frozen = freeze_profile_handler(
            database,
            "triage",
            runner_argv_prefix=[sys.executable, str(FAKE_RUNNER)],
            artifact_root=tmp_path / "exports",
            runtime_defaults=resolver,
        )
        role = json.loads(frozen.role_config_path.read_text(encoding="utf-8"))
        # Role provider wins; the model is inherited from the app file.
        assert role["provider"] == "role-p"
        assert role["model"] == "app-m"
        stored = repository.get_revision("triage", 1)
        assert stored is not None
        assert stored.runner_config == original
    finally:
        database.dispose()
