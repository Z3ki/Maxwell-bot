/**
 * Maxwell Brain Extension for Pi (the actual Discord bot brain)
 *
 * This is the Pi-side "brain" tools + hooks.
 * Pi (via --mode rpc or SDK) becomes the core agent loop for the Maxwell self-bot.
 *
 * - Custom tools here are callable by Pi's LLM (the brain decides when to use).
 * - Port of Maxwell bot_tools.py + new Discord actions.
 * - CRITICAL: maxwell_create_site must produce EXACT same output as Python CreateSiteTool
 *   (public/bot/<slug>/index.html + permissive CSP meta) so Caddy (examples/Caddyfile.example)
 *   serves it identically. Volume mounted in docker/pi-debian.Dockerfile.
 * - Uses same providers from .env (NVIDIA_API_KEY etc). Pi discovers natively.
 *
 * How bridge uses it (see /root/maxwell/pi_bridge.py):
 * - Python thin client serializes Discord msg -> Pi prompt (via RPC).
 * - Pi reasons, may call these tools.
 * - For Discord-mutating tools (send_message, react, etc.): tool can return a structured
 *   "discord_action" that the Python bridge executes via discord.py (with admin gates).
 * - Pure tools (web, image, create_site, shell, yt, fetch): execute fully here (or delegate to python for parity).
 *
 * Install: auto-discovered from .pi/extensions/ or load with `pi -e .pi/extensions/maxwell-brain/index.ts`
 * Reload with /reload in Pi.
 *
 * See:
 *   - PROGRESS_PI_BRAIN_PLAN.md
 *   - AGENTS.md (use maxwell_* , run pi in /workspace, parity required)
 *   - .pi/extensions/maxwell-tools/index.ts (dev harness ports; reuse where possible)
 *   - bot_tools.py for full original specs
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { mkdirSync, writeFileSync, existsSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { execSync } from "node:child_process";

// Explicit paths for parity
const DEFAULT_SITE_DIR = process.env.MAXWELL_SITE_DIR || "public/bot";
const DEFAULT_PUBLIC_BASE = (process.env.MAXWELL_PUBLIC_BASE_URL || "https://maxwell.example.com").replace(/\/$/, "") + "/bot";

function safeSlug(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/, "").slice(0, 30);
}

export default function maxwellBrain(pi: ExtensionAPI) {
  // NOTE on website creator (CRITICAL for Caddy):
  // Use maxwell_create_site + maxwell_list_sites from sibling .pi/extensions/maxwell-tools/
  // (exact parity with Python bot_tools.py:CreateSiteTool).
  // Load BOTH when running Pi as brain:
  //   pi -e .pi/extensions/maxwell-tools/index.ts -e .pi/extensions/maxwell-brain/index.ts
  // maxwell_create_site MUST write public/bot/<slug>/index.html + the permissive CSP meta.
  // This guarantees sites work when served by Caddy (examples/Caddyfile.example) from Docker volume.
  // See docker/pi-debian.Dockerfile for MAXWELL_SITE_DIR=/workspace/public/bot .
  // Do not change CSP or write paths without syncing both sides.

  // Discord action stubs: the brain calls these. Bridge (Python) will interpret the result
  // and perform real discord.py actions (send, react, etc), feeding back the outcome.
  // This keeps discord.py in thin Python layer (no need for Node discord lib).
  pi.registerTool({
    name: "discord_send_message",
    label: "Send Discord Message",
    description: "Send a message to the current (or specified) Discord channel. Use for final replies or proactive posts. Returns structured action for bridge to execute.",
    parameters: Type.Object({
      content: Type.String({ description: "Message content (Discord markdown ok)" }),
      channel_id: Type.Optional(Type.String()),
      // reply_to etc in future
    }),
    async execute(_id, params) {
      // Return a marker the Python bridge recognizes as "do this Discord op"
      // In full bridge impl: on tool_execution, detect name, perform via Discord client, return result string to Pi.
      return {
        content: [{ type: "text", text: `ACTION:discord_send_message:${JSON.stringify(params)}` }],
        details: { action: "discord_send_message", params, note: "Executed by Python bridge for discord.py fidelity + gates" },
      };
    },
  });

  // Example other ports as brain tools (can delegate to maxwell-tools or reimplement)
  // For now, document reuse. Real porting will import/ exec or duplicate minimal logic.
  // e.g. image gen, web_search can stay as maxwell_* or alias here.

  pi.registerTool({
    name: "discord_react",
    label: "React to Message",
    description: "Add emoji reaction. Bridge executes via discord.py.",
    parameters: Type.Object({
      emoji: Type.String(),
      message_id: Type.Optional(Type.String()),
      channel_id: Type.Optional(Type.String()),
    }),
    async execute(_id, params) {
      return {
        content: [{ type: "text", text: `ACTION:discord_react:${JSON.stringify(params)}` }],
        details: { action: "discord_react", params },
      };
    },
  });

  // Shell / fetch / etc can be re-registered here with maxwell names or use built-in bash + custom.
  // For shell, prefer the existing sandbox approach from Python (ShellTool).

  // Hook example: on tool calls we can log or gate (like admin checks later via context passed in prompt)
  pi.on("tool_execution_start", async (e, ctx) => {
    if (["discord_send_message", "discord_react"].includes(e.toolName)) {
      // In real: check if ctx has admin flag (passed via prompt metadata or separate state tool)
      ctx.ui?.notify?.(`Brain tool: ${e.toolName}`, "info");
    }
  });

  // Personality / system notes can come from AGENTS.md + custom prompt when starting Pi.
  // Pi session carries long term state for REM/autonomy.

  pi.on("session_start", async (_e, ctx) => {
    const model = process.env.OLLAMA_MODEL || "xiaomi/mimo-v2.5 (from your PM2 .env)";
    ctx.ui?.notify?.(`Maxwell BRAIN extension loaded. Pi is now the core agent. Using same model as PM2 maxwell-bot: ${model}. Use maxwell_* + discord_* tools. Sites respect MAXWELL_SITE_DIR (${process.env.MAXWELL_SITE_DIR || "public/bot"}).`, "info");
  });

  // Register helpful command inside Pi sessions
  pi.registerCommand("maxwell-brain-status", {
    description: "Status of Pi-as-Maxwell-brain integration",
    handler: async (_args, ctx) => {
      const siteBase = DEFAULT_PUBLIC_BASE;
      ctx.ui.notify(`Pi brain active. Site dir: ${DEFAULT_SITE_DIR} -> ${siteBase}. Providers from env (NVIDIA/OPENROUTER etc). See pi_bridge.py + PROGRESS_PI_BRAIN_PLAN.md`, "info");
    },
  });
}
