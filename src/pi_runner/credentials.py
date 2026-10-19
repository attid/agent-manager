"""Resolve existing Pi credentials without mounting or copying its agent HOME."""
import json
import shutil
import subprocess

from .core import RunnerError


def resolve_pi_credential(provider):
    cli = shutil.which('pi')
    if not cli:
        raise RunnerError('Pi CLI is required to resolve the configured Pi credential')
    result = subprocess.run(
        [cli, 'auth', 'check', '--provider', provider, '--json', '--credentials', '--no-refresh'],
        capture_output=True, text=True, timeout=30,
    )
    try:
        data = json.loads(result.stdout)
        key = data.get('credentials')
        if result.returncode == 0 and data.get('status') == 'ready' and isinstance(key, str) and key:
            return key
    except (ValueError, AttributeError):
        pass
    # Never include stdout/stderr: a credential resolver may put secrets there.
    raise RunnerError(f'Pi could not resolve the existing credential for {provider} using read-only auth check')
