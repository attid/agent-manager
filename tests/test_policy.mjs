import assert from 'node:assert/strict';
import policy, { blockedCommand } from '../src/pi_runner/policy.mjs';

assert.equal(blockedCommand('git commit -m test'), true);
assert.equal(blockedCommand('/usr/bin/git push origin HEAD'), true);
assert.equal(blockedCommand('cd /workspace && git -C . push'), true);
assert.equal(blockedCommand('g=git; "$g" push'), false); // documented bypass
assert.equal(blockedCommand('git log --oneline'), false);
assert.equal(blockedCommand('python3 publish.py'), false); // not a command security boundary
let handler;
policy({ on: (name, fn) => { assert.equal(name, 'tool_call'); handler = fn; } });
assert.equal((await handler({ toolName: 'bash', input: { command: 'git push' } })).block, true);
assert.equal(await handler({ toolName: 'bash', input: { command: 'git log' } }), undefined);
console.log('Policy guard: direct commit/push blocked; bypass boundaries verified.');
