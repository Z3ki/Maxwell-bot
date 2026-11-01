"""Content-free, best-effort operational metrics.

Writes are serialized through a bounded daemon queue so callers never wait for SQLite.
Daily aggregate rows are anonymous; short-lived keyed membership rows support distinct
counts and privacy access/deletion. Latencies use fixed upper-bound buckets.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import math
import os
import json
from pathlib import Path
import queue
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, timedelta

logger = logging.getLogger(__name__)
_KINDS = frozenset({'turn', 'model', 'tool', 'search', 'command', 'error'})
_DIMENSIONS = ('trigger', 'model', 'provider', 'name', 'error_type', 'outcome', 'success')
_NUMBERS = ('queries', 'results', 'tokens_in', 'tokens_out')
_BUCKETS = (10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000, 300000, 600000)
_queue = queue.Queue(maxsize=10000)
_worker_lock = threading.Lock()
_worker = None


def _days(name, default, maximum):
    try:
        return min(maximum, max(1, int(os.environ.get(name, default))))
    except (ValueError, TypeError):
        return default


@contextmanager
def _connect(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / 'usage_metrics.sqlite3', timeout=5)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS daily (day TEXT NOT NULL, kind TEXT NOT NULL,
              dimension TEXT NOT NULL, value TEXT NOT NULL, count INTEGER NOT NULL,
              PRIMARY KEY(day,kind,dimension,value));
            CREATE TABLE IF NOT EXISTS members (day TEXT NOT NULL, type TEXT NOT NULL,
              digest TEXT NOT NULL, PRIMARY KEY(day,type,digest));
            CREATE TABLE IF NOT EXISTS sketches (day TEXT NOT NULL, type TEXT NOT NULL,
              registers BLOB NOT NULL, PRIMARY KEY(day,type));
        ''')
        with db:
            yield db
    finally:
        db.close()


def _salt(db):
    db.execute("INSERT OR IGNORE INTO metadata VALUES ('salt', ?)", (secrets.token_bytes(32),))
    return db.execute("SELECT value FROM metadata WHERE key='salt'").fetchone()[0]


def _digest(salt, kind, value, day=''):
    return hmac.new(salt, (kind + ':' + day + ':' + str(value)).encode(), hashlib.sha256).hexdigest()


def _increment(db, day, kind, dimension, value, amount=1):
    if amount:
        db.execute('''INSERT INTO daily VALUES (?,?,?,?,?) ON CONFLICT(day,kind,dimension,value)
                      DO UPDATE SET count=count+excluded.count''', (day, kind, dimension, str(value), amount))

def _dimension(key, value):
    if isinstance(value, bool):
        value = str(value).lower()
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,80}', value):
        return None
    if re.search(r'(?i)(?:sk-|api[_-]?key|bearer|token|secret|password)', value):
        return None
    if key == 'model':
        configured = {os.environ.get(name) for name in (
            'AI_MODEL', 'AI_FALLBACK_MODEL', 'AI_VISION_MODEL', 'AI_PREMIUM_MODEL',
            'AI_WORKER_MODEL', 'AI_WORKER_FALLBACK_MODEL', 'OLLAMA_MODEL')}
        if value not in configured:
            return None
    elif key == 'provider' and value not in {'primary', 'fallback', 'vision', 'worker', 'searxng', 'tavily', 'ddgs', 'none'}:
        return None
    elif key == 'name' and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', value):
        return None
    return value


def _p95(histogram):
    total = sum(histogram.values())
    if not total:
        return None
    threshold = math.ceil(total * .95)
    cumulative = 0
    for bucket in (*map(str, _BUCKETS), 'overflow'):
        cumulative += histogram.get(bucket, 0)
        if cumulative >= threshold:
            return int(bucket) if bucket != 'overflow' else None
    return None


def _sketch_add(db, day, label, value, salt):
    digest = hmac.new(salt, ('sketch:' + label + ':' + value).encode(), hashlib.sha256).digest()
    index = digest[0]
    rest = int.from_bytes(digest[1:9], 'big')
    rank = 65 - rest.bit_length()
    row = db.execute('SELECT registers FROM sketches WHERE day=? AND type=?', (day, label)).fetchone()
    registers = bytearray(row[0]) if row else bytearray(256)
    registers[index] = max(registers[index], rank)
    db.execute('INSERT OR REPLACE INTO sketches VALUES (?,?,?)', (day, label, bytes(registers)))


def _estimate(registers):
    zeros = registers.count(0)
    if zeros:
        return round(256 * math.log(256 / zeros))
    return round((0.7213 / (1 + 1.079 / 256)) * 256 * 256 / sum(2.0 ** -r for r in registers))


def _user_allowed(root, user_id):
    try:
        path = Path(root) / 'plugins' / 'maxwell_extras' / 'user_preferences.json'
        preferences = json.loads(path.read_text('utf-8'))
        return preferences['users'][str(user_id)]['defaults']['tracking'] == 'on'
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _prune(db, today):
    aggregate_days = max(2, _days('USAGE_METRICS_RETENTION_DAYS', 90, 3650))
    member_days = min(aggregate_days - 1, _days('USAGE_METRICS_ID_RETENTION_DAYS', 7, 365))
    db.execute('DELETE FROM daily WHERE day < ?', ((today - timedelta(days=aggregate_days - 1)).isoformat(),))
    db.execute('DELETE FROM sketches WHERE day < ?', ((today - timedelta(days=aggregate_days - 1)).isoformat(),))
    db.execute('DELETE FROM members WHERE day < ?', ((today - timedelta(days=member_days - 1)).isoformat(),))


def _record(root, kind, fields):
    today = date.today()
    day = today.isoformat()
    with _connect(root) as db:
        _prune(db, today)
        _increment(db, day, kind, 'events', 'total')
        for key in _DIMENSIONS:
            value = _dimension(key, fields.get(key))
            if value is not None:
                _increment(db, day, kind, key, value)
        for key in _NUMBERS:
            value = fields.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000000000:
                _increment(db, day, kind, key, 'total', value)
        name = _dimension('name', fields.get('name'))
        model = _dimension('model', fields.get('model'))
        provider = _dimension('provider', fields.get('provider'))
        if kind == 'search':
            backend = provider or 'unknown'
            _increment(db, day, kind, 'backend_calls', backend)
            for key in ('queries', 'results'):
                value = fields.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000000000:
                    _increment(db, day, kind, 'backend_' + key, backend, value)
        group = None
        if kind == 'tool' and name:
            group = name
        elif kind == 'model':
            group = (model or 'unknown') + '|' + (provider or 'unknown')
        if group is not None:
            _increment(db, day, kind, 'group_events', group)
            if isinstance(fields.get('success'), bool):
                _increment(db, day, kind, 'group_success' if fields['success'] else 'group_failure', group)
            if kind == 'model':
                for key in ('tokens_in', 'tokens_out'):
                    value = fields.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000000000:
                        _increment(db, day, kind, 'group_' + key, group, value)
        duration = fields.get('duration_ms')
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and 0 <= duration <= 1000000000:
            micros = round(duration * 1000)
            bucket = next((x for x in _BUCKETS if duration <= x), 'overflow')
            _increment(db, day, kind, 'latency_ms', bucket)
            _increment(db, day, kind, 'duration_us', 'total', micros)
            _increment(db, day, kind, 'duration_samples', 'total')
            if kind == 'model' and model:
                _increment(db, day, kind, 'model_latency_ms', model + '|' + str(bucket))
            if group is not None:
                _increment(db, day, kind, 'group_duration_us', group, micros)
                _increment(db, day, kind, 'group_duration_samples', group)
                _increment(db, day, kind, 'group_latency_ms', group + '|' + str(bucket))
        all_ids = [(label, str(fields.get(key))) for label, key in (('user', 'user_id'), ('guild', 'guild_id')) if fields.get(key) is not None and str(fields.get(key)).isdecimal() and len(str(fields.get(key))) <= 20]
        if all_ids:
            salt = _salt(db)
            for label, value in all_ids:
                _sketch_add(db, day, label, value, salt)
        guild_id = fields.get('guild_id')
        if guild_id is not None and str(guild_id).isdecimal() and len(str(guild_id)) <= 20:
            salt = _salt(db)
            db.execute('INSERT OR IGNORE INTO members VALUES (?,?,?)',
                       (day, 'guild', _digest(salt, 'guild', guild_id)))
        user_id = fields.get('user_id')
        preferences_path = Path(root) / 'plugins' / 'maxwell_extras' / 'user_preferences.json'
        if (user_id is not None and str(user_id).isdecimal() and
                len(str(user_id)) <= 20 and preferences_path.is_file()):
            # Serialize the membership commit with the preference writer.
            from utils import FileLock
            with FileLock(preferences_path, timeout=5):
                if _user_allowed(root, user_id):
                    salt = _salt(db)
                    db.execute('INSERT OR IGNORE INTO members VALUES (?,?,?)',
                               (day, 'user', _digest(salt, 'user', user_id, day)))
                    db.commit()


def _run():
    roots = set()
    next_prune = time.monotonic() + 3600
    while True:
        try:
            root, kind, fields = _queue.get(timeout=max(0.01, next_prune - time.monotonic()))
        except queue.Empty:
            pass
        else:
            roots.add(root)
            try:
                _record(root, kind, fields)
            except Exception:
                logger.warning('Unable to record usage metrics', exc_info=False)
            finally:
                _queue.task_done()
        if time.monotonic() >= next_prune:
            for root in roots:
                try:
                    with _connect(root) as db:
                        _prune(db, date.today())
                except Exception:
                    logger.warning('Unable to prune usage metrics', exc_info=False)
            next_prune = time.monotonic() + 3600


def record_event(data_dir, kind, **fields):
    """Queue an event without blocking; saturation or storage failures drop it."""
    global _worker
    if kind not in _KINDS or data_dir is None:
        return
    # Whitelist before queueing: message text and other unexpected fields never persist.
    allowed = set(_DIMENSIONS + _NUMBERS + ('duration_ms', 'user_id', 'guild_id'))
    try:
        with _worker_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_run, name='usage-metrics', daemon=True)
                _worker.start()
        _queue.put_nowait((str(data_dir), kind, {k: v for k, v in fields.items() if k in allowed}))
    except Exception:
        logger.warning('Unable to queue usage metrics', exc_info=False)


def _existing(root):
    return (Path(root) / 'usage_metrics.sqlite3').is_file()


def export_user(root, user_id):
    if not _existing(root):
        return []
    with _connect(root) as db:
        salt = db.execute("SELECT value FROM metadata WHERE key='salt'").fetchone()
        if not salt:
            return []
        rows = [(day,) for (day,) in db.execute("SELECT DISTINCT day FROM members WHERE type='user' ORDER BY day") if db.execute("SELECT 1 FROM members WHERE day=? AND type='user' AND digest=?", (day, _digest(salt[0], 'user', user_id, day))).fetchone()]
        return [{'day': row[0], 'activity': 'present'} for row in rows]


def delete_user(root, user_id):
    if not _existing(root):
        return 0
    with _connect(root) as db:
        salt = db.execute("SELECT value FROM metadata WHERE key='salt'").fetchone()
        if not salt:
            return 0
        days = [row[0] for row in db.execute("SELECT DISTINCT day FROM members WHERE type='user'")]
        return sum(db.execute("DELETE FROM members WHERE day=? AND type='user' AND digest=?", (day, _digest(salt[0], 'user', user_id, day))).rowcount for day in days)


def stats(root, days=30):
    days = int(days)
    if days < 1 or days > 3650:
        raise ValueError('days must be between 1 and 3650')
    result = {'days': days, 'from': (date.today() - timedelta(days=days-1)).isoformat(), 'kinds': {}, 'estimated_active_users': 0, 'estimated_active_servers': 0, 'opted_in_distinct_user_days': 0, 'distinct_servers': 0, 'distinct_retention_days': min(max(2, _days('USAGE_METRICS_RETENTION_DAYS', 90, 3650)) - 1, _days('USAGE_METRICS_ID_RETENTION_DAYS', 7, 365))}
    if not _existing(root):
        return result
    with _connect(root) as db:
        _prune(db, date.today())
        rows = db.execute('SELECT kind,dimension,value,SUM(count) FROM daily WHERE day>=? GROUP BY kind,dimension,value', (result['from'],)).fetchall()
        for kind, dimension, value, count in rows:
            result['kinds'].setdefault(kind, {}).setdefault(dimension, {})[value] = count
        for label, count in db.execute('SELECT type,COUNT(DISTINCT digest) FROM members WHERE day>=? GROUP BY type', (result['from'],)):
            result['opted_in_distinct_user_days' if label == 'user' else 'distinct_servers'] = count
        for label in ('user', 'guild'):
            merged = bytearray(256)
            for (registers,) in db.execute('SELECT registers FROM sketches WHERE type=? AND day>=?', (label, result['from'])):
                merged = bytearray(max(a, b) for a, b in zip(merged, registers))
            result['estimated_active_users' if label == 'user' else 'estimated_active_servers'] = _estimate(merged)
    result['tools'] = []
    result['models'] = []
    for metrics in result['kinds'].values():
        model_histograms = {}
        for key, count in metrics.get('model_latency_ms', {}).items():
            model, bucket = key.rsplit('|', 1)
            model_histograms.setdefault(model, {})[bucket] = count
        metrics['model_p95_latency_ms_upper_bound'] = {}
        for model, histogram in model_histograms.items():
            threshold = math.ceil(sum(histogram.values()) * .95)
            cumulative = 0
            for bucket in (*map(str, _BUCKETS), 'overflow'):
                cumulative += histogram.get(bucket, 0)
                if cumulative >= threshold:
                    metrics['model_p95_latency_ms_upper_bound'][model] = int(bucket) if bucket != 'overflow' else None
                    break
        histogram = metrics.get('latency_ms', {})
        samples = metrics.get('duration_samples', {}).get('total', 0)
        if samples:
            metrics['average_latency_ms'] = metrics.get('duration_us', {}).get('total', 0) / samples / 1000
        total = sum(histogram.values())
        if total:
            threshold = math.ceil(total * .95)
            cumulative = 0
            for bucket in (*map(str, _BUCKETS), 'overflow'):
                cumulative += histogram.get(bucket, 0)
                if cumulative >= threshold:
                    metrics['p95_latency_ms_upper_bound'] = int(bucket) if bucket != 'overflow' else None
                    break
    for kind, output in (('tool', 'tools'), ('model', 'models')):
        metrics = result['kinds'].get(kind, {})
        histograms = {}
        for key, count in metrics.get('group_latency_ms', {}).items():
            group, bucket = key.rsplit('|', 1)
            histograms.setdefault(group, {})[bucket] = count
        for group, count in metrics.get('group_events', {}).items():
            samples = metrics.get('group_duration_samples', {}).get(group, 0)
            histogram = histograms.get(group, {})
            row = {'calls': count, 'successes': metrics.get('group_success', {}).get(group, 0),
                   'failures': metrics.get('group_failure', {}).get(group, 0),
                   'average_latency_ms': (metrics.get('group_duration_us', {}).get(group, 0) / samples / 1000) if samples else None,
                   'p95_latency_ms_upper_bound': _p95(histogram)}
            if kind == 'tool':
                row['name'] = group
            else:
                row['model'], row['provider'] = group.split('|', 1)
                row['tokens_in'] = metrics.get('group_tokens_in', {}).get(group, 0)
                row['tokens_out'] = metrics.get('group_tokens_out', {}).get(group, 0)
            result[output].append(row)
        result[output].sort(key=lambda item: (-item['calls'], item.get('name', item.get('model', '')), item.get('provider', '')))
    result['top_commands'] = sorted(result['kinds'].get('command', {}).get('name', {}).items(), key=lambda item: (-item[1], item[0]))
    result['search'] = {key: result['kinds'].get('search', {}).get(key, {}).get('total', 0) for key in ('queries', 'results')}
    result['search']['calls'] = result['kinds'].get('search', {}).get('events', {}).get('total', 0)
    search_metrics = result['kinds'].get('search', {})
    result['search']['backends'] = {
        backend: {
            'calls': count,
            'queries': search_metrics.get('backend_queries', {}).get(backend, 0),
            'results': search_metrics.get('backend_results', {}).get(backend, 0),
        }
        for backend, count in search_metrics.get('backend_calls', {}).items()
    }
    result['errors'] = result['kinds'].get('error', {}).get('error_type', {})
    result['tokens'] = {key: result['kinds'].get('model', {}).get(key, {}).get('total', 0) for key in ('tokens_in', 'tokens_out')}
    return result


def format_summary(report):
    """Compact operator-facing summary; full details remain in JSON."""
    def latency(row):
        avg = row.get('average_latency_ms')
        p95 = row.get('p95_latency_ms_upper_bound')
        return f"avg {avg:.1f}ms" if avg is not None and p95 is None else (
            f"avg {avg:.1f}ms, p95 ≤{p95}ms" if avg is not None else "latency unavailable")

    kinds = report.get('kinds', {})
    lines = [
        f"Usage over {report['days']} days · estimated active users {report['estimated_active_users']}, servers {report['estimated_active_servers']}",
    ]
    for kind in ('turn', 'model'):
        row = kinds.get(kind, {})
        lines.append(f"{kind.title()}: {row.get('events', {}).get('total', 0)} calls, {latency(row)}")
    lines.extend(
        f"Model {row['model']} / {row['provider']}: {row['calls']} calls, {row['failures']} failed, {latency(row)}"
        for row in report.get('models', [])[:3]
    )
    lines.extend(
        f"Tool {row['name']}: {row['calls']} calls, {row['failures']} failed, {latency(row)}"
        for row in report.get('tools', [])[:5]
    )
    commands = report.get('top_commands', [])[:5]
    lines.append("Commands: " + (", ".join(f"{name} {count}" for name, count in commands) or "none"))
    search = report.get('search', {})
    lines.append(f"Search: {search.get('calls', 0)} calls, {search.get('queries', 0)} queries, {search.get('results', 0)} results")
    errors = sorted(report.get('errors', {}).items(), key=lambda item: (-item[1], item[0]))[:5]
    lines.append("Errors: " + (", ".join(f"{name} {count}" for name, count in errors) or "none"))
    tokens = report.get('tokens', {})
    lines.append(f"Tokens: {tokens.get('tokens_in', 0)} in / {tokens.get('tokens_out', 0)} out")
    return "\n".join(lines)
