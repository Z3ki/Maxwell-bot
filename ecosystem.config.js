// PM2 process definitions for Maxwell.
//
// ENV IS LOADED BY PYTHON, NOT HERE. config.py and api/storage.py both call
// load_dotenv(override=True) on every process start, so the local .env file
// is the single source of truth — edit .env, `pm2 restart`, done. No env
// merging in this file (PM2 caches env from first start and --update-env
// does NOT re-read .env, which used to pin stale values like the old
// OLLAMA_FALLBACK_MODEL forever).
//
// Only runtime flags that must exist before the interpreter boots live here:
// PYTHONUNBUFFERED (live logs). Everything else belongs in .env.

const appRoot = process.env.MAXWELL_APP_ROOT || __dirname;

module.exports = {
	apps: [
		{
			name: "maxwell-bot",
			script: "bot.py",
			interpreter: "python3",
			cwd: appRoot,
			instances: 1,
			autorestart: true,
			watch: false,
			max_memory_restart: "1G",
			kill_timeout: 15000, // 15s for graceful shutdown (memory/REM flush)
			kill_signal: "SIGTERM",
			env: {
				PYTHONUNBUFFERED: "1",
			},
			log_date_format: "YYYY-MM-DD HH:mm:ss Z",
			merge_logs: true,
		},
		{
			// Ollama serves the embedding model (qwen3-embedding) and the
			// autonomy/background-agent model (AUTONOMY_BASE_URL points at
			// localhost:11434). It runs here rather than under systemd because
			// the packaged unit runs as user `ollama` with HOME=/usr/share/ollama,
			// whose model store is empty — the 2.3G of pulled models live in
			// /root/.ollama and /root is 0700. pm2 runs as root, so it sees them.
			// The systemd unit is stopped and disabled; don't re-enable it
			// without moving the model store first.
			name: "ollama",
			script: "ollama",
			args: "serve",
			interpreter: "none",
			cwd: appRoot,
			instances: 1,
			autorestart: true,
			watch: false,
			kill_timeout: 10000,
			kill_signal: "SIGTERM",
			env: {
				HOME: "/root",
				OLLAMA_ORIGINS: "*",
			},
			log_date_format: "YYYY-MM-DD HH:mm:ss Z",
			merge_logs: true,
		},
		{
			name: "maxwell-api",
			script: "api/api_server.py",
			interpreter: "python3",
			cwd: appRoot,
			instances: 1,
			autorestart: true,
			watch: false,
			max_memory_restart: "512M",
			kill_timeout: 5000,
			kill_signal: "SIGTERM",
			env: {
				PYTHONUNBUFFERED: "1",
			},
			log_date_format: "YYYY-MM-DD HH:mm:ss Z",
			merge_logs: true,
		},
	],
};
