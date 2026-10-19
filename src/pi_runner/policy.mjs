import fs from "node:fs";
import path from "node:path";
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
      return {
        content: [{ type: "text", text: `Документ "${target}" добавлен к отправке пользователю.` }]
      };
    }
  });

  // 3. Custom Tool: create_agent
  pi.registerTool({
    name: "create_agent",
    label: "Create Agent",
    description: "Create or configure a new specialized agent profile in the knowledge base and system registry.",
    parameters: Type.Object({
      agent_id: Type.String({ description: "Unique slug identifier (e.g. 'skynet-bot-developer')" }),
      name: Type.String({ description: "Human-readable name of the agent" }),
      role: Type.String({ description: "Role category, e.g. 'developer', 'reviewer', 'analyzer', 'secretary'" }),
      tools: Type.Optional(Type.Array(Type.String(), { description: "Allowed tools, e.g. ['read', 'write', 'edit', 'bash']" })),
      skills: Type.Optional(Type.Array(Type.String(), { description: "Optional skill names (e.g. ['codespaces', 'portainer-operations'])" })),
      system_prompt: Type.String({ description: "Complete system instructions / prompt for the agent" })
    }),
    async execute(toolCallId, params) {
      const { agent_id, name, role, tools, skills, system_prompt } = params;
      if (!agent_id || !system_prompt) {
        return { content: [{ type: "text", text: "Error: agent_id and system_prompt are required" }] };
      }
      const sessionDir = "/session";
      const agentsFile = path.join(sessionDir, "created_agents.json");
      appendJsonArray(agentsFile, {
        agent_id: agent_id.trim(),
        name: (name || agent_id).trim(),
        role: (role || "developer").trim(),
        tools: Array.isArray(tools) ? tools : ["read", "write", "edit", "bash"],
        skills: Array.isArray(skills) ? skills : [],
        system_prompt: system_prompt.trim(),
        timestamp: new Date().toISOString()
      });
      return {
        content: [{ type: "text", text: `Профиль агента "${agent_id}" записан и будет сохранён в базе знаний.` }]
      };
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
    try {
      if (fs.existsSync("/session/backend_api.json")) {
        return JSON.parse(fs.readFileSync("/session/backend_api.json", "utf-8"));
      }
    } catch (e) {}
    return {
      backend_url: "http://127.0.0.1:18767",
      api_token: "",
      openviking_url: "http://127.0.0.1:1933",
      openviking_token: "ZGVmYXVsdA.Y29kZXg.NWJmZDlmYWYzYmY4ODhiNmJlNTU1YzVlYjY4ODJiZGZhOGY2ODg2MjQ0ZDg2MDc1NzE4NmFmNGJiZTcwYWYyYw"
    };
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

  // 9. Custom Tool: list_tasks
  pi.registerTool({
    name: "list_tasks",
    label: "List Tasks",
    description: "List tasks from Agent Manager database with optional status and workspace filtering.",
    parameters: Type.Object({
      status: Type.Optional(Type.String({ description: "Filter by status, e.g. 'incoming', 'in_progress', 'ready_to_commit', 'done', 'failed', 'waiting_for_input'" })),
      workspace: Type.Optional(Type.String({ description: "Filter by workspace/project slug" }))
    }),
    async execute(toolCallId, params) {
      const cfg = getBackendConfig();
      try {
        let url = `${cfg.backend_url}/tasks?limit=25`;
        if (params.status) url += `&status=${encodeURIComponent(params.status)}`;
        if (params.workspace) url += `&workspace=${encodeURIComponent(params.workspace)}`;
        const headers = cfg.api_token ? { "Authorization": `Bearer ${cfg.api_token}` } : {};
        const res = await fetch(url, { headers });
        if (!res.ok) {
          return { content: [{ type: "text", text: `HTTP ${res.status}: ${await res.text()}` }] };
        }
        const tasks = await res.json();
        if (!tasks.length) {
          return { content: [{ type: "text", text: "Список задач пуст." }] };
        }
        let out = `### 📋 Задачи (${tasks.length}):\n\n`;
        for (const t of tasks) {
          out += `* **#${t.id}**: **${t.title}** [${t.status}] (workspace: ${t.workspace || '—'})\n`;
        }
        return { content: [{ type: "text", text: out.trim() }] };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка обращения к API задач: ${err.message}` }] };
      }
    }
  });

  // 10. Custom Tool: get_task
  pi.registerTool({
    name: "get_task",
    label: "Get Task",
    description: "Get full details, description, status, and metadata of a task by ID.",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID (e.g. 'task-123' or number)" })
    }),
    async execute(toolCallId, params) {
      const cfg = getBackendConfig();
      try {
        const headers = cfg.api_token ? { "Authorization": `Bearer ${cfg.api_token}` } : {};
        const res = await fetch(`${cfg.backend_url}/tasks/${encodeURIComponent(params.task_id)}`, { headers });
        if (!res.ok) {
          return { content: [{ type: "text", text: `HTTP ${res.status}: ${await res.text()}` }] };
        }
        const t = await res.json();
        const meta = t.metadata || {};
        let out = `### 📌 Задача #${t.id}: ${t.title}\n\n`;
        out += `* **Статус:** \`${t.status}\`\n`;
        out += `* **Пространство:** ${t.workspace || 'default'}\n`;
        out += `* **Описание:**\n${t.description || '*(без описания)*'}\n\n`;
        if (meta.summary) out += `* **Резюме:**\n${meta.summary}\n\n`;
        if (meta.changed_files && meta.changed_files.length) {
          out += `* **Изменённые файлы:**\n` + meta.changed_files.map(f => `  • \`${f}\``).join('\n') + '\n';
        }
        return { content: [{ type: "text", text: out.trim() }] };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка получения задачи: ${err.message}` }] };
      }
    }
  });

  // 11. Custom Tool: create_task
  pi.registerTool({
    name: "create_task",
    label: "Create Task",
    description: "Create a new task in Agent Manager queue.",
    parameters: Type.Object({
      title: Type.String({ description: "Clear, descriptive title of the task" }),
      description: Type.Optional(Type.String({ description: "Detailed specification / instructions for the developer" })),
      workspace: Type.Optional(Type.String({ description: "Workspace slug (e.g. 'scannet', 'agent-manager')" })),
      status: Type.Optional(Type.String({ description: "Initial status: 'incoming' or 'in_progress' (default: 'incoming')" })),
      task_type: Type.Optional(Type.String({ description: "Task category: 'coding', 'devops', 'research'" }))
    }),
    async execute(toolCallId, params) {
      const cfg = getBackendConfig();
      try {
        const headers = {
          "Content-Type": "application/json",
          ...(cfg.api_token ? { "Authorization": `Bearer ${cfg.api_token}` } : {})
        };
        const payload = {
          title: params.title.trim(),
          description: (params.description || "").trim(),
          workspace: params.workspace || null,
          status: params.status || "incoming",
          metadata: {
            task_type: params.task_type || "coding"
          }
        };
        const res = await fetch(`${cfg.backend_url}/tasks`, {
          method: "POST",
          headers,
          body: JSON.stringify(payload)
        });
        if (!res.ok) {
          return { content: [{ type: "text", text: `HTTP ${res.status}: ${await res.text()}` }] };
        }
        const created = await res.json();
        return {
          content: [{
            type: "text",
            text: `✅ Задача #${created.id} "${created.title}" успешно создана в статусе \`${created.status}\` (workspace: ${created.workspace || 'default'}).`
          }]
        };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка создания задачи: ${err.message}` }] };
      }
    }
  });

  // 12. Custom Tool: update_task
  pi.registerTool({
    name: "update_task",
    label: "Update Task",
    description: "Update task description, title, or status (e.g. move to 'in_progress', 'cancelled', 'incoming').",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID" }),
      status: Type.Optional(Type.String({ description: "New status" })),
      title: Type.Optional(Type.String({ description: "Updated title" })),
      description: Type.Optional(Type.String({ description: "Updated description" }))
    }),
    async execute(toolCallId, params) {
      const cfg = getBackendConfig();
      try {
        const headers = {
          "Content-Type": "application/json",
          ...(cfg.api_token ? { "Authorization": `Bearer ${cfg.api_token}` } : {})
        };
        const patchData = {};
        if (params.status) patchData.status = params.status;
        if (params.title) patchData.title = params.title;
        if (params.description !== undefined) patchData.description = params.description;

        const res = await fetch(`${cfg.backend_url}/tasks/${encodeURIComponent(params.task_id)}`, {
          method: "PATCH",
          headers,
          body: JSON.stringify(patchData)
        });
        if (!res.ok) {
          return { content: [{ type: "text", text: `HTTP ${res.status}: ${await res.text()}` }] };
        }
        const updated = await res.json();
        return {
          content: [{
            type: "text",
            text: `✅ Задача #${updated.id} обновлена: статус \`${updated.status}\`, название "${updated.title}".`
          }]
        };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка обновления задачи: ${err.message}` }] };
      }
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

  // 15. Custom Tool: list_agents
  pi.registerTool({
    name: "list_agents",
    label: "List Agents",
    description: "List all registered agent profiles in the system.",
    parameters: Type.Object({}),
    async execute(toolCallId, params) {
      const cfg = getBackendConfig();
      try {
        const headers = cfg.api_token ? { "Authorization": `Bearer ${cfg.api_token}` } : {};
        const res = await fetch(`${cfg.backend_url}/agents`, { headers });
        if (!res.ok) return { content: [{ type: "text", text: `HTTP ${res.status}: ${await res.text()}` }] };
        const agents = await res.json();
        // Include agents created in current session
        try {
          if (fs.existsSync("/session/created_agents.json")) {
            const pending = JSON.parse(fs.readFileSync("/session/created_agents.json", "utf-8")) || [];
            const existingIds = new Set(agents.map(a => a.agent_id));
            for (const p of pending) {
              if (p.agent_id && !existingIds.has(p.agent_id)) {
                agents.push({
                  agent_id: p.agent_id,
                  name: p.name || p.agent_id,
                  role: p.role || "worker",
                  revision: "новая (сохраняется)",
                  runner_config: { tools: p.tools || [], skills: p.skills || [] },
                  system_prompt: p.system_prompt || ""
                });
              }
            }
          }
        } catch (e) {}

        if (!agents.length) return { content: [{ type: "text", text: "Агенты не найдены." }] };
        let out = `### 🤖 Зарегистрированные агенты (${agents.length}):\n\n`;
        for (const ag of agents) {
          out += `* **\`${ag.agent_id}\`** — **${ag.name}** [роль: \`${ag.role}\`, ревизия: ${ag.revision}]\n`;
        }
        return { content: [{ type: "text", text: out.trim() }] };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка получения списка агентов: ${err.message}` }] };
      }
    }
  });

  // 16. Custom Tool: get_agent
  pi.registerTool({
    name: "get_agent",
    label: "Get Agent Profile",
    description: "Get complete configuration and system prompt of an agent profile.",
    parameters: Type.Object({
      agent_id: Type.String({ description: "Unique agent identifier" })
    }),
    async execute(toolCallId, params) {
      const cfg = getBackendConfig();
      const targetId = (params.agent_id || "").trim();
      try {
        const headers = cfg.api_token ? { "Authorization": `Bearer ${cfg.api_token}` } : {};
        const res = await fetch(`${cfg.backend_url}/agents/${encodeURIComponent(targetId)}`, { headers });
        let ag = null;
        if (res.ok) {
          ag = await res.json();
        } else {
          // Check if agent was created in current session
          try {
            if (fs.existsSync("/session/created_agents.json")) {
              const pending = JSON.parse(fs.readFileSync("/session/created_agents.json", "utf-8")) || [];
              const found = pending.find(p => p.agent_id === targetId);
              if (found) {
                ag = {
                  agent_id: found.agent_id,
                  name: found.name || found.agent_id,
                  role: found.role || "worker",
                  revision: "новая (сохраняется в текущем ходе)",
                  runner_config: { tools: found.tools || [], skills: found.skills || [] },
                  system_prompt: found.system_prompt || ""
                };
              }
            }
          } catch (e) {}
        }
        if (!ag) return { content: [{ type: "text", text: `HTTP ${res.status}: агент "${targetId}" не найден.` }] };
        const rcfg = ag.runner_config || {};
        let out = `### 🤖 Профиль агента: \`${ag.agent_id}\`\n\n`;
        out += `* **Имя:** ${ag.name}\n`;
        out += `* **Роль:** \`${ag.role}\` (ревизия: ${ag.revision})\n`;
        out += `* **Инструменты:** \`${JSON.stringify(rcfg.tools || [])}\`\n`;
        out += `* **Скилы:** \`${JSON.stringify(rcfg.skills || ag.skills || [])}\`\n\n`;
        out += `#### Системный промпт:\n\`\`\`markdown\n${ag.system_prompt}\n\`\`\`\n`;
        return { content: [{ type: "text", text: out.trim() }] };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка получения агента: ${err.message}` }] };
      }
    }
  });

  // 17. Custom Tool: update_agent
  pi.registerTool({
    name: "update_agent",
    label: "Update Agent",
    description: "Update prompt, tools, skills, or settings of an existing agent profile.",
    parameters: Type.Object({
      agent_id: Type.String({ description: "Agent ID to update" }),
      system_prompt: Type.Optional(Type.String({ description: "Updated system prompt" })),
      tools: Type.Optional(Type.Array(Type.String(), { description: "Allowed tools" })),
      skills: Type.Optional(Type.Array(Type.String(), { description: "Skills list" })),
      name: Type.Optional(Type.String({ description: "Display name" })),
      role: Type.Optional(Type.String({ description: "Role category" }))
    }),
    async execute(toolCallId, params) {
      const cfg = getBackendConfig();
      try {
        const headers = {
          "Content-Type": "application/json",
          ...(cfg.api_token ? { "Authorization": `Bearer ${cfg.api_token}` } : {})
        };
        // 1. Get current agent to find revision
        const getRes = await fetch(`${cfg.backend_url}/agents/${encodeURIComponent(params.agent_id)}`, { headers });
        if (!getRes.ok) return { content: [{ type: "text", text: `HTTP ${getRes.status}: ${await getRes.text()}` }] };
        const current = await getRes.json();
        const rcfg = { ...(current.runner_config || {}) };
        if (params.tools) rcfg.tools = params.tools;
        if (params.skills) rcfg.skills = params.skills;

        const updatePayload = {
          expected_revision: current.revision,
          name: params.name || current.name,
          role: params.role || current.role,
          system_prompt: params.system_prompt || current.system_prompt,
          runner_config: rcfg,
          skills: params.skills ? params.skills.map(s => ({ skill: s })) : current.skills
        };

        const putRes = await fetch(`${cfg.backend_url}/agents/${encodeURIComponent(params.agent_id)}`, {
          method: "PUT",
          headers,
          body: JSON.stringify(updatePayload)
        });
        if (!putRes.ok) return { content: [{ type: "text", text: `HTTP ${putRes.status}: ${await putRes.text()}` }] };
        const updated = await putRes.json();
        return {
          content: [{
            type: "text",
            text: `✅ Агент \`${updated.agent_id}\` успешно обновлён (новая ревизия: ${updated.revision}).`
          }]
        };
      } catch (err) {
        return { content: [{ type: "text", text: `Ошибка обновления агента: ${err.message}` }] };
      }
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
      const sessionDir = "/session";
      const file = path.join(sessionDir, "written_knowledge.json");
      appendJsonArray(file, {
        category: params.category.trim(),
        name: params.name.trim(),
        content: params.content.trim(),
        timestamp: new Date().toISOString()
      });
      return {
        content: [{ type: "text", text: `Документ "${params.category}/${params.name}" зафиксирован и будет записан в базу знаний.` }]
      };
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
    description: "Trigger an independent Code Review cycle for a specific task.",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID to review" }),
      instructions: Type.Optional(Type.String({ description: "Optional specific review focus or instructions" }))
    }),
    async execute(toolCallId, params) {
      appendJsonArray("/session/actions.json", {
        action: "trigger_review",
        task_id: params.task_id.trim(),
        instructions: (params.instructions || "").trim(),
        timestamp: new Date().toISOString()
      });
      return {
        content: [{ type: "text", text: `Запущен раунд Code Review для задачи #${params.task_id}.` }]
      };
    }
  });

  // 25. Custom Tool: trigger_commit_push
  pi.registerTool({
    name: "trigger_commit_push",
    label: "Trigger Commit and Push",
    description: "Commit task changes and push to remote git repository with CI monitoring.",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID" }),
      push: Type.Optional(Type.Boolean({ description: "Push to remote after local commit (default: true)" })),
      message: Type.Optional(Type.String({ description: "Custom commit message" }))
    }),
    async execute(toolCallId, params) {
      appendJsonArray("/session/actions.json", {
        action: "trigger_commit_push",
        task_id: params.task_id.trim(),
        push: params.push !== false,
        message: (params.message || "").trim(),
        timestamp: new Date().toISOString()
      });
      return {
        content: [{ type: "text", text: `Запущен процесс фиксации и отправки изменений для задачи #${params.task_id}.` }]
      };
    }
  });

  // 26. Custom Tool: trigger_rollback
  pi.registerTool({
    name: "trigger_rollback",
    label: "Trigger Rollback",
    description: "Discard working tree changes and cancel task.",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID to roll back" })
    }),
    async execute(toolCallId, params) {
      appendJsonArray("/session/actions.json", {
        action: "trigger_rollback",
        task_id: params.task_id.trim(),
        timestamp: new Date().toISOString()
      });
      return {
        content: [{ type: "text", text: `Запущен откат изменений по задаче #${params.task_id}.` }]
      };
    }
  });

  // 27. Custom Tool: trigger_analysis
  pi.registerTool({
    name: "trigger_analysis",
    label: "Trigger Session Analysis",
    description: "Trigger deep session and log analysis to extract learnings for the knowledge base.",
    parameters: Type.Object({
      task_id: Type.String({ description: "Task ID to analyze" })
    }),
    async execute(toolCallId, params) {
      appendJsonArray("/session/actions.json", {
        action: "trigger_analysis",
        task_id: params.task_id.trim(),
        timestamp: new Date().toISOString()
      });
      return {
        content: [{ type: "text", text: `Запущен глубокий анализ сессии по задаче #${params.task_id}.` }]
      };
    }
  });
}

