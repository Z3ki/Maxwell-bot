const fs = require('fs');
const path = require('path');

function loadEnvFile(filePath) {
  if (!fs.existsSync(filePath)) {
    return {};
  }
  return fs.readFileSync(filePath, 'utf8').split(/\r?\n/).reduce((env, line) => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) {
      return env;
    }
    const eq = trimmed.indexOf('=');
    if (eq < 1) {
      return env;
    }
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
      value = value.slice(1, -1);
    }
    env[key] = value;
    return env;
  }, {});
}

const appRoot = process.env.MAXWELL_APP_ROOT || __dirname;
const fileEnv = loadEnvFile(process.env.MAXWELL_ENV_FILE || path.join(appRoot, '.env'));

// 2026-07-21: precedence fix. The .env file is the SOURCE OF TRUTH —
// editing OLLAMA_MODEL in .env should change the live model. Previously
// `...process.env` came last and won, so a stale `OLLAMA_MODEL=kimi-k2.6:cloud`
// exported in the pm2 user's shell would silently override the .env
// change. New: .env wins, process.env is the fallback (so you can
// still set MAXWELL_* and other dev-machine vars in your shell).
//
// Escape hatch: MAXWELL_ENV_FILE_PRECEDENCE=process (or any value other
// than 'file') restores the old behaviour. Useful for local dev where
// you want your shell env to override .env.
const fileWins =
  (process.env.MAXWELL_ENV_FILE_PRECEDENCE || 'file').toLowerCase() !== 'process';
const baseEnv = fileWins
  ? { ...process.env, ...fileEnv, NODE_ENV: 'production', PYTHONUNBUFFERED: '1' }
  : { ...fileEnv, ...process.env, NODE_ENV: 'production', PYTHONUNBUFFERED: '1' };

module.exports = {
  apps: [
    {
      name: 'maxwell-bot',
      script: 'bot.py',
      interpreter: 'python3',
      cwd: appRoot,
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '1G',
      kill_timeout: 15000,  // 15s for graceful shutdown (memory/REM flush)
      kill_signal: 'SIGTERM',
      env: baseEnv,
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    },
    {
      name: 'maxwell-api',
      script: 'api/api_server.py',
      interpreter: 'python3',
      cwd: appRoot,
      instances: 1,
      autorestart: true,
      watch: false,
      max_memory_restart: '512M',
      kill_timeout: 5000,
      kill_signal: 'SIGTERM',
      env: baseEnv,
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    }
  ]
};
