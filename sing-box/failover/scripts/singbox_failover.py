#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib import error, parse, request

try:
    import yaml  # type: ignore
except ImportError:
    print('PyYAML is required: pip install pyyaml', file=sys.stderr)
    raise

UTC = timezone.utc


def now_utc() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(UTC).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(UTC)


@dataclass
class CheckResult:
    ok: bool
    status_code: int | None
    latency_ms: int | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            'ok': self.ok,
            'status_code': self.status_code,
            'latency_ms': self.latency_ms,
            'error': self.error,
        }


class FailoverManager:
    def __init__(self, inventory_path: Path, dry_run: bool, check_only: bool) -> None:
        self.inventory_path = inventory_path
        self.dry_run = dry_run
        self.check_only = check_only
        self.inventory = self._load_yaml(inventory_path)
        self.runtime = self.inventory['runtime']
        self.configs = self.inventory['configs']
        self.state_path = Path(self.runtime['state_path'])
        self.log_path = Path(self.runtime['log_path'])
        self.pool_results_path = Path(self.runtime.get('pool_results_path', '/home/petrovov/configs/pool-probe-results.json'))
        self.pool_refresh_max_age_seconds = int(self.runtime.get('pool_refresh_max_age_seconds', 43200))
        self.pool_probe_script = self.runtime.get('pool_probe_script')
        self.active_openai_mode = self.runtime.get('active_openai_mode', 'transport')
        self.state = self._load_state()
        self.timeout = int(self.runtime.get('request_timeout_seconds', 10))
        self.cooldown_seconds = int(self.runtime.get('cooldown_seconds', 900))
        self.failure_threshold = int(self.runtime.get('consecutive_failures_before_switch', 2))
        self.require_both = bool(self.runtime.get('require_both_apis', True))

    def run(self) -> int:
        detected_active_name = self._detect_active_name()
        state_active_name = self.state.get('active_name')
        active_name = detected_active_name or state_active_name
        if detected_active_name and detected_active_name != state_active_name:
            self._log(
                'active_config_reconciled',
                state_active_name=state_active_name,
                detected_active_name=detected_active_name,
            )
        active_cfg = self._find_config(active_name) if active_name else None

        if active_cfg:
            active_health = self._check_apis(self.active_openai_mode)
            self._update_health_state(active_cfg['name'], active_health)
            self._log('active_check', config_name=active_cfg['name'], openai_mode=self.active_openai_mode, **active_health)
            if self._is_healthy(active_health):
                self.state['active_name'] = active_cfg['name']
                self.state['last_known_good'] = active_cfg['name']
                self._save_state()
                return 0
            if self.check_only:
                self._save_state()
                return 2
        else:
            self._log('active_config_unknown')
            if self.check_only:
                return 2

        healthy_pool = self._healthy_pool_candidates(active_name)
        if active_cfg and self._should_notify_active_failure(active_cfg['name']):
            primary_candidate = self._select_primary_candidate(healthy_pool)
            reason = self._summarize_health_error(active_health)
            self._notify_active_failure(active_cfg['name'], reason, primary_candidate['name'] if primary_candidate else None)
        if not healthy_pool:
            self._log('no_healthy_pool_candidates', pool_results_path=str(self.pool_results_path))
            self._save_state()
            return 1

        for cfg in healthy_pool:
            if not self._config_allowed(cfg['name']):
                continue
            if not self._validate_candidate(cfg):
                self._mark_failure(cfg['name'], 'config validation failed')
                continue

            full_probe = self._probe_candidate_full(cfg['name'])
            self._log('candidate_full_probe', config_name=cfg['name'], **full_probe)
            if not full_probe.get('healthy'):
                self._mark_failure(cfg['name'], self._summarize_probe_error(full_probe))
                continue

            if self.dry_run:
                self._log('dry_run_candidate_selected', config_name=cfg['name'])
                self._save_state()
                return 0

            switched = self._apply_config(cfg)
            if not switched:
                self._mark_failure(cfg['name'], 'apply config failed')
                continue

            time.sleep(int(self.runtime.get('stabilization_delay_seconds', 5)))
            post_health = self._check_apis(self.active_openai_mode)
            self._update_health_state(cfg['name'], post_health)
            self._log('post_switch_check', config_name=cfg['name'], openai_mode=self.active_openai_mode, **post_health)
            if self._is_healthy(post_health):
                previous_name = active_cfg['name'] if active_cfg else active_name
                reason = self._summarize_health_error(active_health) if active_cfg else 'active config unknown'
                self.state['active_name'] = cfg['name']
                self.state['last_known_good'] = cfg['name']
                self.state['last_switch_at'] = iso(now_utc())
                self._append_switch_history(previous_name, cfg['name'], reason)
                self._save_state()
                self._notify_failover(previous_name, cfg['name'], reason)
                return 0

        self._log('no_healthy_candidate_found', last_known_good=self.state.get('last_known_good'))
        self._save_state()
        return 1

    def _check_apis(self, openai_mode: str) -> dict[str, Any]:
        telegram = self._check_telegram()
        openai = self._check_openai(openai_mode)
        return {
            'telegram': telegram.to_dict(),
            'openai': openai.to_dict() | {'mode': openai_mode},
            'healthy': telegram.ok and openai.ok if self.require_both else telegram.ok or openai.ok,
        }

    def _check_telegram(self) -> CheckResult:
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        if not token:
            return CheckResult(False, None, None, 'TELEGRAM_BOT_TOKEN is not set')
        base_url = os.getenv('TELEGRAM_API_BASE_URL', 'https://api.telegram.org').rstrip('/')
        url = f'{base_url}/bot{token}/getMe'
        return self._http_json_check(url, headers={}, validator=lambda body: bool(body.get('ok') is True))

    def _check_openai(self, mode: str) -> CheckResult:
        if mode == 'full':
            command = os.getenv('OPENAI_CHECK_COMMAND')
            if not command:
                return CheckResult(False, None, None, 'OPENAI_CHECK_COMMAND is not set')
            started = time.monotonic()
            try:
                result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=self.timeout + 80)
                latency_ms = int((time.monotonic() - started) * 1000)
                if result.returncode == 0:
                    return CheckResult(True, 0, latency_ms, None)
                stderr_tail = (result.stderr or result.stdout or '')[-300:]
                return CheckResult(False, result.returncode, latency_ms, stderr_tail or 'command check failed')
            except Exception as ex:
                latency_ms = int((time.monotonic() - started) * 1000)
                return CheckResult(False, None, latency_ms, str(ex))

        url = os.getenv('OPENAI_TRANSPORT_URL', 'https://chatgpt.com/backend-api/codex')
        return self._http_status_check(url)

    def _http_json_check(self, url: str, headers: dict[str, str], validator=None) -> CheckResult:
        started = time.monotonic()
        req = request.Request(url, headers={'User-Agent': os.getenv('HTTP_CHECK_USER_AGENT', 'singbox-failover/1.0'), **headers})
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                body = response.read().decode('utf-8', errors='replace')
                latency_ms = int((time.monotonic() - started) * 1000)
                status = getattr(response, 'status', None)
                parsed = json.loads(body) if body else {}
                if validator and not validator(parsed):
                    return CheckResult(False, status, latency_ms, 'response validator failed')
                return CheckResult(True, status, latency_ms, None)
        except error.HTTPError as ex:
            latency_ms = int((time.monotonic() - started) * 1000)
            return CheckResult(False, ex.code, latency_ms, f'HTTPError: {ex}')
        except Exception as ex:
            latency_ms = int((time.monotonic() - started) * 1000)
            return CheckResult(False, None, latency_ms, str(ex))

    def _http_status_check(self, url: str) -> CheckResult:
        started = time.monotonic()
        req = request.Request(url, headers={'User-Agent': os.getenv('HTTP_CHECK_USER_AGENT', 'singbox-failover/1.0')})
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                latency_ms = int((time.monotonic() - started) * 1000)
                return CheckResult(True, getattr(response, 'status', None), latency_ms, None)
        except error.HTTPError as ex:
            latency_ms = int((time.monotonic() - started) * 1000)
            if ex.code in (401, 403, 404, 405):
                return CheckResult(True, ex.code, latency_ms, None)
            return CheckResult(False, ex.code, latency_ms, f'HTTPError: {ex}')
        except Exception as ex:
            latency_ms = int((time.monotonic() - started) * 1000)
            return CheckResult(False, None, latency_ms, str(ex))

    def _is_healthy(self, health: dict[str, Any]) -> bool:
        return bool(health.get('healthy'))

    def _healthy_pool_candidates(self, active_name: str | None) -> list[dict[str, Any]]:
        if not self.pool_results_path.exists():
            return []
        age_seconds = time.time() - self.pool_results_path.stat().st_mtime
        if age_seconds > self.pool_refresh_max_age_seconds:
            self._log('pool_results_stale', age_seconds=int(age_seconds), max_age_seconds=self.pool_refresh_max_age_seconds)
            return []
        with self.pool_results_path.open('r', encoding='utf-8') as fh:
            results = json.load(fh)
        healthy_names = {item['name'] for item in results if item.get('healthy')}
        enabled = [cfg for cfg in self.configs if cfg.get('enabled', True) and cfg['name'] in healthy_names and cfg['name'] != active_name]
        enabled.sort(key=lambda cfg: (-int(cfg.get('priority', 0)), cfg['name']))
        return enabled

    def _config_allowed(self, name: str) -> bool:
        cfg_state = self.state.setdefault('configs', {}).setdefault(name, {})
        cooldown_until = parse_iso(cfg_state.get('cooldown_until'))
        return cooldown_until is None or now_utc() >= cooldown_until

    def _select_primary_candidate(self, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
        for cfg in candidates:
            if self._config_allowed(cfg['name']):
                return cfg
        return None

    def _validate_candidate(self, cfg: dict[str, Any]) -> bool:
        path = Path(cfg['path'])
        if not path.exists() or not path.is_file():
            self._log('candidate_missing', config_name=cfg['name'], path=str(path))
            return False
        check_cmd = self.runtime.get('singbox_check_command')
        if not check_cmd:
            return True
        return self._run_command(check_cmd.format(config_path=shlex.quote(str(path))), 'validate_candidate', cfg['name'])

    def _probe_candidate_full(self, config_name: str) -> dict[str, Any]:
        if not self.pool_probe_script:
            return {'healthy': False, 'error': 'pool_probe_script is not configured'}
        out_path = Path('/tmp') / f'singbox-full-probe-{config_name}.json'
        command = [
            sys.executable,
            self.pool_probe_script,
            '--inventory', str(self.inventory_path),
            '--config', config_name,
            '--openai-mode', 'full',
            '--out', str(out_path),
            '--timeout', str(self.timeout),
            '--codex-timeout', str(max(90, self.timeout + 80)),
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=max(180, self.timeout + 120))
        except Exception as ex:
            return {'healthy': False, 'error': str(ex)}
        if result.returncode != 0:
            return {'healthy': False, 'error': (result.stderr or result.stdout or 'full probe failed')[-400:]}
        try:
            data = json.loads(out_path.read_text(encoding='utf-8'))
        except Exception as ex:
            return {'healthy': False, 'error': f'cannot read full probe result: {ex}'}
        if not data:
            return {'healthy': False, 'error': 'empty full probe result'}
        return data[0]

    def _apply_config(self, cfg: dict[str, Any]) -> bool:
        active_path = Path(self.runtime['active_config_path'])
        method = self.runtime.get('switch_method', 'copy-and-restart')
        candidate_path = Path(cfg['path'])
        active_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if method == 'symlink-and-restart':
                tmp_link = active_path.with_suffix(active_path.suffix + '.tmp')
                if tmp_link.exists() or tmp_link.is_symlink():
                    tmp_link.unlink()
                tmp_link.symlink_to(candidate_path)
                tmp_link.replace(active_path)
            else:
                shutil.copy2(candidate_path, active_path)
        except Exception as ex:
            self._log('apply_failed', config_name=cfg['name'], error=str(ex))
            return False
        restart_cmd = self.runtime.get('singbox_restart_command')
        if not restart_cmd:
            return True
        return self._run_command(restart_cmd, 'restart_singbox', cfg['name'])

    def _run_command(self, command: str, event: str, config_name: str) -> bool:
        try:
            result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
        except Exception as ex:
            self._log(event, config_name=config_name, ok=False, error=str(ex))
            return False
        ok = result.returncode == 0
        self._log(event, config_name=config_name, ok=ok, stdout=result.stdout[-500:], stderr=result.stderr[-500:], returncode=result.returncode)
        return ok

    def _update_health_state(self, name: str, health: dict[str, Any]) -> None:
        cfg_state = self.state.setdefault('configs', {}).setdefault(name, {})
        if self._is_healthy(health):
            cfg_state['last_success_at'] = iso(now_utc())
            cfg_state['last_failure_at'] = None
            cfg_state['consecutive_failures'] = 0
            cfg_state['cooldown_until'] = None
            cfg_state['last_error'] = None
            cfg_state['active_failure_notified_at'] = None
            cfg_state['active_failure_notified_reason'] = None
        else:
            cfg_state['last_failure_at'] = iso(now_utc())
            cfg_state['consecutive_failures'] = int(cfg_state.get('consecutive_failures', 0)) + 1
            cfg_state['last_error'] = self._summarize_health_error(health)
            if int(cfg_state['consecutive_failures']) >= self.failure_threshold:
                cfg_state['cooldown_until'] = iso(now_utc() + timedelta(seconds=self.cooldown_seconds))

    def _mark_failure(self, name: str, error_message: str) -> None:
        cfg_state = self.state.setdefault('configs', {}).setdefault(name, {})
        cfg_state['last_failure_at'] = iso(now_utc())
        cfg_state['consecutive_failures'] = int(cfg_state.get('consecutive_failures', 0)) + 1
        cfg_state['last_error'] = error_message
        cfg_state['cooldown_until'] = iso(now_utc() + timedelta(seconds=self.cooldown_seconds))
        self._log('candidate_failed', config_name=name, error=error_message)

    def _summarize_health_error(self, health: dict[str, Any]) -> str:
        parts = []
        for api_name in ('telegram', 'openai'):
            api = health.get(api_name, {})
            if not api.get('ok'):
                parts.append(f"{api_name}: {api.get('error') or 'unhealthy'}")
        return '; '.join(parts) if parts else 'unknown health failure'

    def _summarize_probe_error(self, probe: dict[str, Any]) -> str:
        parts = []
        for api_name in ('telegram', 'openai'):
            api = probe.get(api_name, {})
            if not api.get('ok'):
                parts.append(f"{api_name}: {api.get('error') or 'unhealthy'}")
        return '; '.join(parts) if parts else 'full probe failed'

    def _append_switch_history(self, old_name: str | None, new_name: str, reason: str) -> None:
        history = self.state.setdefault('switch_history', [])
        history.append({
            'ts': iso(now_utc()),
            'old_name': old_name,
            'new_name': new_name,
            'reason': reason,
        })
        if len(history) > 20:
            del history[:-20]

    def _should_notify_active_failure(self, name: str) -> bool:
        cfg_state = self.state.setdefault('configs', {}).setdefault(name, {})
        failures = int(cfg_state.get('consecutive_failures', 0))
        if failures < self.failure_threshold:
            return False
        if cfg_state.get('active_failure_notified_at'):
            return False
        return True

    def _notify_active_failure(self, active_name: str, reason: str, primary_candidate_name: str | None) -> None:
        cfg_state = self.state.setdefault('configs', {}).setdefault(active_name, {})
        old_label = self._config_label(active_name)
        primary_label = self._config_label(primary_candidate_name)
        text = (
            '🚨 Основной sing-box конфиг перестал проходить проверки\n\n'
            f'Причина: {reason}\n'
            f'Текущий основной: {old_label}\n'
            f'Основной кандидат на замену: {primary_label}'
        )
        sent = self._send_telegram_message(text)
        cfg_state['active_failure_notified_at'] = iso(now_utc())
        cfg_state['active_failure_notified_reason'] = reason
        self._log('active_failure_notified', config_name=active_name, primary_candidate=primary_candidate_name, sent=sent, reason=reason)

    def _notify_failover(self, old_name: str | None, new_name: str, reason: str) -> None:
        old_label = self._config_label(old_name)
        new_label = self._config_label(new_name)
        text = (
            '⚠️ sing-box failover выполнен\n\n'
            f'Причина: {reason}\n'
            f'Старый конфиг: {old_label}\n'
            f'Новый конфиг: {new_label}'
        )
        sent = self._send_telegram_message(text)
        self._log('notify_failover_sent', old_name=old_name, new_name=new_name, sent=sent)

    def _send_telegram_message(self, text: str) -> bool:
        token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_NOTIFY_CHAT_ID')
        if not token or not chat_id:
            self._log('notify_skipped', error='TELEGRAM_BOT_TOKEN or TELEGRAM_NOTIFY_CHAT_ID is not set')
            return False
        base_url = os.getenv('TELEGRAM_API_BASE_URL', 'https://api.telegram.org').rstrip('/')
        url = f'{base_url}/bot{token}/sendMessage'
        body = parse.urlencode({'chat_id': chat_id, 'text': text}).encode('utf-8')
        req = request.Request(url, data=body, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode('utf-8', errors='replace') or '{}')
                if payload.get('ok') is True:
                    return True
                self._log('notify_failed', chat_id=chat_id, error='telegram ok=false')
        except Exception as ex:
            self._log('notify_failed', chat_id=chat_id, error=str(ex))
        return False

    def _config_label(self, name: str | None) -> str:
        cfg = self._find_config(name)
        if not cfg:
            return name or 'unknown'
        tag = cfg.get('source_outbound_tag') or (cfg.get('tags') or [''])[0]
        return f"{cfg['name']} ({tag})"

    def _detect_active_name(self) -> str | None:
        active_path = Path(self.runtime['active_config_path'])
        if active_path.is_symlink():
            try:
                target = active_path.resolve()
            except Exception:
                return None
            for cfg in self.configs:
                if Path(cfg['path']).resolve() == target:
                    return cfg['name']
        try:
            with active_path.open('r', encoding='utf-8') as fh:
                active_doc = json.load(fh)
        except Exception:
            return None
        active_outbound = None
        for outbound in active_doc.get('outbounds', []):
            if outbound.get('tag') == 'proxy-out':
                active_outbound = outbound
                break
        if not active_outbound:
            return None
        active_identity = self._outbound_identity(active_outbound)
        for cfg in self.configs:
            try:
                with Path(cfg['path']).open('r', encoding='utf-8') as fh:
                    candidate_doc = json.load(fh)
            except Exception:
                continue
            for outbound in candidate_doc.get('outbounds', []):
                if outbound.get('tag') != 'proxy-out':
                    continue
                if self._outbound_identity(outbound) == active_identity:
                    return cfg['name']
        return None

    @staticmethod
    def _outbound_identity(outbound: dict[str, Any]) -> str:
        normalized = {key: value for key, value in outbound.items() if key != 'tag'}
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def _find_config(self, name: str | None) -> dict[str, Any] | None:
        if not name:
            return None
        for cfg in self.configs:
            if cfg['name'] == name:
                return cfg
        return None

    def _load_yaml(self, path: Path) -> dict[str, Any]:
        with path.open('r', encoding='utf-8') as fh:
            return yaml.safe_load(fh)

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {'active_name': None, 'last_known_good': None, 'configs': {}}
        with self.state_path.open('r', encoding='utf-8') as fh:
            return json.load(fh)

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.state_path.open('w', encoding='utf-8') as fh:
            json.dump(self.state, fh, ensure_ascii=False, indent=2)

    def _log(self, event: str, **fields: Any) -> None:
        payload = {'ts': iso(now_utc()), 'event': event, **fields}
        line = json.dumps(payload, ensure_ascii=False)
        print(line)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open('a', encoding='utf-8') as fh:
            fh.write(line + '\n')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='sing-box config failover by API reachability')
    parser.add_argument('--inventory', required=True, help='path to inventory yaml')
    parser.add_argument('--dry-run', action='store_true', help='log candidate selection without switching')
    parser.add_argument('--check-only', action='store_true', help='check current active config only')
    parser.add_argument('--lock-file', default='/run/singbox-failover.lock', help='shared non-blocking lock path')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lock_path = Path(args.lock_file)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open('a+', encoding='utf-8') as lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({'event': 'failover_skipped', 'reason': 'lock_busy'}))
                return 0
            try:
                manager = FailoverManager(Path(args.inventory), dry_run=args.dry_run, check_only=args.check_only)
                return manager.run()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError as ex:
        print(f'cannot acquire failover lock: {ex}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
