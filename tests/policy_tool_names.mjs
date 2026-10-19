// Enumerate the real registered JS tools without invoking any tool body.
// Usage: node policy_tool_names.mjs <path-to-policy.mjs>
// Prints a JSON array of registered tool names.
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const [policyPath] = process.argv.slice(2);
assert.ok(policyPath, 'policy path is required');
const names = [];
const { default: policy } = await import(pathToFileURL(policyPath));
policy({ on() {}, registerTool(tool) { names.push(tool.name); } });
assert.ok(names.length > 0, 'policy registered no tools');
process.stdout.write(JSON.stringify(names));
