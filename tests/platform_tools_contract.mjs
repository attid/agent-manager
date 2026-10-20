import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const [policyPath, session, backend, workspace, knowledgeRoot] = process.argv.slice(2);
assert.ok(knowledgeRoot, 'knowledge root is required');
const originalRead = fs.readFileSync.bind(fs);
const originalExists = fs.existsSync.bind(fs);
const originalWrite = fs.writeFileSync.bind(fs);
const redirect = value => typeof value === 'string' && value.startsWith('/session/') ? path.join(session, value.slice('/session/'.length)) : value;
fs.readFileSync = (name, ...args) => originalRead(redirect(name), ...args);
fs.existsSync = name => originalExists(redirect(name));
fs.writeFileSync = (name, ...args) => originalWrite(redirect(name), ...args);
fs.writeFileSync('/session/backend_api.json', JSON.stringify({ backend_url: backend }));
const { default: policy } = await import(pathToFileURL(policyPath));
const tools = new Map();
policy({ on() {}, registerTool(tool) { tools.set(tool.name, tool); } });
const call = async (name, params, invocationId = 'test-call') => {
  const result = await tools.get(name).execute(invocationId, params);
  return { ...JSON.parse(result.content[0].text), isError: result.isError };
};
const created = await call('create_agent', { agent_id: 'scout', name: 'Scout', role: 'analyzer', system_prompt: 'Approved scout.', tools: ['read', 'bash'], runner_config: { project: workspace, project_read_only: true, network: true } });
assert.equal(created.status, 'applied', JSON.stringify(created));
assert.equal(created.data.revision, 1);
assert.equal(fs.existsSync('/session/created_agents.json'), false);
const duplicate = await call('create_agent', { agent_id: 'scout', name: 'Wrong', role: 'analyzer', system_prompt: 'Must not overwrite.' });
assert.equal(duplicate.http_status, 409);
assert.equal(duplicate.isError, true);
const updated = await call('update_agent', { agent_id: 'scout', system_prompt: 'Approved revision.', runner_config: { network: false } });
assert.equal(updated.status, 'applied', JSON.stringify(updated));
assert.equal(updated.data.revision, 2);
assert.equal(updated.data.runner_config.project_read_only, true);
assert.equal(updated.data.runner_config.network, false);
assert.deepEqual(updated.data.runner_config.tools, ['read', 'bash']);
const fetched = await fetch(`${backend}/agents/scout`).then(r => r.json());
assert.equal(fetched.system_prompt, 'Approved revision.');
const task = await call('create_task', { title: 'Find project', agent_id: 'scout', task_type: 'research' });
assert.equal(task.status, 'applied', JSON.stringify(task));
assert.equal(task.data.status, 'incoming');
assert.equal(task.data.agent_id, 'scout');
assert.equal(task.data.workspace, workspace);
const inspected = await call('get_task', { task_id: task.data.id });
assert.equal(inspected.data.agent_id, 'scout');
const listed = await call('list_tasks', {});
assert.equal(listed.data[0].agent_id, 'scout');
// get_task_logs advertises a structured ok/failed result like the other
// platform reads; it must not return raw Markdown to a JSON consumer.
const taskLogs = await call('get_task_logs', { task_id: task.data.id });
assert.equal(taskLogs.status, 'ok', JSON.stringify(taskLogs));
assert.equal(taskLogs.task_id, task.data.id);
assert.equal(taskLogs.title, 'Find project');
assert.equal(taskLogs.task_status, 'incoming');
assert.deepEqual(taskLogs.changed_files, []);
assert.equal(taskLogs.isError, false);
const missingLogs = await call('get_task_logs', { task_id: 'missing-task' });
assert.equal(missingLogs.status, 'failed');
assert.equal(missingLogs.http_status, 404);
assert.equal(missingLogs.isError, true);
const agentProfile = await call('get_agent', { agent_id: 'scout' });
assert.equal(agentProfile.data.runner_config.project_read_only, true);

assert.equal(task.execution.sandbox_path, '/workspace');
assert.equal(task.execution.project_read_only, true);
const changed = await call('update_task', { task_id: task.data.id, workspace: path.join(workspace, 'other'), agent_id: 'scout', status: 'in_progress' });
assert.equal(changed.status, 'applied', JSON.stringify(changed));
assert.equal(changed.data.workspace, path.join(workspace, 'other'));
const resumed = await call('resume_task_session', { task_id: task.data.id, message: 'Add a regression test.' });
assert.equal(resumed.status, 'applied', JSON.stringify(resumed));
assert.equal(resumed.task_status, 'in_progress');
const resumedTask = await fetch(`${backend}/tasks/${task.data.id}`).then(r => r.json());
assert.ok(resumedTask.metadata.additional_instructions.some(item => item.text === 'Add a regression test.'));
const missing = await call('create_task', { title: 'Must not create', workspace: path.join(workspace, 'missing'), agent_id: 'scout' });
assert.equal(missing.status, 'failed');
assert.equal(missing.http_status, 422);
const stored = await fetch(`${backend}/tasks`).then(r => r.json());
assert.equal(stored.length, 1);

// Trigger tools route through the durable operation API and return its id.
const review = await call('trigger_review', { task_id: task.data.id, instructions: 'Check edge cases' });
assert.equal(review.status, 'applied', JSON.stringify(review));
assert.equal(review.data.kind, 'review');
assert.equal(review.data.status, 'pending');
assert.equal(review.data.target_id, task.data.id);
assert.deepEqual(review.data.input, { instructions: 'Check edge cases' });
assert.equal(review.data.result, null);
const reviewRetry = await call('trigger_review', { task_id: task.data.id, instructions: 'Check edge cases' });
assert.equal(reviewRetry.data.id, review.data.id);
// A separate intentional invocation (a new toolCallId) is not a retry: it
// creates a new operation for the same task even with identical input, so a
// later review after code changes is not suppressed forever.
const reviewAgain = await call('trigger_review', { task_id: task.data.id, instructions: 'Check edge cases' }, 'test-call-2');
assert.equal(reviewAgain.status, 'applied', JSON.stringify(reviewAgain));
assert.notEqual(reviewAgain.data.id, review.data.id);
// An explicit idempotency key is honored across invocations, so a caller that
// wants a stable replay can still supply one.
const explicitFirst = await call('trigger_review', { task_id: task.data.id, idempotency_key: 'shared-explicit-key' }, 'invocation-a');
const explicitSecond = await call('trigger_review', { task_id: task.data.id, idempotency_key: 'shared-explicit-key' }, 'invocation-b');
assert.equal(explicitFirst.data.id, explicitSecond.data.id);
const analysis = await call('trigger_analysis', { task_id: task.data.id });
assert.equal(analysis.status, 'applied', JSON.stringify(analysis));
assert.equal(analysis.data.kind, 'analysis');
assert.equal(analysis.data.status, 'pending');
assert.notEqual(analysis.data.id, review.data.id);
const fetchedOperation = await fetch(`${backend}/operations/${review.data.id}`).then(r => r.json());
assert.equal(fetchedOperation.status, 'pending');
assert.equal(fetchedOperation.result, null);

// Analysis/recommendation workflow tools route through the analysis API.
const emptyReports = await call('list_analysis_reports', { task_id: task.data.id });
assert.equal(emptyReports.status, 'ok', JSON.stringify(emptyReports));
assert.deepEqual(emptyReports.data, []);
const emptyRecommendations = await call('list_recommendations', { task_id: task.data.id });
assert.equal(emptyRecommendations.status, 'ok', JSON.stringify(emptyRecommendations));
assert.deepEqual(emptyRecommendations.data, []);
const missingReport = await call('get_analysis_report', { report_id: 'missing' });
assert.equal(missingReport.status, 'failed');
assert.equal(missingReport.http_status, 404);
const missingPreview = await call('preview_recommendation', { recommendation_id: 'missing' });
assert.equal(missingPreview.status, 'failed');
assert.equal(missingPreview.http_status, 404);
const missingApprove = await call('approve_recommendation', { recommendation_id: 'missing' });
assert.equal(missingApprove.status, 'failed');
assert.equal(missingApprove.http_status, 404);
const missingApply = await call('apply_recommendation', { recommendation_id: 'missing' });
assert.equal(missingApply.status, 'failed');
assert.equal(missingApply.http_status, 404);
// Knowledge deletion is one exact Markdown file through DELETE /knowledge/{path}.
const agentDelete = await call('delete_knowledge', { path: 'agents/scout.md' });
assert.equal(agentDelete.status, 'failed', JSON.stringify(agentDelete));
assert.equal(agentDelete.isError, true);
assert.match(agentDelete.error, /profile API/);
assert.equal(fs.existsSync(path.join(knowledgeRoot, 'agents/scout.md')), true);
const wildcardDelete = await call('delete_knowledge', { path: 'docs/*.md' });
assert.equal(wildcardDelete.status, 'failed', JSON.stringify(wildcardDelete));
assert.equal(wildcardDelete.isError, true);
const deletedKnowledge = await call('delete_knowledge', { path: 'инструкции/секретарь.md' });
assert.equal(deletedKnowledge.status, 'applied', JSON.stringify(deletedKnowledge));
assert.equal(deletedKnowledge.data.deleted, true);
assert.equal(fs.existsSync(path.join(knowledgeRoot, 'инструкции/секретарь.md')), false);
assert.equal(fs.existsSync(path.join(knowledgeRoot, 'instructions/secretary.md')), true);
const missingKnowledge = await call('delete_knowledge', { path: 'инструкции/секретарь.md' });
assert.equal(missingKnowledge.status, 'failed', JSON.stringify(missingKnowledge));
assert.equal(missingKnowledge.http_status, 404);
console.log('Synchronous profile create/update, revision conflict, task routing, workspace preflight, durable trigger submission, exact knowledge deletion: passed.');
