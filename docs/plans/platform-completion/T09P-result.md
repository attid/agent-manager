# T09P — Verify AGY round 1 P2 and fix Markdown review findings: result

Status: complete. Fix and regression commit `e38718b` on
`platform/t09-review-parser`; base `5c1e951`.

## Reproduction before the fix

The exact AGY round-1 output was fed to `parse_review_report` before any
source change:

```bash
PYTHONPATH=/home/itolstov/Projects/mtl/AgentManager/worktrees/t09-review-parser/src \
  .venv/bin/python - <<'PY'
from agent_manager.review_executor import parse_review_report, ReviewFormatError
text = '''### Замечания

**P1: index.html:2 — кнопка фактически не становится зелёной.**
Класс `class="primary"` добавлен, но в проекте нет ни одного определения `.primary`...

ВЕРДИКТ: ТРЕБУЮТСЯ ИСПРАВЛЕНИЯ
'''
try:
    print(parse_review_report(text))
except ReviewFormatError as e:
    print(type(e).__name__, e)
PY
```

Result:

```
ReviewFormatError review verdict changes_requested has no evidence-linked findings
```

This is the `invalid_report` failure reported by AGY: a real
`changes_requested` report with a valid P1 finding was rejected because the
finding regex did not accept Markdown bold decoration.

## Fix

`src/agent_manager/review_executor.py`

- Added `_LIST_ITEM_RE` and normalized each candidate line with
  `_normalize_finding_line`: strip one list marker, then an enclosing inline
  code, `**bold**` or `__bold__` wrapper. Severity-only decoration such as
  `**P1**: ...` is handled by allowing optional emphasis markers around the
  severity and separator in `_FINDING_RE`.
- `_FINDING_RE` now allows en dash (`–`) and em dash (`—`) separators in
  addition to `:` and `-`, and captures the message body separately.
- The body is stripped of trailing `*`, `_` and backticks before use, so the
  stored message contains no trailing markup. Empty bodies are skipped, which
  keeps `changes_requested` without a real finding a hard failure.
- The explicit verdict, `approved`-with-critical contradiction and
  `changes_requested`-without-finding rules are unchanged. The parser never
  derives approval from a malformed report.

Public interface is unchanged: `parse_review_report(report_text)` still
returns `(verdict, tuple[ReviewFinding, ...])`. `ReviewFinding` fields
(`severity`, `message`, `file`, `line`, `evidence`) are unchanged.

## Regression tests

`tests/test_review_executor.py` adds:

- `test_parse_changes_requested_accepts_markdown_finding_variants`:
  parametrized over the exact AGY line plus list, `__bold__`, inline-code,
  `**P1**: ...`, `**P1** — ...`, `**P1:** ...` and plain en/em dash variants,
  asserting severity, file, line and the exact markup-free message.
- `test_parse_changes_requested_rejects_malformed_markdown_finding`:
  empty body, missing separator, invalid severity and wrongly decorated
  severity still raise `ReviewFormatError`.
- `test_parse_bold_finding_without_verdict_still_fails` and
  `test_parse_approved_with_bold_critical_finding_fails` preserve the strict
  verdict and contradiction rules.
- `test_execute_changes_requested_accepts_markdown_bold_finding`: the
  executor produces a `changes_requested` result with file/line and clean
  message from the real AGY-style report.

## Validation commands and outcomes

Focused parser/executor/task/operation regressions:

```bash
PYTHONPATH=/home/itolstov/Projects/mtl/AgentManager/worktrees/t09-review-parser/src \
  .venv/bin/python -m pytest \
  tests/test_review_executor.py tests/test_review_operation.py \
  tests/test_task_worker.py tests/test_operations.py \
  -q -o addopts='' --tb=short
```

Outcome: `77 passed, 1 warning in 6.04s`, exit code `0`.

Real enabled isolated acceptance once, normal host auth, disposable instance:

```bash
AGENT_MANAGER_REAL_MODEL_E2E=1 \
PYTHONPATH=/home/itolstov/Projects/mtl/AgentManager/worktrees/t09-review-parser/src \
  .venv/bin/python -m pytest tests/test_e2e_real_model.py \
  -q -o addopts='' --tb=short
```

Outcome: `2 passed in 80.91s`, exit code `0`. The real lifecycle reviewer
produced a valid verdict and the review operation succeeded. No credential
was read or echoed, and no production endpoint or Telegram instance was used.

Full suite once after the final source changes:

```bash
PYTHONPATH=/home/itolstov/Projects/mtl/AgentManager/worktrees/t09-review-parser/src \
  .venv/bin/python -m pytest -q -o addopts='' --tb=short
```

Outcome: `479 passed, 2 skipped, 1 warning in 52.50s`, exit code `0`. The two
skips are the opt-in real-model tests without `AGENT_MANAGER_REAL_MODEL_E2E`.

```bash
git diff --check
```

Outcome: no output, exit code `0`.

Exact AGY reproduction through `parse_review_report` after the fix returned
`changes_requested` with `severity=P1`, `file=index.html`, `line=2` and
`message="index.html:2 — кнопка фактически не становится зелёной."`.

## Commit

- `e38718b` — `fix(review): accept Markdown-decorated reviewer findings`
  (source + regression tests).
- This result file and `T09P.md` are committed separately as the task
  report/spec.

## Remaining gaps

- No known functional gap. The parser now normalizes ordinary Markdown
  emphasis around severity and the whole finding; it does not attempt to
  interpret arbitrary nested Markdown, decorative blockquotes or non-ASCII
  colon variants beyond the documented separators.
- The real-model acceptance is opt-in and depends on the host Pi runtime and
  provider availability; it was run once here and passed. It is not part of
  the default suite.
