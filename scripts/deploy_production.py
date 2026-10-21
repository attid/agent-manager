#!/usr/bin/env python3
"""Deploy Kuzya to an isolated production environment.

Ensures strict separation between development repository and system runtime:
- Prod Application: ~/.kuzya/app
- Prod Virtualenv:  ~/.kuzya/venv
- Prod Config:      ~/.kuzya/kuzya.env
- Systemd Service:  ~/.kuzya/kuzya.service (wired via `systemctl --user link`)
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("deploy")

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROD_APP = Path.home() / ".kuzya/app"
DEFAULT_PROD_VENV = Path.home() / ".kuzya/venv"
DEFAULT_CONFIG_DIR = Path.home() / ".kuzya"
DEFAULT_PROD_ROOT = Path.home() / ".kuzya"
DEFAULT_DATA_DIR = Path.home() / ".kuzya/data"


def copy_app_files(source_root: Path, target_root: Path) -> None:
    """Copy only clean runtime files to target production directory."""
    logger.info("Copying application files from %s to %s", source_root, target_root)
    target_root.mkdir(parents=True, exist_ok=True)

    def ignore_patterns(path: str, names: list[str]) -> set[str]:
        ignored = set()
        for name in names:
            if name == "__pycache__" or name.endswith(".pyc") or name == ".pytest_cache":
                ignored.add(name)
        return ignored

    # 1. src/
    target_src = target_root / "src"
    if target_src.exists():
        shutil.rmtree(target_src)
    shutil.copytree(source_root / "src", target_src, ignore=ignore_patterns)

    # 2. migrations/
    target_migrations = target_root / "migrations"
    if target_migrations.exists():
        shutil.rmtree(target_migrations)
    shutil.copytree(source_root / "migrations", target_migrations, ignore=ignore_patterns)

    # 3. Individual root files
    root_files = ["alembic.ini", "pyproject.toml", "README.md"]
    for filename in root_files:
        src_file = source_root / filename
        if src_file.exists():
            shutil.copy2(src_file, target_root / filename)

    logger.info("Application files successfully copied to %s", target_root)


def setup_prod_venv(prod_venv: Path, prod_app: Path) -> None:
    """Create isolated production virtual environment and install dependencies."""
    logger.info("Setting up isolated production virtualenv at %s", prod_venv)
    prod_venv.parent.mkdir(parents=True, exist_ok=True)

    python_bin = prod_venv / "bin/python"
    if not python_bin.exists():
        logger.info("Creating clean venv with uv (Python 3.14)...")
        subprocess.run(
            ["uv", "venv", str(prod_venv), "--python", "3.14"],
            check=True,
        )

    logger.info("Installing production dependencies into %s...", prod_venv)
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python_bin), str(prod_app)],
        check=True,
    )
    logger.info("Production dependencies installed successfully.")


def setup_config_file(config_dir: Path) -> Path:
    """Ensure ~/.kuzya/kuzya.env exists with safe permissions."""
    config_dir.mkdir(parents=True, exist_ok=True)
    env_file = config_dir / "kuzya.env"

    if env_file.exists():
        logger.info("Configuration file %s already exists, keeping existing secrets.", env_file)
        return env_file

    logger.info("Creating default configuration file at %s...", env_file)
    default_api_token = f"am_{secrets.token_urlsafe(24)}"
    env_content = f"""# Kuzya System Configuration
# Generated automatically during deployment

# HTTP API and Multi-Bearer Auth
KUZYA_HOST=127.0.0.1
KUZYA_PORT=18767
# List of comma-separated allowed Bearer tokens for Tailscale / internal access
KUZYA_API_TOKENS={default_api_token}

# SQLite Database (Transactional storage)
KUZYA_DB_URL=sqlite:////home/itolstov/.kuzya/data/tasks.db
KUZYA_AUTO_MIGRATE=true

# Filesystem Roots
KUZYA_ARTIFACT_ROOT=/home/itolstov/.kuzya/data/artifacts
KUZYA_KNOWLEDGE_ROOT=/home/itolstov/.kuzya/data/knowledge
KUZYA_DEFAULT_WORKSPACE=/home/itolstov/Projects/mtl/kuzya
KUZYA_RUNNER_PREFIX=/home/itolstov/.kuzya/venv/bin/python -m pi_runner

# Telegram Secretary Bot (Заполните ваши данные для работы бота)
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_ID=
KUZYA_BACKEND_URL=http://127.0.0.1:18767
TELEGRAM_STATE_DB_PATH=/home/itolstov/.kuzya/data/telegram_state.db
TELEGRAM_MEDIA_DIR=/home/itolstov/.kuzya/data/media

# Audio Transcription (Deepgram)
DEEPGRAM_API_KEY=

# Logging
LOG_LEVEL=INFO
LOG_FORMAT=text

# Agent wall-clock context (IANA zone; default: system timezone)
# KUZYA_TIMEZONE=America/Sao_Paulo
"""
    env_file.write_text(env_content, encoding="utf-8")
    env_file.chmod(0o600)
    logger.info("Created %s with permissions 0600. Generated initial API token: %s", env_file, default_api_token)
    return env_file


def setup_systemd_service(prod_root: Path, prod_app: Path, prod_venv: Path, env_file: Path) -> Path:
    """Write the systemd user unit inside ~/.kuzya and link it.

    The unit file lives with the rest of the installation (one directory,
    ~/.kuzya); `systemctl --user link` registers it, leaving only a symlink
    under ~/.config/systemd/user — the minimum systemd physically requires.
    """
    prod_root.mkdir(parents=True, exist_ok=True)
    service_file = prod_root / "kuzya.service"

    unit_content = f"""[Unit]
Description=Kuzya Service (FastAPI + Dispatcher + TaskWorker + Telegram Bot)
After=network.target

[Service]
Type=simple
WorkingDirectory={prod_app}
EnvironmentFile={env_file}
Environment=PYTHONPATH={prod_app}/src
Environment="PATH=/home/itolstov/.nvm/versions/node/v24.19.0/bin:/home/itolstov/.local/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart={prod_venv}/bin/python -m kuzya.runner
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""
    service_file.write_text(unit_content, encoding="utf-8")
    logger.info("Systemd unit written to %s", service_file)

    try:
        subprocess.run(
            ["systemctl", "--user", "link", "--force", str(service_file)],
            check=True,
        )
        logger.info("Linked %s into the systemd user manager", service_file)
    except Exception as e:
        logger.warning("Could not link the unit (run it manually): %s", e)
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        logger.info("Executed systemctl --user daemon-reload")
    except Exception as e:
        logger.warning("Could not run daemon-reload: %s", e)

    return service_file


def verify_prod_installation(prod_venv: Path, prod_app: Path) -> None:
    """Verify that the production environment can successfully load all modules."""
    logger.info("Verifying production environment integrity...")
    python_bin = prod_venv / "bin/python"

    cmd = [
        str(python_bin),
        "-c",
        "import kuzya.runner, aiogram, fastapi, sqlalchemy, alembic, httpx, multipart; print('INTEGRITY_OK')",
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(prod_app / "src")

    res = subprocess.run(cmd, env=env, capture_output=True, text=True, check=True)
    if "INTEGRITY_OK" in res.stdout:
        logger.info("Verification succeeded: production venv and imports are fully operational.")
    else:
        raise RuntimeError(f"Verification output unexpected: {res.stdout}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy Kuzya to isolated production")
    parser.add_argument("--source", type=Path, default=REPO_ROOT, help="Source repository root")
    parser.add_argument("--prod-app", type=Path, default=DEFAULT_PROD_APP, help="Target production app directory")
    parser.add_argument("--prod-venv", type=Path, default=DEFAULT_PROD_VENV, help="Target production virtualenv")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR, help="Target config directory")
    parser.add_argument("--prod-root", type=Path, default=DEFAULT_PROD_ROOT, help="Target single installation root (~/.kuzya)")
    args = parser.parse_args()

    # Create data directories
    DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DEFAULT_DATA_DIR / "artifacts").mkdir(parents=True, exist_ok=True)
    (DEFAULT_DATA_DIR / "knowledge").mkdir(parents=True, exist_ok=True)
    (DEFAULT_DATA_DIR / "skills").mkdir(parents=True, exist_ok=True)
    (DEFAULT_DATA_DIR / "media").mkdir(parents=True, exist_ok=True)

    copy_app_files(args.source, args.prod_app)
    setup_prod_venv(args.prod_venv, args.prod_app)
    env_file = setup_config_file(args.config_dir)
    setup_systemd_service(args.prod_root, args.prod_app, args.prod_venv, env_file)
    verify_prod_installation(args.prod_venv, args.prod_app)

    logger.info("=== Deployment completed successfully! ===")
    logger.info("To control the service:")
    logger.info("  systemctl --user start kuzya.service")
    logger.info("  systemctl --user status kuzya.service")
    logger.info("  journalctl --user -u kuzya.service -f")


if __name__ == "__main__":
    main()
