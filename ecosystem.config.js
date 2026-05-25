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
const baseEnv = {
  ...process.env,
  ...fileEnv,
  NODE_ENV: 'production',
  PYTHONUNBUFFERED: '1'
};

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
      env: baseEnv,
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true
    }
  ]
};
