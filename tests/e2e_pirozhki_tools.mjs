// Drive the real JavaScript platform tools against an isolated E2E backend.
//
// Usage:
//   node e2e_pirozhki_tools.mjs <policy> <session> <backend> <workspace> \
//       <phase> [argument]
//
// The script never calls a model. It loads the production `policy.mjs`, mocks
// only the `/session/` filesystem prefix and the backend discovery file, and
// prints one JSON object to stdout so the Python acceptance test can assert
// the cross-language boundary.

import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const [policyPath, session, backend, workspace, phase, argument] =
  process.argv.slice(2);
if (!policyPath || !session || !backend || !workspace || !phase) {
  console.error('missing arguments');
  process.exit(2);
}

const originalRead = fs.readFileSync.bind(fs);
const originalExists = fs.existsSync.bind(fs);
const originalWrite = fs.writeFileSync.bind(fs);
const redirect = (value) =>
  typeof value === 'string' && value.startsWith('/session/')
    ? path.join(session, value.slice('/session/'.length))
    : value;
fs.readFileSync = (name, ...args) => originalRead(redirect(name), ...args);
fs.existsSync = (name) => originalExists(redirect(name));
fs.writeFileSync = (name, ...args) => originalWrite(redirect(name), ...args);

fs.writeFileSync(
  '/session/backend_api.json',
  JSON.stringify({ backend_url: backend, api_token: process.env.E2E_ADMIN_TOKEN }),
);

const { default: policy } = await import(pathToFileURL(policyPath));
const tools = new Map();
policy({ on() {}, registerTool(tool) { tools.set(tool.name, tool); } });

const call = async (name, params, invocationId = 'e2e-call') => {
  const tool = tools.get(name);
  assert.ok(tool, `tool not registered: ${name}`);
  const result = await tool.execute(invocationId, params);
  const parsed = JSON.parse(result.content[0].text);
  parsed.isError = result.isError === true;
  return parsed;
};

const emit = (value) => console.log(JSON.stringify(value));
const fail = (value) => {
  emit(value);
  process.exit(1);
};

if (phase === 'submit') {
  const agent = await call('create_agent', {
    agent_id: 'pirozhki-developer',
    name: 'Pirozhki Developer',
    role: 'developer',
    system_prompt:
      'You develop the pirozhki-shop storefront. Edit files and report a summary.',
    tools: ['read', 'write', 'edit', 'bash', 'read_knowledge'],
    runner_config: {
      project: workspace,
      network: true,
      project_read_only: false,
      provider: 'test-provider',
      model: 'test-model',
      mode: 'success',
      summary_text: 'Заменил кнопку заказа: зелёная, текст «Заказать горячий пирожок».',
      changed_files: ['index.html'],
      write_files: {
        'index.html':
          '<!doctype html>\n<html><body><button id="order" class="primary">Заказать горячий пирожок</button></body></html>\n',
      },
    },
  });
  if (agent.status !== 'applied') {
    fail({ phase, ok: false, agent });
  }
  const created = await call('create_task', {
    title: 'Поменять дизайн кнопки заказа',
    description: 'Сделать кнопку заказа зелёной с текстом «Заказать горячий пирожок».',
    workspace,
    agent_id: 'pirozhki-developer',
    task_type: 'coding',
  });
  if (created.status !== 'applied') {
    fail({ phase, ok: false, task: created });
  }
  const started = await call('update_task', {
    task_id: created.data.id,
    status: 'in_progress',
  });
  if (started.status !== 'applied') {
    fail({ phase, ok: false, task: started });
  }
  emit({
    phase,
    ok: true,
    task_id: created.data.id,
    agent_id: 'pirozhki-developer',
    agent_revision: agent.data.revision,
    task_status: started.data.status,
    host_path: started.execution ? started.execution.host_path : null,
  });
} else if (phase === 'review') {
  assert.ok(argument, 'review phase requires a task id');
  const reviewRetry = await call(
    'trigger_review',
    { task_id: argument, instructions: 'Проверить изменение кнопки' },
    'review-invocation-1',
  );
  const reviewReplay = await call(
    'trigger_review',
    { task_id: argument, instructions: 'Проверить изменение кнопки' },
    'review-invocation-1',
  );
  const reviewAgain = await call(
    'trigger_review',
    { task_id: argument, instructions: 'Повторная проверка после правок' },
    'review-invocation-2',
  );
  emit({
    phase,
    ok: reviewRetry.status === 'applied' && reviewAgain.status === 'applied',
    review_operation_id: reviewRetry.data.id,
    review_replayed: reviewReplay.data.id,
    review_second_operation_id: reviewAgain.data.id,
    review_status: reviewRetry.data.status,
  });
} else if (phase === 'analysis') {
  assert.ok(argument, 'analysis phase requires a task id');
  const analysis = await call(
    'trigger_analysis',
    { task_id: argument },
    'analysis-invocation-1',
  );
  emit({
    phase,
    ok: analysis.status === 'applied',
    analysis_operation_id: analysis.data.id,
    analysis_status: analysis.data.status,
  });
} else if (phase === 'recommend') {
  assert.ok(argument, 'recommend phase requires a recommendation id');
  const preview = await call('preview_recommendation', {
    recommendation_id: argument,
  });
  const approved = await call('approve_recommendation', {
    recommendation_id: argument,
  });
  const applied = await call('apply_recommendation', {
    recommendation_id: argument,
  });
  emit({
    phase,
    ok: applied.status === 'applied',
    would_conflict: preview.data.would_conflict,
    approved_status: approved.data.status,
    applied_status: applied.data.status,
    applied_revision: applied.data.applied_revision,
    applied_content_hash: applied.data.applied_content_hash,
  });
} else {
  console.error(`unknown phase: ${phase}`);
  process.exit(2);
}
