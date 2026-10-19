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

const { default: policy } = await import(pathToFileURL(policyPath));
const tools = new Map();
policy({ on() {}, registerTool(tool) { tools.set(tool.name, tool); } });
const call = async (name, params) => {
  const raw = await tools.get(name).execute(`call-${name}`, params);
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

const clarification = await call('ask_clarification', {
  title: 'Ambiguous scope', description: 'Two projects match.', question: 'Which project?',
});
assert.equal(clarification.status, 'awaiting_user', JSON.stringify(clarification));
assert.equal(clarification.question, 'Which project?');
assert.ok(fs.existsSync(redirect('/session/clarification.json')));

console.log('Session-deferred tools return structured pending status: passed.');
