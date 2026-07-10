/**
 * Maxwell Tools - Pi Extension
 * Ports key Maxwell bot tools/systems into Pi coding agent for use in this project.
 * 
 * This makes Pi able to do web_search, safe fetch, image generation (using same providers),
 * YouTube analysis, local site creation (mirrors create_site for website creator testing),
 * and other Maxwell capabilities while developing the Python bot.
 *
 * Install / use:
 *   - Discovered automatically if in .pi/extensions/ (after project trust) or ~/.pi/agent/extensions/
 *   - Or run: pi -e .pi/extensions/maxwell-tools/index.ts
 *   - Reload in Pi with /reload
 *
 * Providers: Uses the same env/API keys as Maxwell (.env):
 *   - NVIDIA_API_KEY for image gen (NIM/flux)
 *   - OPENROUTER_API_KEY or OLLAMA_* mapped for search/LLM if needed
 *   - GPT_IMAGE_* for alt image
 * Pi natively supports these keys (see pi.dev/docs/providers). No duplication.
 *
 * Website creator: create_local_site writes HTML to public/bot/<slug> exactly like
 * CreateSiteTool so host Caddy (examples/Caddyfile.example) continues serving it.
 * Works inside Docker volume mounts.
 *
 * Future ports: autonomy logic, REM as Pi skills; discord bridge via API if running;
 * full tool parity via python exec bridge if desired.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { execSync } from "node:child_process";
import { mkdirSync, writeFileSync, existsSync } from "node:fs";
import { join, resolve } from "node:path";
import https from "node:https";

const DEFAULT_SITE_DIR = process.env.MAXWELL_SITE_DIR || "public/bot";
const DEFAULT_PUBLIC_BASE = (process.env.MAXWELL_PUBLIC_BASE_URL || "https://maxwell.example.com").replace(/\/$/, "") + "/bot";

function safeSlug(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9-]/g, "-").replace(/-+/g, "-").replace(/^-|-$/g, "").slice(0, 30);
}

async function httpsPostJson(url: string, data: any, headers: Record<string, string> = {}): Promise<any> {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(data);
    const u = new URL(url);
    const req = https.request({
      hostname: u.hostname,
      port: u.port || 443,
      path: u.pathname + u.search,
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Content-Length": Buffer.byteLength(body),
        ...headers,
      },
    }, (res) => {
      let chunks = "";
      res.on("data", (d) => (chunks += d));
      res.on("end", () => {
        try { resolve(JSON.parse(chunks || "{}")); } catch (e) { resolve({ raw: chunks }); }
      });
    });
    req.on("error", reject);
    req.write(body);
    req.end();
  });
}

export default function maxwellTools(pi: ExtensionAPI) {
  // Port of web_search (uses ddgs in Python, here simple or exec to python for parity)
  pi.registerTool({
    name: "maxwell_web_search",
    label: "Maxwell Web Search",
    description: "Web search using Maxwell's ddgs backend (or fallback). Returns results with titles, urls, snippets. Same as bot web_search tool.",
    parameters: Type.Object({
      query: Type.String({ description: "Search query" }),
      max_results: Type.Optional(Type.Integer({ minimum: 1, maximum: 20, default: 8 })),
    }),
    async execute(_id, params) {
      const q = params.query;
      const n = params.max_results || 8;
      try {
        // Prefer calling into installed python ddgs for exact parity with Maxwell
        const cmd = `python3 -c "
from ddgs import DDGS
import json
with DDGS() as ddgs:
    res = list(ddgs.text('${q.replace(/'/g, "\\'")}', max_results=${n}))
print(json.dumps(res))
" `;
        const out = execSync(cmd, { encoding: "utf8", stdio: ["pipe", "pipe", "pipe"], timeout: 30000 });
        const results = JSON.parse(out || "[]");
        return {
          content: [{ type: "text", text: JSON.stringify(results, null, 2) }],
          details: { count: results.length },
        };
      } catch (e: any) {
        return { content: [{ type: "text", text: `Search error (fallback note): ${e.message}. Install python ddgs or use bash 'ddgs ...'` }], details: { error: true } };
      }
    },
  });

  // Port of fetch_url with basic SSRF awareness (Pi bash can do more; this adds safety note + simple get)
  pi.registerTool({
    name: "maxwell_fetch_url",
    label: "Maxwell Fetch URL",
    description: "Safe-ish URL fetch mirroring Maxwell fetch_url tool. Blocks obvious private IPs, caps size. For full, use bash + curl with care.",
    parameters: Type.Object({
      url: Type.String({ description: "HTTP/HTTPS URL" }),
      max_bytes: Type.Optional(Type.Integer({ default: 200000 })),
    }),
    async execute(_id, params) {
      const urlStr = params.url;
      try {
        const u = new URL(urlStr);
        if (!["http:", "https:"].includes(u.protocol)) throw new Error("Only http/https");
        const host = u.hostname;
        if (/^(127\.|10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[0-1])\.|localhost|::1)/i.test(host)) {
          throw new Error("Blocked private/loopback for safety (matches Maxwell _SafeResolver)");
        }
        const res = await fetch(urlStr, { redirect: "manual", signal: AbortSignal.timeout(15000) });
        const text = await res.text();
        const capped = text.slice(0, params.max_bytes || 200000);
        return { content: [{ type: "text", text: capped }], details: { status: res.status, url: urlStr } };
      } catch (e: any) {
        return { content: [{ type: "text", text: `Fetch failed: ${e.message}` }], details: { error: true } };
      }
    },
  });

  // Image generation port (Pollinations + NVIDIA NIM using same keys as Maxwell .env)
  pi.registerTool({
    name: "maxwell_image_gen",
    label: "Maxwell Image Generator",
    description: "Generate image using Maxwell providers (Pollinations default or NVIDIA if NVIDIA_API_KEY present). Returns URL or base64 note.",
    parameters: Type.Object({
      prompt: Type.String({ description: "Image prompt" }),
      provider: Type.Optional(Type.String({ enum: ["pollinations", "nvidia", "gpt"], default: "pollinations" })),
    }),
    async execute(_id, params) {
      const prompt = params.prompt;
      const prov = params.provider || "pollinations";
      const nvidiaKey = process.env.NVIDIA_API_KEY;
      try {
        if (prov === "nvidia" && nvidiaKey) {
          // Simplified NVIDIA NIM call (flux etc). Real Maxwell uses specific endpoint.
          const body = { prompt, num_inference_steps: 20 };
          const result: any = await httpsPostJson(
            process.env.NVIDIA_IMAGE_URL || "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.1-dev",
            body,
            { Authorization: `Bearer ${nvidiaKey}` }
          );
          return { content: [{ type: "text", text: `NVIDIA image result (inspect response): ${JSON.stringify(result).slice(0,500)}` }], details: { provider: "nvidia" } };
        }
        // Default Pollinations (free, no key, same as Maxwell fallback)
        const url = `https://image.pollinations.ai/prompt/${encodeURIComponent(prompt)}?model=${process.env.POLLINATIONS_MODEL || "flux"}&safe=false`;
        return {
          content: [{ type: "text", text: `Image URL (open or download): ${url}\nPrompt: ${prompt}` }],
          details: { url, provider: "pollinations" },
        };
      } catch (e: any) {
        return { content: [{ type: "text", text: `Image gen error: ${e.message}` }], details: { error: true } };
      }
    },
  });

  // YouTube port stub (full uses yt-dlp in Maxwell). Delegates to bash for power.
  pi.registerTool({
    name: "maxwell_youtube",
    label: "Maxwell YouTube",
    description: "Analyze YouTube: title, transcript (timedtext or yt-dlp), frames if timestamps. Delegates to system yt-dlp for parity with Maxwell.",
    parameters: Type.Object({
      url: Type.String(),
      timestamps: Type.Optional(Type.String({ description: "e.g. 0:10,1:23" })),
    }),
    async execute(_id, params) {
      const { url, timestamps } = params;
      try {
        const cmd = `yt-dlp --print "%(title)s|%(channel)s|%(duration)s" --write-auto-sub --skip-download "${url}" 2>/dev/null | head -5`;
        const meta = execSync(cmd, { encoding: "utf8", timeout: 45000 }).trim();
        let frames = "";
        if (timestamps) {
          frames = `\n(Frames for timestamps would be extracted via yt-dlp web_embedded + ffmpeg here; use bash for now.)`;
        }
        return { content: [{ type: "text", text: `YT meta: ${meta}${frames}\nFull transcript via yt-dlp --write-subs in your shell.` }], details: { url } };
      } catch (e: any) {
        return { content: [{ type: "text", text: `YT error: ${e.message}. Ensure yt-dlp installed.` }], details: { error: true } };
      }
    },
  });

  // Website creator port: mirrors CreateSiteTool exactly for testing in Pi / Docker.
  // Writes to same dir so Caddy serves it. Full HTML body as-is + permissive CSP.
  pi.registerTool({
    name: "maxwell_create_site",
    label: "Maxwell Create Site (website creator)",
    description: "Create a temporary site exactly like Maxwell's create_site tool. Writes full HTML to MAXWELL_SITE_DIR/<slug>/index.html. Works in Docker; served by your Caddy at $MAXWELL_PUBLIC_BASE_URL/bot/<slug>/.",
    parameters: Type.Object({
      name: Type.String({ description: "slug (a-z0-9-)" }),
      title: Type.String(),
      body: Type.String({ description: "Complete HTML document (or body content)" }),
      encoding: Type.Optional(Type.String({ default: "text" })),
    }),
    async execute(_id, params) {
      const slug = safeSlug(params.name);
      if (!slug || slug.length < 2) return { content: [{ type: "text", text: "Invalid slug" }], details: { error: true } };
      const baseDir = resolve(DEFAULT_SITE_DIR);
      const siteDir = join(baseDir, slug);
      mkdirSync(siteDir, { recursive: true });
      let body = params.body;
      if (params.encoding === "base64" || params.encoding === "b64") {
        body = Buffer.from(body, "base64").toString("utf8");
      }
      // Inject permissive CSP like the Python CreateSiteTool
      const csp = '<meta http-equiv="Content-Security-Policy" content="default-src https: data: blob:; img-src https: data: blob:; style-src \'unsafe-inline\' https:; script-src \'unsafe-inline\' \'unsafe-eval\' https:; font-src https: data:; connect-src https:; media-src https: data: blob:;">';
      if (/<head/i.test(body)) {
        body = body.replace(/<head([^>]*)>/i, `<head$1>\n${csp}`);
      } else if (/<html/i.test(body)) {
        body = body.replace(/<html([^>]*)>/i, `<html$1>\n<head>${csp}</head>`);
      } else {
        body = `<head>${csp}</head>\n${body}`;
      }
      const indexPath = join(siteDir, "index.html");
      writeFileSync(indexPath, body, "utf8");
      const url = `${DEFAULT_PUBLIC_BASE}/${slug}/`;
      return {
        content: [{ type: "text", text: `Site created (Pi port of create_site): ${url}\nEdit ${indexPath} directly or re-run tool.` }],
        details: { url, path: indexPath },
      };
    },
  });

  // List sites (port)
  pi.registerTool({
    name: "maxwell_list_sites",
    label: "Maxwell List Sites",
    description: "List sites created under MAXWELL_SITE_DIR (see maxwell_create_site).",
    parameters: Type.Object({}),
    async execute() {
      const base = resolve(DEFAULT_SITE_DIR);
      if (!existsSync(base)) return { content: [{ type: "text", text: "No sites dir" }] };
      const dirs = execSync(`ls -1 "${base}" 2>/dev/null || true`, { encoding: "utf8" }).trim().split("\n").filter(Boolean);
      return { content: [{ type: "text", text: `Sites: ${dirs.join(", ") || "(none)"}` }], details: { sites: dirs } };
    },
  });

  // Register a command example: /maxwell-status
  pi.registerCommand("maxwell-status", {
    description: "Show Maxwell + Pi integration status and provider hints.",
    handler: async (_args, ctx) => {
      const keys = ["NVIDIA_API_KEY", "OPENROUTER_API_KEY", "OLLAMA_API_KEY", "MAXWELL_PUBLIC_BASE_URL"].map(k => `${k}=${process.env[k] ? "set" : "missing"}`).join(" ");
      ctx.ui.notify(`Maxwell tools loaded. Providers status: ${keys}. Use maxwell_* tools. Site base: ${DEFAULT_PUBLIC_BASE}`, "info");
    },
  });

  pi.on("session_start", async (_e, ctx) => {
    ctx.ui.notify("Maxwell tools extension active (web, fetch, image, yt, create_site, ...). Same providers as .env", "info");
  });

  // Tip for future: port more by registering tools that exec python bot_tools or call the running API.
  // Autonomy/REM can be turned into Pi skills (.pi/skills/*.md) or prompt templates.
}
