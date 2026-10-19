"""Opt-in real-model acceptance for the isolated Pirozhki instance.

This module is skipped unless ``AGENT_MANAGER_REAL_MODEL_E2E`` is truthy. It
then composes the production ``pi_runner`` and the real default system
profiles against a disposable instance and drives one secretary conversation
turn with whatever provider/model the host Pi runtime resolves through the
normal mechanisms.

No credential is read, echoed or persisted by this test, and no production
Telegram, API or data path is touched. When the environment cannot provide a
working model/runtime the test fails with the actual runner diagnostic rather
than reporting a synthetic success.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys

import pytest

from .pirozhki_harness import REPO_ROOT, pirozhki_instance

_TRUTHY = {"1", "true", "yes", "on"}


def _real_model_enabled() -> bool:
    return os.environ.get("AGENT_MANAGER_REAL_MODEL_E2E", "").strip().lower() in _TRUTHY


pytestmark = pytest.mark.skipif(
    not _real_model_enabled(),
    reason=(
        "opt-in real-model acceptance: set AGENT_MANAGER_REAL_MODEL_E2E=1 "
        "to run against the host Pi runtime"
    ),
)


def test_real_model_secretary_conversation_turn(
    migrated_db: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap is required for a real Pi execution")
    src = str(REPO_ROOT / "src")
    existing = os.environ.get("PYTHONPATH", "")
    if src not in existing.split(os.pathsep):
        os.environ["PYTHONPATH"] = (
            src + (os.pathsep + existing if existing else "")
        )

    with pirozhki_instance(
        migrated_db,
        tmp_path / "real-model",
        runner_argv_prefix=(sys.executable, "-m", "pi_runner"),
        seed_deterministic_configs=False,
    ) as instance:
        conversation = instance.post(
            "/conversations", json={"title": "Real model"}
        ).json()
        message = instance.post(
            f"/conversations/{conversation['id']}/messages",
            json={"text": "Ответь одним коротким предложением: система готова."},
        ).json()
        turn = instance.post(
            f"/conversations/{conversation['id']}/turns",
            json={"input_message_id": message["id"]},
        ).json()
        outcome = instance.dispatcher.step()
        assert outcome is not None, "no turn was claimed"
        assert outcome.status == "completed", (
            "real-model turn failed; runner diagnostic follows:\n"
            f"{outcome.error}"
        )
        assert outcome.reply is not None
        assert outcome.reply.text.strip(), "real model returned an empty reply"
        stored_turn = instance.get(f"/turns/{turn['id']}").json()
        assert stored_turn["status"] == "completed"
