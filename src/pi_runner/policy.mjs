import fs from "node:fs";
import path from "node:path";
import { createHash } from "node:crypto";
import { Type } from "typebox";

// An accidental-action guard. This is deliberately not a shell security parser.
export function blockedCommand(command, allowCommit = false) {
  // Recognize common Git options including -C DIR and -c NAME=VALUE.
  // Variable expansion, aliases and interpreted programs are outside this guard.
  if (allowCommit) {
    return /(?:^|[\s;&|()])(?:[^\s;&|()]*\/)?git\s+(?:(?:(?:-C|-c|--git-dir|--work-tree)\s+[^\s;&|()]+|-[^\s;&|()]+)\s+)*(?:push)\b/.test(command);
  }
  return /(?:^|[\s;&|()])(?:[^\s;&|()]*\/)?git\s+(?:(?:(?:-C|-c|--git-dir|--work-tree)\s+[^\s;&|()]+|-[^\s;&|()]+)\s+)*(?:commit|push)\b/.test(command);
}

function appendJsonArray(filePath, item) {
  let list = [];
  try {
    if (fs.existsSync(filePath)) {
      list = JSON.parse(fs.readFileSync(filePath, "utf-8"));
      if (!Array.isArray(list)) list = [];
    }
  } catch (e) {
    list = [];
  }
  list.push(item);
  fs.writeFileSync(filePath, JSON.stringify(list, null, 2) + "\n", "utf-8");
}

export default function policy(pi) {
  function toolResult(result) {
    return { isError: result.status === "failed", content: [{ type: "text", text: JSON.stringify(result) }] };
  }
  async function backendRequest(method, endpoint, payload) {
    const cfg = getBackendConfig();
    if (!cfg.backend_url) {
      return { status: "failed", error: "platform backend is not configured for this execution",
        instruction: "This profile received no scoped backend credential. Do not invent a backend address." };
    }
    try {
      const response = await fetch(`${cfg.backend_url}${endpoint}`, {
        method,
        headers: { "Content-Type": "application/json", ...(cfg.api_token ? { Authorization: `Bearer ${cfg.api_token}` } : {}) },
        ...(payload !== undefined ? { body: JSON.stringify(payload) } : {})
      });
      const body = await response.text();
      let data;
      try { data = JSON.parse(body); } catch { data = body; }
      if (!response.ok) return { status: "failed", http_status: response.status, error: data,
        ...(method !== "GET" && response.status >= 500 ? { outcome: "unknown", instruction: "Read current state before retrying this write." } : {}) };
      return { status: method === "GET" ? "ok" : "applied", data };
    } catch (error) {
      return { status: "failed", outcome: method === "GET" ? "unavailable" : "unknown", error: error.message,
        instruction: "Read the current state before retrying; a lost response does not prove that a write failed." };
    }
  }
  async function executionPreflight(params) {
    const query = new URLSearchParams();
    if (params.workspace != null) query.set("workspace", params.workspace);
    if (params.agent_id != null) query.set("agent_id", params.agent_id);
    return backendRequest("GET", `/workspaces/resolve?${query}`);
  }
  function operationKey(kind, targetId, payload) {
    return createHash("sha256").update(JSON.stringify([kind, targetId ?? null, payload ?? null])).digest("hex");
  }
  // Submit a durable operation. The default key is deterministic for the same
  // kind/target/input, so an exact retry after a lost response resolves to the
  // same operation instead of duplicating the side effect.
  async function submitOperation(kind, targetId, payload, explicitKey) {
    const body = { kind, input: payload ?? {} };
    if (targetId != null) body.target_id = targetId;
    body.idempotency_key = explicitKey || operationKey(kind, targetId, payload);
    return backendRequest("POST", "/operations", body);
  }

  // 1. Guard against accidental git commit / push
  pi.on("tool_call", async (event) => {
    const allowCommit = fs.existsSync("/session/allow_git_commit") || process.env.RUNNER_ALLOW_GIT_COMMIT === "1";
    if (event.toolName === "bash" && blockedCommand(event.input.command ?? "", allowCommit)) {
      return {
        block: true,
        reason: allowCommit
          ? "Runner forbids automatic git push."
          : "Runner forbids automatic git commit and git push. Leave edits in the working tree."
      };
    }
  });

  // 2. Custom Tool: attach_document
  pi.registerTool({
    name: "attach_document",
    label: "Attach Document",
    description: "Attach and send a document from the knowledge base or workspace to the user in Telegram.",
    parameters: Type.Object({
      target: Type.String({
        description: "Target file path or shorthand name (e.g. 'agent-manager-developer', 'projects/agent-manager/summary.md', 'README.md')"
      })
    }),
    async execute(toolCallId, params) {
      const target = (params.target || "").trim();
      if (!target) {
        return { content: [{ type: "text", text: "Error: target cannot be empty" }] };
      }
      const sessionDir = "/session";
      const attachFile = path.join(sessionDir, "attachments.json");
      appendJsonArray(attachFile, {
        target,
        timestamp: new Date().toISOString()
      });
      return toolResult({ status: "pending", operation: "attach_document", target,
        instruction: "The document will be attached after this turn finishes." });
    }
  });

  // Profile writes use the same synchronous API as profile reads.
  pi.registerTool({
    name: "create_agent", label: "Create Agent",
    description: "Create an agent only after the user approved its complete Markdown draft. Returns applied and the stored profile; an existing ID is a conflict, never an upsert.",
    parameters: Type.Object({
      agent_id: Type.String(), name: Type.String(), role: Type.String(), system_prompt: Type.String(),
      tools: Type.Optional(Type.Array(Type.String())), skills: Type.Optional(Type.Array(Type.String())),
      runner_config: Type.Optional(Type.Record(Type.String(), Type.Any(), { description: "Runner policy, including project (host path), project_read_only and network. Network=false also blocks direct model API access." }))
    }),
    async execute(toolCallId, params) {
      const config = { ...(params.runner_config || {}) };
      if (params.tools !== undefined) config.tools = params.tools;
      if (params.skills !== undefined) config.skills = params.skills;
      const result = await backendRequest("POST", "/agents", {
        agent_id: params.agent_id, name: params.name, role: params.role,
        system_prompt: params.system_prompt, runner_config: config,
        skills: (params.skills || []).map(skill => ({ skill }))
      });
      return toolResult(result);
    }
  });

  // 3b. Custom Tool: archive_agent
  pi.registerTool({
    name: "archive_agent",
    label: "Archive Agent",
    description: "Archive (soft-delete) an existing agent profile from the system. Cannot be used on system agents (system-*).",
    parameters: Type.Object({
      agent_id: Type.String({ description: "Unique slug identifier of the agent to archive (e.g. 'project-scout')" })
    }),
    async execute(toolCallId, params) {
      const { agent_id } = params;
      if (!agent_id) {
        return { content: [{ type: "text", text: "Error: agent_id is required" }] };
      }
      const trimmedId = agent_id.trim();
      const coreAgents = ["system-secretary", "system-code-reviewer", "system-log-analyzer"];
      if (coreAgents.includes(trimmedId)) {
        return { content: [{ type: "text", text: `Error: базового системного агента "${trimmedId}" нельзя архивировать.` }] };
      }
      const sessionDir = "/session";
      const archiveFile = path.join(sessionDir, "archived_agents.json");
      appendJsonArray(archiveFile, {
        agent_id: trimmedId,
        timestamp: new Date().toISOString()
      });
      return {
        content: [{ type: "text", text: `Агент "${trimmedId}" помечен для архивации и будет архивирован.` }]
      };
    }
  });

  // 4. Custom Tool: read_knowledge
  pi.registerTool({
    name: "read_knowledge",
    label: "Read Knowledge",
    description: "Read a document, project summary, component rules, or agent profile from the knowledge base.",
    parameters: Type.Object({
      query: Type.String({
        description: "Path or query in knowledge base (e.g. 'projects/agent-manager/summary.md', 'components/aiogram/rules.md', 'agent-manager-developer')"
      })
    }),
    async execute(toolCallId, params) {
      const query = (params.query || "").trim();
      if (!query) {
        return { content: [{ type: "text", text: "Error: query cannot be empty" }] };
      }
      const candidates = [
        path.join("/knowledge", query),
        path.join("/knowledge", `${query}.md`),
        path.join("/knowledge", "agents", `${query}.md`),
        path.join("/knowledge", "projects", query, "summary.md"),
        path.join("/knowledge", "components", query, "rules.md")
      ];
      for (const p of candidates) {
        try {
          if (fs.existsSync(p) && fs.statSync(p).isFile()) {
            const content = fs.readFileSync(p, "utf-8");
            return {
              content: [{ type: "text", text: `# Knowledge: ${query}\n\n${content}` }]
            };
          }
        } catch (e) {}
      }
      return {
        content: [{ type: "text", text: `Документ по запросу "${query}" не найден в базе знаний.` }]
      };
    }
  });

  // 5. Custom Tool: ask_clarification
  pi.registerTool({
    name: "ask_clarification",
    label: "Ask Clarification",
    description: "Ask the user a clarifying question when requirements have ambiguity >= 10% or a critical obstacle occurs. Halts execution until the user replies.",
    parameters: Type.Object({
      title: Type.String({ description: "Short title of the clarification/blocker" }),
      description: Type.String({ description: "Detailed description (a full paragraph) of facts, findings, and hypotheses" }),
      question: Type.String({ description: "Clear, open-ended question to the user asking for directions" })
    }),
    async execute(toolCallId, params) {
      const { title, description, question } = params;
      const sessionDir = "/session";
      const clarFile = path.join(sessionDir, "clarification.json");
      const record = {
        title: (title || "Уточнение задачи").trim(),
        description: (description || "").trim(),
        question: (question || "").trim(),
        timestamp: new Date().toISOString()
      };
      fs.writeFileSync(clarFile, JSON.stringify(record, null, 2) + "\n", "utf-8");
      return {
        content: [{
          type: "text",
          text: `Уточняющий вопрос зафиксирован:\n\n**${record.title}**\n${record.description}\n\nВопрос: ${record.question}\n\nСессия переводится в ожидание ответа пользователя.`
        }]
      };
    }
  });

  // 6. Custom Tool: save_project_summary
  pi.registerTool({
    name: "save_project_summary",
    label: "Save Project Summary",
    description: "Save or update a project's summary.md in the knowledge base. Only call this tool after explicit confirmation from the user.",
    parameters: Type.Object({
      name: Type.String({ description: "Unique project slug / folder name (e.g. 'skynet_bot')" }),
      summary: Type.String({ description: "Complete markdown summary of the project (purpose, stack, key paths, architecture, ports)" })
    }),
    async execute(toolCallId, params) {
      const { name, summary } = params;
      if (!name || !summary) {
        return { content: [{ type: "text", text: "Error: name and summary are required" }] };
      }
      const sessionDir = "/session";
      const projectSummariesFile = path.join(sessionDir, "saved_projects.json");
      appendJsonArray(projectSummariesFile, {
        name: name.trim(),
        summary: summary.trim(),
        timestamp: new Date().toISOString()
      });
      return {
        content: [{ type: "text", text: `Описание проекта "${name}" зафиксировано и будет сохранено в базе знаний.` }]
      };
    }
  });

  function getBackendConfig() {
    // The Runner writes the explicit per-instance backend document. There is
    // no compiled-in production address or token fallback.
    try {
      if (fs.existsSync("/session/backend_api.json")) {
        const parsed = JSON.parse(fs.readFileSync("/session/backend_api.json", "utf-8"));
        if (parsed && typeof parsed === "object") return parsed;
      }
    } catch (e) {}
    return {};
  }

  // 7. Custom Tool: openviking_search
  pi.registerTool({
    name: "openviking_search",
    label: "OpenViking Search",
    description: "Search long-term memories, documentation, and resources in OpenViking knowledge repository.",
    parameters: Type.Object({
      query: Type.String({ description: "Search query or question" }),
      limit: Type.Optional(Type.Number({ description: "Max results to return (default: 5)" }))
    }),
    async execute(toolCallId, params) {
      const cfg = getBackendConfig();
      if (!cfg.openviking_url || !cfg.openviking_token) {
        return { content: [{ type: "text", text: "OpenViking не настроен для этой инсталляции." }] };
      }
      const limit = params.limit || 5;
      try {
        const res = await fetch(`${cfg.openviking_url}/api/v1/search/search`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${cfg.openviking_token}`
          },
          body: JSON.stringify({ query: params.query, limit })
        });
        if (!res.ok) {
          return { content: [{ type: "text", text: `OpenViking search returned HTTP ${res.status}: ${await res.text()}` }] };
        }
        const data = await res.json();
        const memories = (data.result && data.result.memories) || [];
        if (!memories.length) {
          return { content: [{ type: "text", text: `В OpenViking ничего не найдено по запросу: "${params.query}"` }] };
        }
        let out = `### 🔍 Результаты OpenViking (${memories.length}):\n\n`;
        for (const m of memories) {
          out += `* **URI:** \`${m.uri}\` (Score: ${(m.score || 0).toFixed(2)})\n  ${(m.abstract || "").trim()}\n\n`;
        }
        return { content: [{ type: "text", text: out.trim() }] };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка связи со службой OpenViking: ${err.message}` }] };
      }
    }
  });

  // 8. Custom Tool: openviking_read
  pi.registerTool({
    name: "openviking_read",
    label: "OpenViking Read",
    description: "Read full content of a viking:// URI from OpenViking.",
    parameters: Type.Object({
      uri: Type.String({ description: "viking:// URI to read" })
    }),
    async execute(toolCallId, params) {
      const cfg = getBackendConfig();
      if (!cfg.openviking_url || !cfg.openviking_token) {
        return { content: [{ type: "text", text: "OpenViking не настроен для этой инсталляции." }] };
      }
      try {
        const res = await fetch(`${cfg.openviking_url}/api/v1/content/read?uri=${encodeURIComponent(params.uri)}`, {
          headers: {
            "Authorization": `Bearer ${cfg.openviking_token}`
          }
        });
        if (!res.ok) {
          return { content: [{ type: "text", text: `OpenViking read returned HTTP ${res.status}: ${await res.text()}` }] };
        }
        const data = await res.json();
        const text = data.result || JSON.stringify(data, null, 2);
        return { content: [{ type: "text", text: text }] };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка чтения из OpenViking: ${err.message}` }] };
      }
    }
  });

  pi.registerTool({
    name: "list_tasks", label: "List Tasks",
    description: "Read tasks, including the assigned agent and execution context, with optional filters.",
    parameters: Type.Object({ status: Type.Optional(Type.String()), workspace: Type.Optional(Type.String()) }),
    async execute(toolCallId, params) {
      const query = new URLSearchParams();
      if (params.status !== undefined) query.set("status", params.status);
      if (params.workspace !== undefined) query.set("workspace", params.workspace);
      return toolResult(await backendRequest("GET", `/tasks?${query}`));
    }
  });
  pi.registerTool({
    name: "get_task", label: "Get Task",
    description: "Read the complete task, assigned agent, workspace, execution context, error and provenance.",
    parameters: Type.Object({ task_id: Type.String() }),
    async execute(toolCallId, params) {
      return toolResult(await backendRequest("GET", `/tasks/${encodeURIComponent(params.task_id)}`));
    }
  });

  pi.registerTool({
    name: "resolve_workspace", label: "Resolve Workspace",
    description: "Read-only preflight: returns the exact host directory, /workspace mount, executor and filesystem/network policy. Missing explicit paths fail; no fallback to another project.",
    parameters: Type.Object({ workspace: Type.Optional(Type.String()), agent_id: Type.Optional(Type.String()) }),
    async execute(toolCallId, params) { return toolResult(await executionPreflight(params)); }
  });

  pi.registerTool({
    name: "create_task", label: "Create Task",
    description: "Create a task for an explicit agent and workspace. incoming saves a draft and does not run; in_progress authorizes execution. Preflight must succeed before creation.",
    parameters: Type.Object({
      title: Type.String(), description: Type.Optional(Type.String()),
      workspace: Type.Optional(Type.String()), agent_id: Type.Optional(Type.String()),
      status: Type.Optional(Type.String()), task_type: Type.Optional(Type.String())
    }),
    async execute(toolCallId, params) {
      const check = await executionPreflight(params);
      if (check.status === "failed") return toolResult(check);
      const created = await backendRequest("POST", "/tasks", {
        title: params.title, description: params.description || "",
        workspace: check.data.host_path, agent_id: check.data.agent_id,
        status: params.status || "incoming", metadata: { task_type: params.task_type || "coding" }
      });
      if (created.status !== "failed") created.execution = check.data;
      return toolResult(created);
    }
  });

  pi.registerTool({
    name: "update_task", label: "Update Task",
    description: "Edit a task, its executor or workspace. Setting in_progress starts execution. Verify the effective workspace and agent before routing changes or launch.",
    parameters: Type.Object({
      task_id: Type.String(), status: Type.Optional(Type.String()), title: Type.Optional(Type.String()),
      description: Type.Optional(Type.String()), workspace: Type.Optional(Type.String()), agent_id: Type.Optional(Type.String())
    }),
    async execute(toolCallId, params) {
      const patch = {};
      for (const key of ["status", "title", "description", "workspace", "agent_id"]) {
        if (params[key] !== undefined) patch[key] = params[key];
      }
      let execution;
      if (params.status === "in_progress" || params.workspace !== undefined || params.agent_id !== undefined) {
        const stored = await backendRequest("GET", `/tasks/${encodeURIComponent(params.task_id)}`);
        if (stored.status === "failed") return toolResult(stored);
        const check = await executionPreflight({ ...stored.data, ...patch });
        if (check.status === "failed") return toolResult(check);
        execution = check.data;
        patch.workspace = execution.host_path;
        patch.agent_id = execution.agent_id;
      }
      const result = await backendRequest("PATCH", `/tasks/${encodeURIComponent(params.task_id)}`, patch);
      if (execution && result.status !== "failed") result.execution = execution;
      return toolResult(result);
    }
  });

  // 13. Custom Tool: get_task_logs
  pi.registerTool({
    name: "get_task_logs",
    label: "Get Task Logs",
    description: "Retrieve execution summary, errors, diffs, and review feedback for a task.",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID" })
    }),
    async execute(toolCallId, params) {
      const cfg = getBackendConfig();
      if (!cfg.backend_url) {
        return { content: [{ type: "text", text: "Платформенный backend не настроен для этой инсталляции." }] };
      }
      try {
        const headers = cfg.api_token ? { "Authorization": `Bearer ${cfg.api_token}` } : {};
        const res = await fetch(`${cfg.backend_url}/tasks/${encodeURIComponent(params.task_id)}`, { headers });
        if (!res.ok) {
          return { content: [{ type: "text", text: `HTTP ${res.status}: ${await res.text()}` }] };
        }
        const t = await res.json();
        const meta = t.metadata || {};
        let out = `### 📋 Логи и отчёт по задаче #${t.id} (${t.title}):\n\n`;
        out += `* **Текущий статус:** \`${t.status}\`\n`;
        if (meta.error) {
          out += `* **❌ Ошибка выполнения:**\n\`\`\`\n${meta.error}\n\`\`\`\n\n`;
        }
        if (meta.summary) {
          out += `* **Резюме разработчика / ревьюера:**\n${meta.summary}\n\n`;
        }
        if (meta.changed_files && meta.changed_files.length) {
          out += `* **Изменённые файлы:**\n` + meta.changed_files.map(f => `  • \`${f}\``).join('\n') + '\n\n';
        }
        return { content: [{ type: "text", text: out.trim() }] };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка получения логов: ${err.message}` }] };
      }
    }
  });

  // 14. Custom Tool: resume_task_session
  pi.registerTool({
    name: "resume_task_session",
    label: "Resume Task Session",
    description: "Provide additional instructions or answers to a task agent and resume its session.",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID" }),
      message: Type.String({ description: "Clarifications, directions, or answers to resume execution with" })
    }),
    async execute(toolCallId, params) {
      const cfg = getBackendConfig();
      if (!cfg.backend_url) {
        return { content: [{ type: "text", text: "Платформенный backend не настроен для этой инсталляции." }] };
      }
      try {
        const headers = {
          "Content-Type": "application/json",
          ...(cfg.api_token ? { "Authorization": `Bearer ${cfg.api_token}` } : {})
        };
        // Retrieve current task metadata
        const getRes = await fetch(`${cfg.backend_url}/tasks/${encodeURIComponent(params.task_id)}`, { headers });
        if (!getRes.ok) return { content: [{ type: "text", text: `HTTP ${getRes.status}: ${await getRes.text()}` }] };
        const task = await getRes.json();
        const meta = task.metadata || {};
        const additional = meta.additional_instructions || [];
        additional.push({ text: params.message.trim(), timestamp: new Date().toISOString() });
        meta.additional_instructions = additional;

        // Patch back with in_progress status
        const patchRes = await fetch(`${cfg.backend_url}/tasks/${encodeURIComponent(params.task_id)}`, {
          method: "PATCH",
          headers,
          body: JSON.stringify({ status: "in_progress", metadata: meta })
        });
        if (!patchRes.ok) return { content: [{ type: "text", text: `HTTP ${patchRes.status}: ${await patchRes.text()}` }] };
        return {
          content: [{
            type: "text",
            text: `✅ Дополнительные указания добавлены к задаче #${params.task_id}. Задача возвращена в статус \`in_progress\` для продолжения работы.`
          }]
        };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка возобновления задачи: ${err.message}` }] };
      }
    }
  });

  pi.registerTool({
    name: "list_agents", label: "List Agents",
    description: "Read registered profiles, with their stored revisions and complete runtime policies. Does not present pending session records as registered agents.",
    parameters: Type.Object({}),
    async execute() { return toolResult(await backendRequest("GET", "/agents")); }
  });
  pi.registerTool({
    name: "get_agent", label: "Get Agent Profile",
    description: "Read the stored profile, revision and complete runner_config, including workspace and access policy.",
    parameters: Type.Object({ agent_id: Type.String() }),
    async execute(toolCallId, params) {
      return toolResult(await backendRequest("GET", `/agents/${encodeURIComponent(params.agent_id)}`));
    }
  });

  pi.registerTool({
    name: "update_agent", label: "Update Agent",
    description: "Apply approved changes to an existing agent. Uses PATCH with revision checking and preserves omitted fields. Never retry writes to inspect their result.",
    parameters: Type.Object({
      agent_id: Type.String(), system_prompt: Type.Optional(Type.String()),
      name: Type.Optional(Type.String()), role: Type.Optional(Type.String()),
      tools: Type.Optional(Type.Array(Type.String())), skills: Type.Optional(Type.Array(Type.String())),
      runner_config: Type.Optional(Type.Record(Type.String(), Type.Any()))
    }),
    async execute(toolCallId, params) {
      const current = await backendRequest("GET", `/agents/${encodeURIComponent(params.agent_id)}`);
      if (current.status === "failed") return toolResult(current);
      const profile = current.data;
      const patch = { expected_revision: profile.revision };
      for (const key of ["system_prompt", "name", "role"]) {
        if (params[key] !== undefined) patch[key] = params[key];
      }
      if (params.runner_config !== undefined || params.tools !== undefined || params.skills !== undefined) {
        patch.runner_config = { ...(profile.runner_config || {}), ...(params.runner_config || {}) };
        if (params.tools !== undefined) patch.runner_config.tools = params.tools;
        if (params.skills !== undefined) patch.runner_config.skills = params.skills;
      }
      if (params.skills !== undefined) patch.skills = params.skills.map(skill => ({ skill }));
      return toolResult(await backendRequest("PATCH", `/agents/${encodeURIComponent(params.agent_id)}`, patch));
    }
  });

  // 18. Custom Tool: list_skills
  pi.registerTool({
    name: "list_skills",
    label: "List Skills",
    description: "List all installed skills in /skills directory.",
    parameters: Type.Object({}),
    async execute(toolCallId, params) {
      const skillsDir = "/skills";
      try {
        if (!fs.existsSync(skillsDir)) {
          return { content: [{ type: "text", text: "Каталог /skills не обнаружен в песочнице." }] };
        }
        const entries = fs.readdirSync(skillsDir, { withFileTypes: true });
        const skills = [];
        for (const ent of entries) {
          if (ent.isDirectory()) {
            const skillMd = path.join(skillsDir, ent.name, "SKILL.md");
            let desc = "";
            if (fs.existsSync(skillMd)) {
              const content = fs.readFileSync(skillMd, "utf-8");
              const m = content.match(/description:\s*(.+)/i);
              if (m) desc = m[1].trim();
            }
            skills.push({ name: ent.name, description: desc });
          }
        }
        if (!skills.length) return { content: [{ type: "text", text: "Установленных скилов не найдено." }] };
        let out = `### 🧩 Доступные скилы (${skills.length}):\n\n`;
        for (const s of skills) {
          out += `* **\`${s.name}\`**: ${s.description || '*(без описания)*'}\n`;
        }
        return { content: [{ type: "text", text: out.trim() }] };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка чтения скилов: ${err.message}` }] };
      }
    }
  });

  // 19. Custom Tool: install_skill
  pi.registerTool({
    name: "install_skill",
    label: "Install Skill",
    description: "Install or import a skill into the system repository.",
    parameters: Type.Object({
      name: Type.String({ description: "Name of the skill" }),
      source: Type.String({ description: "Source location or OpenViking name" })
    }),
    async execute(toolCallId, params) {
      const sessionDir = "/session";
      const file = path.join(sessionDir, "install_skills.json");
      appendJsonArray(file, {
        name: params.name.trim(),
        source: params.source.trim(),
        timestamp: new Date().toISOString()
      });
      return {
        content: [{ type: "text", text: `Запрос на установку скила "${params.name}" из "${params.source}" зафиксирован и будет выполнен.` }]
      };
    }
  });

  // 20. Custom Tool: create_skill
  pi.registerTool({
    name: "create_skill",
    label: "Create Skill",
    description: "Create a new custom skill with SKILL.md and optional helper scripts.",
    parameters: Type.Object({
      name: Type.String({ description: "Unique skill slug" }),
      skill_md: Type.String({ description: "Complete SKILL.md markdown with YAML frontmatter" }),
      scripts: Type.Optional(Type.Record(Type.String(), Type.String(), { description: "Map of filename to script content" }))
    }),
    async execute(toolCallId, params) {
      const sessionDir = "/session";
      const file = path.join(sessionDir, "created_skills.json");
      appendJsonArray(file, {
        name: params.name.trim(),
        skill_md: params.skill_md.trim(),
        scripts: params.scripts || {},
        timestamp: new Date().toISOString()
      });
      return {
        content: [{ type: "text", text: `Новый скил "${params.name}" зарегистрирован и будет сохранён в хранилище скилов.` }]
      };
    }
  });

  // 21. Custom Tool: list_knowledge
  pi.registerTool({
    name: "list_knowledge",
    label: "List Knowledge",
    description: "List documents, projects, rules, or agents in the knowledge base.",
    parameters: Type.Object({
      category: Type.Optional(Type.String({ description: "Optional category: 'projects', 'agents', 'components', 'docs'" }))
    }),
    async execute(toolCallId, params) {
      const targetDir = params.category ? path.join("/knowledge", params.category) : "/knowledge";
      try {
        if (!fs.existsSync(targetDir)) return { content: [{ type: "text", text: `Категория "${params.category || ''}" не найдена в базе знаний.` }] };
        const items = [];
        function walk(dir, rel = "") {
          for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
            if (ent.name.startsWith(".")) continue;
            const subRel = rel ? `${rel}/${ent.name}` : ent.name;
            if (ent.isDirectory()) {
              walk(path.join(dir, ent.name), subRel);
            } else if (ent.isFile() && ent.name.endsWith(".md")) {
              items.push(subRel);
            }
          }
        }
        walk(targetDir);
        if (!items.length) return { content: [{ type: "text", text: "Документы не найдены." }] };
        let out = `### 📚 База знаний [${params.category || 'все'}]:\n\n`;
        for (const it of items) out += `* \`${it}\`\n`;
        return { content: [{ type: "text", text: out.trim() }] };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка просмотра базы знаний: ${err.message}` }] };
      }
    }
  });

  // 22. Custom Tool: write_knowledge
  pi.registerTool({
    name: "write_knowledge",
    label: "Write Knowledge",
    description: "Write or update a documentation or specification file in the knowledge base.",
    parameters: Type.Object({
      category: Type.String({ description: "Category, e.g. 'docs', 'projects', 'rules'" }),
      name: Type.String({ description: "File path or name within category (e.g. 'architecture.md')" }),
      content: Type.String({ description: "Complete markdown content" })
    }),
    async execute(toolCallId, params) {
      if (path.join(params.category, params.name).split(path.sep)[0] === "agents") {
        return { isError: true, content: [{ type: "text", text: "Use create_agent or update_agent for approved profiles. Store drafts under docs/drafts and attach them for approval." }] };
      }
      const relative = path.join(params.category, params.name);
      if (path.isAbsolute(params.category) || path.isAbsolute(params.name) || path.isAbsolute(relative) || relative === ".." || relative.startsWith("../")) {
        return { isError: true, content: [{ type: "text", text: "Document must stay within the knowledge base." }] };
      }
      const sessionDir = "/session";
      const file = path.join(sessionDir, "written_knowledge.json");
      appendJsonArray(file, {
        category: params.category.trim(),
        name: params.name.trim(),
        content: params.content.trim(),
        timestamp: new Date().toISOString()
      });
      return toolResult({ status: "pending", operation: "write_knowledge", target: `${params.category}/${params.name}`,
        instruction: "The document is queued and will be saved after this turn finishes. Do not repeat the write to check it." });
    }
  });

  // 23. Custom Tool: update_component_rules
  pi.registerTool({
    name: "update_component_rules",
    label: "Update Component Rules",
    description: "Record or update architectural rules and guidelines for a component in the knowledge base.",
    parameters: Type.Object({
      component: Type.String({ description: "Component slug (e.g. 'aiogram', 'bubblewrap')" }),
      rules: Type.String({ description: "Complete markdown rules and constraints" })
    }),
    async execute(toolCallId, params) {
      const sessionDir = "/session";
      const file = path.join(sessionDir, "written_knowledge.json");
      appendJsonArray(file, {
        category: "components",
        name: `${params.component.trim()}/rules.md`,
        content: params.rules.trim(),
        timestamp: new Date().toISOString()
      });
      return {
        content: [{ type: "text", text: `Правила компонента "${params.component}" обновлены и будут сохранены в базе знаний.` }]
      };
    }
  });

  // 24. Custom Tool: trigger_review
  pi.registerTool({
    name: "trigger_review",
    label: "Trigger Code Review",
    description: "Submit an independent Code Review operation for a task and return its durable operation id. Inspect the operation to learn the review verdict; submission alone is not a verdict.",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID to review" }),
      instructions: Type.Optional(Type.String({ description: "Optional specific review focus or instructions" })),
      idempotency_key: Type.Optional(Type.String({ description: "Optional stable key; an exact retry reuses the same operation" }))
    }),
    async execute(toolCallId, params) {
      return toolResult(await submitOperation("review", params.task_id.trim(), { instructions: (params.instructions || "").trim() }, params.idempotency_key));
    }
  });

  // 25. Custom Tool: trigger_commit_push
  pi.registerTool({
    name: "trigger_commit_push",
    label: "Trigger Commit and Push",
    description: "Submit a commit/push operation for a task and return its durable operation id. The write is not finished when the tool returns; inspect the operation for the outcome.",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID" }),
      push: Type.Optional(Type.Boolean({ description: "Push to remote after local commit (default: true)" })),
      message: Type.Optional(Type.String({ description: "Custom commit message" })),
      idempotency_key: Type.Optional(Type.String({ description: "Optional stable key; an exact retry reuses the same operation" }))
    }),
    async execute(toolCallId, params) {
      const payload = { push: params.push !== false, message: (params.message || "").trim() };
      return toolResult(await submitOperation("git.commit_push", params.task_id.trim(), payload, params.idempotency_key));
    }
  });

  // 26. Custom Tool: trigger_rollback
  pi.registerTool({
    name: "trigger_rollback",
    label: "Trigger Rollback",
    description: "Submit a rollback operation for a task and return its durable operation id. Inspect the operation for the outcome.",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID to roll back" }),
      idempotency_key: Type.Optional(Type.String({ description: "Optional stable key; an exact retry reuses the same operation" }))
    }),
    async execute(toolCallId, params) {
      return toolResult(await submitOperation("git.rollback", params.task_id.trim(), {}, params.idempotency_key));
    }
  });

  // 27. Custom Tool: trigger_analysis
  pi.registerTool({
    name: "trigger_analysis",
    label: "Trigger Session Analysis",
    description: "Submit a session-analysis operation for a task and return its durable operation id. Inspect the operation for the report; submission alone is not a result.",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID to analyze" }),
      idempotency_key: Type.Optional(Type.String({ description: "Optional stable key; an exact retry reuses the same operation" }))
    }),
    async execute(toolCallId, params) {
      return toolResult(await submitOperation("analysis", params.task_id.trim(), {}, params.idempotency_key));
    }
  });
}

