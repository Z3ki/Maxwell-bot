import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from datetime import date, timedelta

import usage_metrics as metrics


def _preferences(root, tracking):
    path = root / 'plugins' / 'maxwell_extras' / 'user_preferences.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'users': {'12345': {'defaults': {'tracking': tracking}}}}))


def test_aggregates_privacy_and_histogram(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_MODEL', 'example')
    _preferences(tmp_path, 'on')
    metrics._record(tmp_path, 'model', {'user_id': '12345', 'guild_id': '999', 'model': 'example', 'tokens_in': 10, 'duration_ms': 80, 'prompt': 'private prompt'})
    metrics._record(tmp_path, 'model', {'user_id': '12345', 'guild_id': '999', 'duration_ms': 300})
    report = metrics.stats(tmp_path, 1)
    assert report['opted_in_distinct_user_days'] == 1
    assert report['estimated_active_users'] == 1
    assert report['estimated_active_servers'] == 1
    assert report['distinct_servers'] == 1
    assert report['kinds']['model']['tokens_in']['total'] == 10
    assert report['kinds']['model']['model_p95_latency_ms_upper_bound']['example'] == 100
    assert report['kinds']['model']['p95_latency_ms_upper_bound'] == 500
    assert metrics.export_user(tmp_path, '12345') == [{'day': date.today().isoformat(), 'activity': 'present'}]
    with sqlite3.connect(tmp_path / 'usage_metrics.sqlite3') as db:
        dump = '\n'.join(db.iterdump())
    assert '12345' not in dump
    assert 'private prompt' not in dump
    assert metrics.delete_user(tmp_path, '12345') == 1
    assert not metrics.export_user(tmp_path, '12345')
    assert metrics.stats(tmp_path, 1)['kinds']['model']['events']['total'] == 2


def test_opt_out_and_missing_preference_never_store_user(tmp_path):
    metrics._record(tmp_path, 'turn', {'user_id': '12345'})
    _preferences(tmp_path, 'off')
    metrics._record(tmp_path, 'turn', {'user_id': '12345'})
    assert metrics.stats(tmp_path)['opted_in_distinct_user_days'] == 0
    assert metrics.stats(tmp_path)['estimated_active_users'] == 1


def test_retention_and_daily_unlinkability(tmp_path, monkeypatch):
    monkeypatch.setenv('USAGE_METRICS_RETENTION_DAYS', '2')
    monkeypatch.setenv('USAGE_METRICS_ID_RETENTION_DAYS', '1')
    _preferences(tmp_path, 'on')
    metrics._record(tmp_path, 'turn', {'user_id': '12345'})
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    with metrics._connect(tmp_path) as db:
        salt = metrics._salt(db)
        db.execute('INSERT INTO members VALUES (?,?,?)', (yesterday, 'user', metrics._digest(salt, 'user', '12345', yesterday)))
        db.execute('INSERT INTO daily VALUES (?,?,?,?,?)', (yesterday, 'turn', 'events', 'total', 1))
    report = metrics.stats(tmp_path, 2)
    assert report['opted_in_distinct_user_days'] == 1
    assert report['kinds']['turn']['events']['total'] == 2


def test_rejects_untrusted_dimensions(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_MODEL', 'sk-secret')
    metrics._record(tmp_path, 'model', {'model': 'sk-secret', 'provider': 'user-provider', 'name': 'sk-abcdef', 'duration_ms': 2})
    result = metrics.stats(tmp_path)['kinds']['model']
    assert 'model' not in result
    assert 'provider' not in result
    assert 'name' not in result
    assert not result['model_p95_latency_ms_upper_bound']


def test_worker_models_are_grouped_with_usage(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_WORKER_MODEL', 'claude-haiku-5-5')
    monkeypatch.setenv('AI_WORKER_FALLBACK_MODEL', 'deepseek/flash')
    metrics._record(tmp_path, 'model', {
        'model': 'claude-haiku-5-5', 'provider': 'worker',
        'tokens_in': 20, 'tokens_out': 3, 'duration_ms': 100, 'success': True,
    })
    metrics._record(tmp_path, 'model', {
        'model': 'deepseek/flash', 'provider': 'fallback',
        'tokens_in': 5, 'tokens_out': 2, 'duration_ms': 200, 'success': True,
    })
    report = metrics.stats(tmp_path)
    models = {(row['model'], row['provider']) for row in report['models']}
    assert models == {('claude-haiku-5-5', 'worker'), ('deepseek/flash', 'fallback')}
    assert report['kinds']['model']['tokens_in']['total'] == 25
    assert report['kinds']['model']['tokens_out']['total'] == 5


def test_cli_json(tmp_path):
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'stats.py'
    import os
    result = subprocess.run(
        [sys.executable, str(script), '--days', '3', '--json'],
        env={**os.environ, 'DATA_DIR': str(tmp_path)},
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout)['days'] == 3


def test_grouped_latency_and_summary(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_MODEL', 'example')
    for duration, success in ((10, True), (30, False)):
        metrics._record(tmp_path, 'tool', {'name': 'lookup', 'duration_ms': duration, 'success': success})
        metrics._record(tmp_path, 'model', {'model': 'example', 'provider': 'primary', 'duration_ms': duration, 'success': success})
        metrics._record(tmp_path, 'turn', {'duration_ms': duration})
    metrics._record(tmp_path, 'command', {'name': 'help'})
    metrics._record(tmp_path, 'search', {'provider': 'tavily', 'queries': 2, 'results': 3})
    metrics._record(tmp_path, 'error', {'error_type': 'ValueError'})
    report = metrics.stats(tmp_path)
    assert report['kinds']['turn']['average_latency_ms'] == 20
    assert report['tools'][0]['average_latency_ms'] == 20
    assert report['tools'][0]['failures'] == 1
    assert report['tools'][0]['p95_latency_ms_upper_bound'] == 50
    assert report['models'][0]['model'] == 'example'
    assert report['models'][0]['provider'] == 'primary'
    assert report['models'][0]['average_latency_ms'] == 20
    assert report['top_commands'] == [('help', 1)]
    assert report['search']['queries'] == 2
    assert report['search']['backends']['tavily'] == {'calls': 1, 'queries': 2, 'results': 3}
    assert report['errors']['ValueError'] == 1
    summary = metrics.format_summary(report)
    assert 'Tool lookup' in summary
    assert 'Model example / primary' in summary
    assert 'Tokens:' in summary
