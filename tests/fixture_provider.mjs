// Deterministic protocol fixture: exercises real Pi tools without an LLM or API.
export default function fixture(pi) {
  let step = 0;
  const calls = [
    { name: 'read', arguments: { path: 'AGENTS.md' } },
    { name: 'bash', arguments: { command: 'python3 isolation_probe.py' } },
    { name: 'write', arguments: { path: 'result.txt', content: 'changed by real Pi\n' } },
    { name: 'bash', arguments: { command: 'git push' } },
  ];
  pi.registerProvider('runner-fixture', {
    api: 'runner-fixture-api', baseUrl: 'http://unused.invalid', apiKey: 'not-a-secret',
    models: ['fixture', 'fixture-next'].map(id => ({ id, name: id, reasoning: false, input: ['text'],
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, contextWindow: 128000, maxTokens: 1024 })),
    streamSimple(model, context) {
      if (!JSON.stringify(context).includes('RUNNER_AGENT_INSTRUCTIONS')) throw new Error('AGENTS.md was not loaded');
      const call = calls[step++];
      const message = { role: 'assistant', api: model.api, provider: model.provider, model: model.id,
        timestamp: Date.now(), content: call ? [{ type: 'toolCall', id: `call-${step}`, ...call }] : [{ type: 'text', text: 'Real Pi fixture completed' }],
        stopReason: call ? 'toolUse' : 'stop',
        usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
          cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } } };
      return {
        async *[Symbol.asyncIterator]() {
          yield { type: 'start', partial: message };
          yield { type: 'done', reason: message.stopReason, message };
        },
        result: async () => message,
      };
    },
  });
}
