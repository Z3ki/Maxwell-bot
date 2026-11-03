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
    metrics._record(tmp_path, 'model', {'model': 'sk-secret', 'provider': 'https://user-provider/path', 'name': 'sk-abcdef', 'duration_ms': 2})
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


def test_model_groups_distinguish_routes_and_preserve_legacy(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_MODEL', 'example')
    for route, duration, success, tokens in (
        ('primary', 10, True, 7),
        ('fallback', 80, False, 3),
    ):
        metrics._record(tmp_path, 'model', {
            'model': 'example', 'provider': 'api.example.com', 'route': route,
            'duration_ms': duration, 'success': success, 'tokens_in': tokens,
        })
    with metrics._connect(tmp_path) as db:
        day = date.today().isoformat()
        for dimension, value, count in (
            ('group_events', 'example|primary', 2),
            ('group_success', 'example|primary', 1),
            ('group_failure', 'example|primary', 1),
            ('group_tokens_out', 'example|primary', 4),
            ('group_duration_us', 'example|primary', 40000),
            ('group_duration_samples', 'example|primary', 2),
            ('group_latency_ms', 'example|primary|25', 2),
        ):
            metrics._increment(db, day, 'model', dimension, value, count)
    report = metrics.stats(tmp_path)
    rows = {(row['provider'], row['route']): row for row in report['models']}
    assert rows[('api.example.com', 'primary')]['tokens_in'] == 7
    assert rows[('api.example.com', 'primary')]['p95_latency_ms_upper_bound'] == 10
    assert rows[('api.example.com', 'fallback')]['failures'] == 1
    assert rows[('api.example.com', 'fallback')]['average_latency_ms'] == 80
    legacy = rows[('unknown', 'primary')]
    assert (legacy['calls'], legacy['successes'], legacy['failures']) == (2, 1, 1)
    assert legacy['tokens_out'] == 4
    assert legacy['average_latency_ms'] == 20
    assert legacy['p95_latency_ms_upper_bound'] == 25
    assert report['tokens']['tokens_in'] == 10
    summary = metrics.format_summary(report)
    assert 'Model example / provider api.example.com / route primary' in summary
    assert 'Model example / provider api.example.com / route fallback' in summary
    assert 'Model example / provider unknown / route primary' in summary


def test_model_dimensions_accept_redacted_labels_and_reject_private_values(tmp_path):
    metrics._record(tmp_path, 'model', {'model': 'byok', 'provider': 'byok', 'route': 'byok'})
    metrics._record(tmp_path, 'model', {'model': 'other', 'provider': 'worker', 'route': 'worker'})
    metrics._record(tmp_path, 'model', {
        'model': 'private-model', 'provider': 'https://private.example/path',
        'route': 'private-route',
    })
    rows = {(row['model'], row['provider'], row['route']) for row in metrics.stats(tmp_path)['models']}
    assert rows == {('byok', 'byok', 'byok'), ('other', 'worker', 'worker'), ('unknown', 'unknown', 'unknown')}


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
    assert 'Model example / provider primary' in summary
    assert 'Tokens:' in summary


def test_cache_aggregation_keeps_unknown_separate_and_weights_known_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv('AI_MODEL', 'example')
    common = {'model': 'example', 'provider': 'primary', 'route': 'primary'}
    for fields in (
        {'tokens_in': 100, 'cached_tokens': 80, 'cache_write_tokens': 20},
        {'tokens_in': 300, 'cached_tokens': 0, 'cache_write_tokens': 0},
        {'tokens_in': 600},
        {'cached_tokens': 10},
        {'tokens_in': 10, 'cached_tokens': True, 'cache_write_tokens': -1},
    ):
        metrics._record(tmp_path, 'model', {**common, **fields})
    metrics._record(tmp_path, 'model', {
        **common, 'route': 'fallback', 'tokens_in': 50,
    })
    report = metrics.stats(tmp_path)
    rows = {row['route']: row for row in report['models']}
    primary, fallback = rows['primary'], rows['fallback']
    assert primary['cached_tokens'] == 90
    assert primary['cache_write_tokens'] == 20
    assert primary['cached_tokens_samples'] == 3
    assert primary['cache_write_tokens_samples'] == 2
    assert primary['cache_hit_ratio'] == 0.2
    assert fallback['cached_tokens'] is None
    assert fallback['cache_write_tokens'] is None
    assert fallback['cache_hit_ratio'] is None
    assert report['tokens']['cached_tokens'] == 90
    assert report['tokens']['cache_hit_ratio'] == 0.2
    summary = metrics.format_summary(report)
    assert 'cache reads 90, writes 20, hit ratio 20.0%' in summary
    assert 'cache reads unknown, writes unknown, hit ratio unknown' in summary


def test_cache_zero_is_reported_and_cli_outputs_cache(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv('AI_MODEL', 'example')
    metrics._record(tmp_path, 'model', {
        'model': 'example', 'route': 'primary', 'tokens_in': 100,
        'cached_tokens': 0, 'cache_write_tokens': 0,
    })
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'stats.py'
    env = {**os.environ, 'DATA_DIR': str(tmp_path)}
    command = [sys.executable, str(script)]
    output = subprocess.run(command, env=env, capture_output=True, text=True, check=True).stdout
    assert 'cache reads 0, writes 0, hit ratio 0.0%' in output
    output = subprocess.run(command + ['--json'], env=env, capture_output=True, text=True, check=True).stdout
    report = json.loads(output)
    assert report['models'][0]['cached_tokens'] == 0
    assert report['models'][0]['cache_write_tokens'] == 0
    assert report['models'][0]['cache_hit_ratio'] == 0
