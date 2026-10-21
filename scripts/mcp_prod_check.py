"""Live MCP acceptance probe: secretary /ask flow and artifact download.

Runs against a real Kuzya instance (default http://127.0.0.1:18767, override
with KUZYA_BASE): creates a real conversation and calls the real secretary,
so it spends model turns — not for CI. The Bearer token is read from the
service env file into memory only and is never printed or logged.

Usage: ~/.kuzya/venv/bin/python scripts/mcp_prod_check.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re
import sys

import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

ENV_FILE = pathlib.Path.home() / ".kuzya/kuzya.env"
BASE = os.environ.get("KUZYA_BASE", "http://127.0.0.1:18767")


def load_token() -> str:
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        m = re.match(r'^(?:export\s+)?KUZYA_API_TOKENS?\s*=\s*"?([^"#]+)', line)
        if m:
            token = m.group(1).strip().strip('"').split(",")[0].strip()
            if token:
                return token
    raise SystemExit("no API token found in env file")


def by_endpoint(tools, snippet: str):
    matches = [t for t in tools if snippet in t.description]
    if len(matches) != 1:
        raise SystemExit(f"expected one tool for {snippet!r}, got {[t.name for t in matches]}")
    return matches[0]


def preview(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


async def wait_for_secretary_reply(
    session: ClientSession, messages_tool, conversation_id: str, known: int, deadline_s: float = 90.0
):
    """Poll conversation messages until a new assistant message appears."""
    for _ in range(int(deadline_s / 5)):
        await asyncio.sleep(5)
        result = await session.call_tool(
            messages_tool.name, {"path": {"conversation_id": conversation_id}}
        )
        messages = result.structured_content.get("items", [])
        fresh = [m for m in messages[len(known):] if m.get("role") == "assistant"]
        if fresh:
            return messages, fresh
    return None, []


def print_results(results):
    print("\n=== RESULTS ===", flush=True)
    failed = 0
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)
        failed += 0 if ok else 1
    return failed


def log(msg: str) -> None:
    import datetime
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


async def main() -> None:
    token = load_token()
    results: list[tuple[str, bool, str]] = []

    async with httpx.AsyncClient(
        base_url=BASE,
        headers={"Authorization": f"Bearer {token}"},
        timeout=httpx.Timeout(120.0),
    ) as http:
        async with streamable_http_client(f"{BASE}/mcp", http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                results.append(("handshake + tools/list", True, f"{len(tools)} tools"))
                log(f"handshake ok, {len(tools)} tools")
                ask_tool = by_endpoint(tools, "POST /ask\n")
                messages_tool = by_endpoint(
                    tools, "GET /conversations/{conversation_id}/messages\n"
                )
                list_tasks_tool = by_endpoint(tools, "GET /tasks\n")
                artifacts_tool = by_endpoint(tools, "GET /tasks/{task_id}/artifacts\n")
                download_tool = by_endpoint(
                    tools, "GET /tasks/{task_id}/artifacts/{artifact_name}\n"
                )

                log("asking secretary about tasks (may take up to ~50s)…")
                # --- 1. ask the secretary about tasks --------------------
                res = await session.call_tool(
                    ask_tool.name,
                    {"body": {
                        "message": "Какие задачи сейчас есть в системе? Перечисли кратко.",
                        "timeout_seconds": 50,
                    }},
                )
                assert not res.is_error, res.content[0].text
                data = res.structured_content
                conversation_id = data["conversation_id"]
                reply1 = data.get("reply")
                status1 = data["status"]

                if status1 == "completed" and reply1:
                    results.append(("ask #1: reply received", True, preview(reply1)))
                else:
                    listing = await session.call_tool(
                        messages_tool.name,
                        {"path": {"conversation_id": conversation_id}},
                    )
                    known = len(listing.structured_content.get("items", []))
                    messages, fresh = await wait_for_secretary_reply(
                        session, messages_tool, conversation_id, known
                    )
                    if fresh:
                        results.append((
                            "ask #1: reply after polling",
                            True,
                            f"status={status1}, then: " + preview(fresh[-1]["text"]),
                        ))
                    else:
                        results.append(("ask #1: reply received", False, f"status={status1}, no assistant message in 90s"))

                log("continuing conversation…")
                # --- 2. continue the same conversation -------------------
                res2 = await session.call_tool(
                    ask_tool.name,
                    {"body": {
                        "message": "Уточни, пожалуйста: какая из них самая свежая и что в ней нужно сделать?",
                        "conversation_id": conversation_id,
                        "timeout_seconds": 50,
                    }},
                )
                assert not res2.is_error, res2.content[0].text
                data2 = res2.structured_content
                if data2["conversation_id"] != conversation_id:
                    results.append(("ask #2: same conversation", False,
                                    f"new id {data2['conversation_id']}"))
                else:
                    results.append(("ask #2: same conversation", True, conversation_id))
                reply2 = data2.get("reply")
                if data2["status"] == "completed" and reply2:
                    results.append(("ask #2: reply received", True, preview(reply2)))
                else:
                    listing = await session.call_tool(
                        messages_tool.name,
                        {"path": {"conversation_id": conversation_id}},
                    )
                    known = len(listing.structured_content.get("items", []))
                    messages, fresh = await wait_for_secretary_reply(
                        session, messages_tool, conversation_id, known
                    )
                    if fresh:
                        results.append(("ask #2: reply after polling", True, preview(fresh[-1]["text"])))
                    else:
                        results.append(("ask #2: reply received", False, f"status={data2['status']}"))

                # --- 3. verify conversation transcript --------------------
                listing = await session.call_tool(
                    messages_tool.name, {"path": {"conversation_id": conversation_id}}
                )
                items = listing.structured_content.get("items", [])
                roles = [m["role"] for m in items]
                assistant_count = roles.count("assistant")
                ok = assistant_count >= 2 and roles[0] == "user"
                results.append((
                    "conversation transcript",
                    ok,
                    f"{len(items)} messages, roles={roles[:8]}{'…' if len(roles) > 8 else ''}",
                ))

                log("scanning recent tasks for attachments…")
                # --- 4. find a task with attachments ----------------------
                tasks_res = await session.call_tool(list_tasks_tool.name, {"query": {"limit": 100}})
                tasks = tasks_res.structured_content.get("items", [])
                found_task = None
                found_artifact = None
                for task in tasks:
                    arts = await session.call_tool(
                        artifacts_tool.name, {"path": {"task_id": task["id"]}}
                    )
                    art_items = arts.structured_content.get("items", arts.structured_content.get("artifacts", []))
                    if isinstance(art_items, list) and art_items:
                        found_task = task
                        found_artifact = art_items[0]
                        break
                if not found_task:
                    results.append(("attachment found", False, f"no artifacts in the {len(tasks)} most recent tasks"))
                    print_results(results)
                    return
                results.append((
                    "attachment found",
                    True,
                    f"task {found_task.get('number', '?')} «{preview(found_task['title'], 40)}» -> {found_artifact.get('name')}",
                ))

                # --- 5. download it through the MCP tool -------------------
                dl = await session.call_tool(
                    download_tool.name,
                    {"path": {
                        "task_id": found_task["id"],
                        "artifact_name": found_artifact["name"],
                    }},
                )
                text = dl.content[0].text if dl.content else ""
                structured = dl.structured_content or {}
                payload = structured.get("text", "")
                ok = (not dl.is_error) and len(payload) > 0
                results.append((
                    "artifact downloaded via MCP",
                    ok,
                    f"is_error={dl.is_error}, bytes~{len(payload)}, head={preview(payload, 120)!r}",
                ))

    failed = print_results(results)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
