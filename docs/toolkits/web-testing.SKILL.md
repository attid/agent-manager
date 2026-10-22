---
name: web-testing
description: Use when the task involves a web interface - driving pages with Playwright (headless Chromium), filling forms, clicking, scraping rendered state, or taking screenshots to verify UI behaviour. Covers running the app locally in the sandbox and testing external URLs.
---

# Web testing with Playwright (headless Chromium)

A shared read-only Playwright toolkit is mounted in the sandbox. Nothing to
install - write a small Node script and run it.

## Paths and environment (required)

The toolkit package and the browser binaries are mounted at their host paths:

```bash
NODE=/home/itolstov/.nvm/versions/node/v24.19.0/bin/node
export PLAYWRIGHT_BROWSERS_PATH=/home/itolstov/.cache/ms-playwright
```

Run scripts with the exact node binary above (`node` is not on PATH).

**Importing playwright (verified in battle):** `NODE_PATH` does NOT work for
ESM scripts outside the toolkit directory (`ERR_MODULE_NOT_FOUND`). Use
CommonJS require with an absolute-path fallback instead:

```js
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
let chromium;
try {
  ({ chromium } = require('playwright'));
} catch {
  ({ chromium } = require('/home/itolstov/.kuzya/tools/playwright/node_modules/playwright'));
}
```

## Mandatory launch flags inside this sandbox

```js
import { chromium } from 'playwright';          // with NODE_PATH set
// or: const { chromium } = require('playwright');
const browser = await chromium.launch({
  args: ['--no-sandbox', '--disable-dev-shm-usage'],
});
```

- `--no-sandbox` - Chromium's own sandbox conflicts with the outer Bubblewrap
  namespace; without this flag the browser will not start.
- `--disable-dev-shm-usage` - no writable `/dev/shm` in the sandbox.

## Reference script

A verified minimal example lives at
`/home/itolstov/.kuzya/tools/playwright/verify-sandbox.mjs` (launch, goto,
`page.title()`, screenshot). Copy it as a starting point.

## Common gotchas (from battle runs)

- **Emoji/UTF-8 in bash**: page HTML often contains emoji; printing them from
  Python without a UTF-8 locale raises `UnicodeEncodeError` mid-script. Export
  `PYTHONIOENCODING=utf-8` (or use `grep`/`jq` instead of printing raw HTML).

## Patterns

- **Testing the task's own app**: start the app in the background on a
  localhost port (`my-server & sleep 1`), then `page.goto('http://127.0.0.1:PORT')`.
  The sandbox shares the host network, so localhost works both ways.
- **External URLs**: allowed (the profile runs with full network). For
  authenticated pages keep credentials in the workspace secrets, never in
  committed files or screenshots.
- **Artifacts**: write screenshots, traces and reports to `/outbox` - they are
  delivered to the task artifacts and survive the session. Do not write them
  into the toolkit or browser paths: those mounts are read-only.
- **Waiting**: prefer `await page.waitForURL(...)` / `waitForSelector(...)`
  over `sleep`; the app may be slow to boot on first request.
- **Cleanup**: always `await browser.close()` in a `finally` block; a leaked
  Chromium keeps running for the whole session.
