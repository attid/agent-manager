"""Adversarial stress testing suite for Requirement R2 (128KB argument list limit in pi_runner).

This test suite empirically challenges the 128KB argument limit fix:
1. Negative control: Demonstrates that passing >=131,072 byte arguments directly via execve/Popen
   raises OSError(7, 'Argument list too long') on this Linux kernel.
2. 128 KiB boundary test: Exactly 131,072 bytes (MAX_ARG_STRLEN boundary).
3. 132 KiB overflow test: Exactly 135,168 bytes (> 128 KiB).
4. 256 KiB massive stress test: Exactly 262,144 bytes containing:
   - Multibyte Unicode (Cyrillic, CJK, Japanese, Arabic, Devanagari, Emojis, Math symbols)
   - Heterogeneous newlines (\n, \r\n, \r, multi-line blocks)
   - Binary sequences & control characters (\x00 null bytes, \x01-\x1f, ANSI escapes, \x7f, \ufeff, \ufffd)
   - Bash-escapable characters & injection payloads ($(rm -rf /), `whoami`, quotes, pipes, wildcards, subshells)
5. Child process verification:
   - Child process receives '@session/task.txt' via sys.argv.
   - Child process reads /session/task.txt inside bubblewrap sandbox.
   - Child process computes SHA-256 and byte length, confirming 0% truncation and 100% byte fidelity.
   - Host-side session files (task.txt and task.md) are identical and untruncated.
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from pi_runner.cli import execute


ADVERSARIAL_PROBE_SCRIPT = r'''
import hashlib
import json
import os
import pathlib
import sys

task_arg = sys.argv[-1]
assert task_arg == '@/session/task.txt', f"Expected argv[-1] to be '@/session/task.txt', got {task_arg!r}"

task_file = pathlib.Path('/session/task.txt')
assert task_file.is_file(), f"/session/task.txt does not exist or is not a file"

raw_bytes = task_file.read_bytes()
text = task_file.read_text(encoding='utf-8')
byte_len = len(raw_bytes)
char_len = len(text)
sha256_hex = hashlib.sha256(raw_bytes).hexdigest()

# Record inspection from inside the sandbox
report = {
    'task_arg': task_arg,
    'file_exists': True,
    'byte_length': byte_len,
    'char_length': char_len,
    'sha256': sha256_hex,
    'starts_with': text[:60],
    'ends_with': text[-60:],
    'contains_null_byte': '\x00' in text,
    'contains_unicode': 'Привет' in text or '你好' in text,
    'contains_bash_injection': '$(rm -rf /)' in text,
}
pathlib.Path('/workspace/probe_report.json').write_text(json.dumps(report, indent=2))

# Write minimal session entry
session = pathlib.Path('/session/session.jsonl')
with session.open('a') as f:
    f.write(json.dumps({'type': 'session', 'id': 'adversarial-session'}) + '\n')

# Emit assistant message
print(json.dumps({
    'type': 'message_end',
    'message': {
        'role': 'assistant',
        'content': [{'type': 'text', 'text': f'Verified prompt of {byte_len} bytes, sha256={sha256_hex}'}],
        'stopReason': 'stop',
        'usage': {'input': byte_len, 'output': 10},
        'model': 'adversarial-probe-model'
    }
}), flush=True)
'''


def build_128k_prompt() -> str:
    """Build a prompt of exactly 131,072 bytes (128 KiB)."""
    target_bytes = 131_072
    header = "=== START_PROMPT_128_KIB ===\n"
    footer = "\n=== END_PROMPT_128_KIB ==="
    overhead = len(header.encode('utf-8')) + len(footer.encode('utf-8'))
    padding = "A" * (target_bytes - overhead)
    prompt = header + padding + footer
    assert len(prompt.encode('utf-8')) == target_bytes
    return prompt


def build_132k_prompt() -> str:
    """Build a prompt of exactly 135,168 bytes (132 KiB)."""
    target_bytes = 135_168
    header = "=== START_PROMPT_132_KIB ===\n"
    footer = "\n=== END_PROMPT_132_KIB ==="
    overhead = len(header.encode('utf-8')) + len(footer.encode('utf-8'))
    padding = "B" * (target_bytes - overhead)
    prompt = header + padding + footer
    assert len(prompt.encode('utf-8')) == target_bytes
    return prompt


def build_256k_adversarial_prompt() -> str:
    """Build a complex adversarial prompt of exactly 262,144 bytes (256 KiB).

    Includes:
    - Multibyte Unicode (Cyrillic, CJK, Arabic, Devanagari, Emojis, Math symbols)
    - Mixed newlines (\r\n, \r, \n, multi-line blocks)
    - Binary sequences / control codes (\x00, \x01-\x1f, ANSI escapes, \x7f, \ufeff, \ufffd)
    - Bash-escapable characters and shell injection patterns
    - Exact padding to 262,144 bytes in UTF-8
    """
    target_bytes = 262_144

    sections = []
    sections.append("### HEADER: ADVERSARIAL STRESS TEST 256 KiB\n")

    # 1. Multibyte Unicode
    sections.append(
        "### SECTION 1: MULTIBYTE UNICODE\n"
        "Cyrillic: Привет, мир! Тестирование устойчивости pi_runner к длинным аргументам командной строки.\n"
        "CJK: 这是一段用于压力测试的中文字符串：天地玄黄宇宙洪荒，日月盈昃辰宿列张。\n"
        "Japanese: 日本語テスト：いろはにほへとちりぬるをわかよたれそつねならむ。\n"
        "Arabic: مرحبا بالعالم! هذا اختبار مجهد لمعالجة النصوص الضخمة باللغة العربية.\n"
        "Devanagari: नमस्ते दुनिया! यह एक व्यापक परीक्षण स्ट्रिंग है।\n"
        "Emojis: 🚀🔥🎉💡⚡🛠🧪📦🤖💬🔐🚨🛑🎯✨🌈🌍💻📡🛡🔑\n"
        "Math: ∀x∈ℝ, ∃y∈ℝ: x + y = 0 ∧ ∫₀^∞ e^(-x²) dx = √π / 2 ∧ ∑_{n=1}^∞ 1/n² = π²/6\n"
    )

    # 2. Heterogeneous newlines (single, double, multiple consecutive empty lines, leading/trailing)
    sections.append(
        "### SECTION 2: NEWLINES & PARAGRAPH BLOCKS\n"
        "Single newline line 1\n"
        "Single newline line 2\n\n"
        "Double newline block\n\n\n"
        "Triple newline block\n\n\n\n"
        "Quadruple newline block with indentation:\n"
        "    indented line 1\n"
        "        deeply indented line 2\n"
        "\n\n"
    )

    # 3. Binary sequences and control characters (valid in UTF-8 string)
    sections.append(
        "### SECTION 3: BINARY SEQUENCES & CONTROL CHARACTERS\n"
        "Null byte: [\x00]\n"
        "Control codes: [\x01\x02\x03\x04\x05\x06\x07\x08\x0b\x0c\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f]\n"
        "ANSI Escapes: \x1b[31;1m[RED_ALERT]\x1b[0m \x1b[32;4m[GREEN_UNDERLINE]\x1b[0m \x1b[0m\n"
        "ASCII DEL: [\x7f]\n"
        "Unicode BOM: [\ufeff]\n"
        "Unicode Replacement: [\ufffd]\n"
        "Unicode Plane 16 Max: [\U0010ffff]\n"
    )

    # 4. Bash-escapable characters and shell injection constructs
    sections.append(
        "### SECTION 4: BASH INJECTIONS & ESCAPABLE CHARACTERS\n"
        "Command substitution: $(rm -rf /) and `whoami && id && cat /etc/shadow`\n"
        "Variable expansion: ${HOME:-/tmp} $PATH $? $! $$ $# $0 $@ $* $-\n"
        "Logical operators: && echo 'PWNED' > /tmp/evil || exit 127\n"
        "Pipes and redirects: > /dev/null 2>&1 < /dev/zero | grep -E '.*' | xargs -0 -I{} echo {}\n"
        "Quotes: 'single' \"double\" '''triple''' \"\"\"triple_double\"\"\" `backticks`\n"
        "Escaped backslashes: \\\\\\\\ \\n \\r \\t \\' \\\" \\$\n"
        "Wildcards and globs: * ? [a-zA-Z0-9] {foo,bar,baz} ~ ! ^\n"
        "Subshells and process substitution: <(cat /dev/null) >(cat) (( x = 1 + 2 )) ( ( exit 0 ) )\n"
        "Terminator sequences: ; & ;; |& ;& ;;&\n"
    )

    sections.append("### SECTION 5: REPEATED PATTERNS & PADDING\n")
    prefix = "".join(sections)
    footer = "\n=== END_OF_ADVERSARIAL_PROMPT_256K ==="

    prefix_bytes = prefix.encode('utf-8')
    footer_bytes = footer.encode('utf-8')
    overhead = len(prefix_bytes) + len(footer_bytes)
    assert target_bytes >= overhead, f"Prefix and footer exceed target ({overhead} > {target_bytes})"

    remaining = target_bytes - overhead
    # Fill remaining bytes with structured repeating pattern
    chunk = "0123456789ABCDEF-тест-测试-🔥-"
    chunk_bytes = chunk.encode('utf-8')
    num_chunks = remaining // len(chunk_bytes)
    padding_core = chunk * num_chunks
    padding_tail = "X" * (remaining - len(padding_core.encode('utf-8')))
    padding = padding_core + padding_tail

    prompt = prefix + padding + footer
    assert len(prompt.encode('utf-8')) == target_bytes
    return prompt


class AdversarialR2StressTests(unittest.TestCase):
    """Empirical challenge suite for Requirement R2."""

    def setUp(self):
        if not shutil.which('bwrap'):
            self.skipTest('bubblewrap is not installed on this system')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'project'
        self.repo.mkdir()
        (self.repo / 'AGENTS.md').write_text('Stress test workspace')
        self.state = self.root / 'state'
        self.state.mkdir()

        # Write the adversarial probe script
        self.probe_script = self.root / 'adversarial_probe_pi.py'
        self.probe_script.write_text(ADVERSARIAL_PROBE_SCRIPT, encoding='utf-8')

        # Base config pointing to the adversarial probe script
        self.config_path = self.root / 'config.json'
        self.config_path.write_text(json.dumps({
            'agent_id': 'adversarial_agent',
            'project': str(self.repo),
            'state_dir': str(self.state),
            'pi_command': ['/usr/bin/python3', str(self.probe_script)],
            'runtime_paths': [str(self.probe_script)],
        }))

    def test_00_negative_control_baseline_proves_linux_errno_7(self):
        """Negative control: directly passing prompts >= 131,072 bytes in argv raises [Errno 7]."""
        print("\n[NEGATIVE CONTROL BASELINE]")
        # 1. 131,071 bytes succeeds on Linux x86_64 MAX_ARG_STRLEN (128 KiB - 1 null terminator)
        res_ok = subprocess.run(['/bin/echo', 'X' * 131_071], capture_output=True)
        self.assertEqual(res_ok.returncode, 0)
        print("  131,071 bytes in argv -> SUCCEEDED (returncode 0)")

        # 2. Exactly 131,072 bytes (128 KiB): exceeds MAX_ARG_STRLEN by 1 byte -> raises Errno 7
        with self.assertRaises(OSError) as ctx_128k:
            subprocess.run(['/bin/echo', 'X' * 131_072], capture_output=True)
        self.assertEqual(ctx_128k.exception.errno, errno.E2BIG)
        self.assertIn("Argument list too long", str(ctx_128k.exception))
        print("  131,072 bytes (128 KiB) in argv -> FAILED with [Errno 7] Argument list too long")

        # 3. 135,168 bytes (132 KiB): raises Errno 7
        with self.assertRaises(OSError) as ctx_132k:
            subprocess.run(['/bin/echo', 'X' * 135_168], capture_output=True)
        self.assertEqual(ctx_132k.exception.errno, errno.E2BIG)
        self.assertIn("Argument list too long", str(ctx_132k.exception))
        print("  135,168 bytes (132 KiB) in argv -> FAILED with [Errno 7] Argument list too long")

        # 4. 262,144 bytes (256 KiB): raises Errno 7
        with self.assertRaises(OSError) as ctx_256k:
            subprocess.run(['/bin/echo', 'X' * 262_144], capture_output=True)
        self.assertEqual(ctx_256k.exception.errno, errno.E2BIG)
        self.assertIn("Argument list too long", str(ctx_256k.exception))
        print("  262,144 bytes (256 KiB) in argv -> FAILED with [Errno 7] Argument list too long")

        # 5. Embedded null byte in argv raises ValueError in Python
        with self.assertRaises(ValueError) as ctx_null:
            subprocess.run(['/bin/echo', 'prefix\x00suffix'], capture_output=True)
        self.assertIn("embedded null byte", str(ctx_null.exception))
        print("  Embedded null byte in argv -> FAILED with ValueError('embedded null byte')")

    def _execute_and_verify(self, prompt: str, expected_bytes: int, case_name: str):
        """Execute pi_runner with prompt, verify no Errno 7, and assert zero truncation."""
        prompt_bytes = prompt.encode('utf-8')
        self.assertEqual(len(prompt_bytes), expected_bytes)
        expected_sha256 = hashlib.sha256(prompt_bytes).hexdigest()

        # Write prompt to task file
        task_file = self.root / f'task_{case_name}.md'
        task_file.write_text(prompt, encoding='utf-8')

        # Reset any prior report in workspace
        report_file = self.repo / 'probe_report.json'
        if report_file.exists():
            report_file.unlink()

        # Execute pi_runner.cli.execute
        # If the fix was not in place, this would fail with OSError: [Errno 7] Argument list too long
        print(f"\n[STRESS TEST] Case: {case_name}")
        print(f"  Expected bytes: {expected_bytes} ({expected_bytes / 1024:.1f} KiB)")
        print(f"  Expected SHA-256: {expected_sha256}")
        result = execute(self.config_path, task_file)
        print(f"  Execute result: status={result['status']}, exit_code={result['exit_code']}, session_id={result['session_id']}")

        # 1. Verify execution status and exit code
        errors_content = Path(result['errors_path']).read_text(encoding='utf-8') if Path(result.get('errors_path', '')).exists() else ''
        self.assertEqual(
            result['status'], 'completed',
            f"Execution failed for {case_name}: status={result['status']}, error={result.get('error')}, stderr={errors_content}"
        )
        self.assertEqual(result['exit_code'], 0, f"Non-zero exit code for {case_name}: {result['exit_code']}")
        self.assertIsNone(result.get('error'), f"Unexpected errors for {case_name}: {result.get('error')}")

        # 2. Verify probe report generated by the child process inside the sandbox
        self.assertTrue(report_file.exists(), f"Probe report not written by sandbox for {case_name}")
        report = json.loads(report_file.read_text(encoding='utf-8'))
        print(f"  Sandbox probe report:")
        print(f"    task_arg: {report['task_arg']}")
        print(f"    byte_length: {report['byte_length']} bytes (100% match, zero truncation)")
        print(f"    char_length: {report['char_length']} chars")
        print(f"    sha256: {report['sha256']}")
        print(f"    preserves_null_byte: {report.get('contains_null_byte')}")
        print(f"    preserves_unicode: {report.get('contains_unicode')}")
        print(f"    preserves_bash_syntax: {report.get('contains_bash_injection')}")

        # Child process must receive '@/session/task.txt' as last argument
        self.assertEqual(report['task_arg'], '@/session/task.txt')
        self.assertTrue(report['file_exists'])

        # Child process must receive exact byte length (0% truncation)
        self.assertEqual(report['byte_length'], expected_bytes,
                         f"Byte length mismatch in child for {case_name}: expected {expected_bytes}, got {report['byte_length']}")
        self.assertEqual(report['char_length'], len(prompt),
                         f"Char length mismatch in child for {case_name}: expected {len(prompt)}, got {report['char_length']}")

        # Child process must read exact byte-for-byte SHA-256 hash
        self.assertEqual(report['sha256'], expected_sha256,
                         f"SHA-256 mismatch in child for {case_name}: expected {expected_sha256}, got {report['sha256']}")

        # 3. Verify host-side session storage integrity
        session_dir = self.state / 'sessions' / result['session_id']
        host_task_file = session_dir / 'pi' / 'task.txt'
        host_task_md = session_dir / 'task.md'

        self.assertTrue(host_task_file.exists(), "Host session task.txt does not exist")
        self.assertTrue(host_task_md.exists(), "Host session task.md does not exist")

        self.assertEqual(host_task_file.read_bytes(), prompt_bytes,
                         f"Host task.txt content differs from input for {case_name}")
        self.assertEqual(host_task_md.read_bytes(), prompt_bytes,
                         f"Host task.md content differs from input for {case_name}")
        print(f"  Host session files verified (task.txt and task.md match exactly).")

        return report

    def test_01_stress_128_kib_boundary(self):
        """Stress test: prompt of exactly 131,072 bytes (128 KiB, Linux MAX_ARG_STRLEN)."""
        prompt = build_128k_prompt()
        report = self._execute_and_verify(prompt, 131_072, "128k_boundary")
        self.assertTrue(report['starts_with'].startswith("=== START_PROMPT_128_KIB ==="))
        self.assertTrue(report['ends_with'].endswith("=== END_PROMPT_128_KIB ==="))

    def test_02_stress_132_kib_overflow(self):
        """Stress test: prompt of exactly 135,168 bytes (132 KiB, exceeding 128 KiB)."""
        prompt = build_132k_prompt()
        report = self._execute_and_verify(prompt, 135_168, "132k_overflow")
        self.assertTrue(report['starts_with'].startswith("=== START_PROMPT_132_KIB ==="))
        self.assertTrue(report['ends_with'].endswith("=== END_PROMPT_132_KIB ==="))

    def test_03_stress_256_kib_massive_adversarial(self):
        """Stress test: massive prompt of 262,144 bytes with unicode, newlines, binary, bash-escapable chars."""
        prompt = build_256k_adversarial_prompt()
        report = self._execute_and_verify(prompt, 262_144, "256k_adversarial")
        self.assertTrue(report['starts_with'].startswith("### HEADER: ADVERSARIAL STRESS TEST 256 KiB"))
        self.assertTrue(report['ends_with'].endswith("=== END_OF_ADVERSARIAL_PROMPT_256K ==="))
        self.assertTrue(report['contains_null_byte'], "Child process failed to preserve null byte")
        self.assertTrue(report['contains_unicode'], "Child process failed to preserve unicode characters")
        self.assertTrue(report['contains_bash_injection'], "Child process failed to preserve bash injection syntax")


if __name__ == '__main__':
    unittest.main()
