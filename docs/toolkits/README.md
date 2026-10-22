# Centralized toolkits for sandboxed agents

How to give sandboxed agents a shared, read-only tool installation (Playwright
today, anything similar tomorrow) **without touching platform code**. Everything
below rides on three existing mechanisms: `readonly_paths` / `runtime_paths` in
the agent profile, the skills directory, and the host filesystem.

Source of truth for the deployed skill lives next to this file
(`web-testing.SKILL.md`); the working copy is installed at
`~/.kuzya/data/skills/web-testing/SKILL.md`.

## The recipe (verified in production 2026-10-22)

### 1. Build the toolkit once on the host

```bash
mkdir -p ~/.kuzya/tools/playwright && cd ~/.kuzya/tools/playwright
npm init -y
npm install playwright@1.64.1 --no-audit --no-fund
npx playwright install chromium     # browsers land in ~/.cache/ms-playwright
```

The package version must match the browser build (playwright 1.64.1 ↔
chromium-1248). Keep a small `verify-sandbox.mjs` in the toolkit root
(launch → goto → screenshot) as a self-check and as the reference example.

### 2. Prove it inside a Bubblewrap sandbox before granting it

Reproduce the platform sandbox base (same binds `pi_runner/core.py` builds)
and run the verify script inside:

```bash
bwrap --die-with-parent --new-session --unshare-pid --unshare-ipc \
  --unshare-uts --unshare-cgroup-try --cap-drop ALL --clearenv \
  --ro-bind /usr /usr \
  --symlink usr/bin /bin --symlink usr/sbin /sbin --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --proc /proc --dev /dev --tmpfs /tmp --dir /etc --dir /run --dir /home \
  --ro-bind /etc/ld.so.cache /etc/ld.so.cache --ro-bind /etc/ssl/certs /etc/ssl/certs \
  --ro-bind /etc/resolv.conf /etc/resolv.conf --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf \
  --ro-bind /etc/hosts /etc/hosts \
  --ro-bind ~/.kuzya/tools/playwright ~/.kuzya/tools/playwright \
  --ro-bind ~/.cache/ms-playwright ~/.cache/ms-playwright \
  --ro-bind ~/.nvm/versions/node/v24.19.0 ~/.nvm/versions/node/v24.19.0 \
  --setenv PATH "$HOME/.nvm/versions/node/v24.19.0/bin:/usr/bin:/bin" \
  --setenv PLAYWRIGHT_BROWSERS_PATH "$HOME/.cache/ms-playwright" \
  --setenv HOME /tmp \
  ~/.nvm/versions/node/v24.19.0/bin/node ~/.kuzya/tools/playwright/verify-sandbox.mjs
```

Expected: `TITLE=kuzya-ok`, exit 0. Mandatory chromium flags inside bwrap:
`--no-sandbox` (chromium's sandbox conflicts with the outer namespace) and
`--disable-dev-shm-usage`.

### 3. Install the skill

```bash
mkdir -p ~/.kuzya/data/skills/web-testing
cp docs/toolkits/web-testing.SKILL.md ~/.kuzya/data/skills/web-testing/SKILL.md
```

The skill carries everything environment-specific: the node binary path (it is
not on PATH inside the sandbox), `PLAYWRIGHT_BROWSERS_PATH`, the ESM import
gotcha (`NODE_PATH` does not work for ESM outside the toolkit dir — use
`createRequire` with an absolute-path fallback), and `PYTHONIOENCODING=utf-8`.

### 4. Grant to agent profiles (PATCH /agents/{id})

Add to the profile's `runner_config`:

- `readonly_paths += ["~/.kuzya/tools/playwright", "~/.cache/ms-playwright"]`
  (host paths; they appear at the same paths inside the sandbox);
- `skills += ["web-testing"]`.

**Critical gotcha — `runtime_paths`.** The platform injects default runtime
paths (pi bundle, node, uv, python) only when the profile has **no**
`runtime_paths` key at all. Setting a custom non-empty list **overrides the
injection** and breaks `pi_command` validation ("pi_command path needs an
explicit runtime_paths entry") — tasks fail at launch. If you must extend
`runtime_paths`, include the full default set in the list: the pi release dir
(`~/.pi/agent/install/releases/<ver>`), `~/.local/bin/uv`, and
`~/.local/share/uv/python/cpython-3.14.7-...`. In most cases you do not need
to touch `runtime_paths` at all — the skill uses absolute paths to the mounted
node binary.

### 5. Verify end to end

Give an agent a real task ("open pages, screenshot to /outbox") and check the
artifacts. First battle run reference: task #83 (2026-10-22) — dev server up,
6 screenshots, zero console errors.

## Why this works

- `readonly_paths` entries are ro-bound at their host paths — agents see the
  toolkit at stable absolute paths, executables run fine from read-only mounts.
- Profiles are immutable revisions; granting is a PATCH that creates a new
  revision, instantly effective for new tasks.
- Skills are instruction packages bound per profile; agents demonstrably
  follow them.
- Nothing above requires changes to `kuzya` code or a platform deploy.
