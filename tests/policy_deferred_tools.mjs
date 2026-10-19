// Exercise the session-deferred policy tools against a redirected /session
// directory and assert they return structured, truthful status objects.
// Usage: node policy_deferred_tools.mjs <path-to-policy.mjs> <session-dir>
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const [policyPath, session] = process.argv.slice(2);
assert.ok(policyPath && session, 'policy path and session dir are required');
const originalRead = fs.readFileSync.bind(fs);
const originalExists = fs.existsSync.bind(fs);
const originalWrite = fs.writeFileSync.bind(fs);
const redirect = value => typeof value === 'string' && value.startsWith('/session/')
  ? path.join(session, value.slice('/session/'.length)) : value;
fs.readFileSync = (name, ...args) => originalRead(redirect(name), ...args);
fs.existsSync = name => originalExists(redirect(name));
fs.writeFileSync = (name, ...args) => originalWrite(redirect(name), ...args);

fs.writeFileSync('/session/turn.json', JSON.stringify({ turn_id: 'turn-42' }));
const { default: policy } = await import(pathToFileURL(policyPath));
const tools = new Map();
policy({ on() {}, registerTool(tool) { tools.set(tool.name, tool); } });
const call = async (name, params, invocationId = `call-${name}`) => {
  const raw = await tools.get(name).execute(invocationId, params);
  const parsed = JSON.parse(raw.content[0].text);
  return { ...parsed, isError: raw.isError };
};

const deferred = [
  ['attach_document', { target: 'projects/demo/summary.md' }, 'attachments.json'],
  ['archive_agent', { agent_id: 'legacy-worker' }, 'archived_agents.json'],
  ['save_project_summary', { name: 'demo', summary: '# Demo' }, 'saved_projects.json'],
  ['write_knowledge', { category: 'docs', name: 'note.md', content: '# Note' }, 'written_knowledge.json'],
  ['update_component_rules', { component: 'runner', rules: '# Rules' }, 'written_knowledge.json'],
  ['install_skill', { name: 'demo-skill', source: '/tmp/demo-skill' }, 'install_skills.json'],
  ['create_skill', { name: 'inline-skill', skill_md: '# Skill' }, 'created_skills.json'],
];
for (const [name, params, receipt] of deferred) {
  const result = await call(name, params);
  assert.equal(result.status, 'pending', `${name}: ${JSON.stringify(result)}`);
  assert.notEqual(result.isError, true, `${name} reported an error`);
  assert.ok(result.instruction, `${name} must instruct the caller`);
  assert.ok(fs.existsSync(redirect(`/session/${receipt}`)), `${name} wrote no receipt`);
}

// An outgoing request carries the invocation and turn identity, not just a
// target, so the host can scope it to the turn that actually asked for it.
const attachPath = redirect('/session/attachments.json');
const readAttachments = () => JSON.parse(originalRead(attachPath, 'utf-8'));
let attachments = readAttachments();
assert.equal(attachments.length, 1, JSON.stringify(attachments));
assert.equal(attachments[0].target, 'projects/demo/summary.md');
assert.equal(attachments[0].invocation_id, 'call-attach_document');
assert.equal(attachments[0].turn_id, 'turn-42');
assert.ok(attachments[0].timestamp);
// A retry of the same tool invocation is stable: it does not append a
// second request for the same delivery.
const attachRetry = await call('attach_document', { target: 'projects/demo/summary.md' });
assert.equal(attachRetry.status, 'pending');
assert.equal(readAttachments().length, 1);
// A later intentional invocation for the same target is a new request.
const attachAgain = await call(
  'attach_document',
  { target: 'projects/demo/summary.md' },
  'call-attach-document-2',
);
assert.equal(attachAgain.status, 'pending');
attachments = readAttachments();
assert.equal(attachments.length, 2);
assert.equal(attachments[1].invocation_id, 'call-attach-document-2');
assert.equal(attachments[1].turn_id, 'turn-42');

const clarification = await call('ask_clarification', {
  title: 'Ambiguous scope', description: 'Two projects match.', question: 'Which project?',
});
assert.equal(clarification.status, 'awaiting_user', JSON.stringify(clarification));
assert.equal(clarification.question, 'Which project?');
assert.ok(fs.existsSync(redirect('/session/clarification.json')));

// Completion subscriptions use the host-provided conversation, never a
// model-supplied destination. Exercise the registered tools' real requests.
const missingContext = await call('subscribe_task', { task_id: '8' });
assert.equal(missingContext.status, 'failed');
fs.writeFileSync('/session/backend_api.json', JSON.stringify({ backend_url: 'http://fixture', conversation_id: 'origin-conversation' }));
const requests = [];
globalThis.fetch = async (url, init) => {
  requests.push({ url, method: init.method, body: JSON.parse(init.body) });
  return { ok: true, status: 200, text: async () => JSON.stringify({ status: 'pending' }) };
};
assert.equal((await call('subscribe_task', { task_id: '8', instructions: 'Summarize findings' })).status, 'applied');
assert.equal((await call('unsubscribe_task', { task_id: '8' })).status, 'applied');
assert.deepEqual(requests.map(r => r.method), ['POST', 'DELETE']);
assert.equal(requests[0].url, 'http://fixture/tasks/8/subscriptions');
assert.equal(requests[0].body.conversation_id, 'origin-conversation');
assert.equal(requests[0].body.instructions, 'Summarize findings');
console.log('Session-deferred tools and task subscriptions: passed.');
