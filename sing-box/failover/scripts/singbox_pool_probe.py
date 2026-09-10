#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml  # type: ignore


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open('r', encoding='utf-8') as fh:
        return yaml.safe_load(fh)


def find_free_port(start: int) -> int:
    port = start
    while port < start + 2000:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(('127.0.0.1', port))
                return port
            except OSError:
                port += 1
    raise RuntimeError('no free port found')


def build_probe_config(candidate_path: Path, port: int, workdir: Path) -> Path:
    data = json.loads(candidate_path.read_text(encoding='utf-8'))
    data['inbounds'] = [{
        'type': 'mixed',
        'tag': 'probe-in',
        'listen': '127.0.0.1',
        'listen_port': port,
        'sniff': True,
        'sniff_override_destination': False,
    }]
    data.setdefault('route', {})['auto_detect_interface'] = True
    out = workdir / f'{candidate_path.stem}.probe.json'
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return out


def wait_port(port: int, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def check_telegram(proxy_url: str, token: str, timeout_seconds: int) -> dict[str, Any]:
    started = time.monotonic()
    cmd = ['curl', '-sS', '--max-time', str(timeout_seconds), '--proxy', proxy_url, f'https://api.telegram.org/bot{token}/getMe']
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds + 10)
    except subprocess.TimeoutExpired:
        latency_ms = int((time.monotonic() - started) * 1000)
        return {'ok': False, 'latency_ms': latency_ms, 'error': f'telegram timeout after {timeout_seconds}s', 'body_preview': None}
    latency_ms = int((time.monotonic() - started) * 1000)
    ok = False
    error = None
    body = res.stdout[:400]
    if res.returncode == 0:
        try:
            parsed = json.loads(res.stdout)
            ok = bool(parsed.get('ok') is True)
            if not ok:
                error = 'telegram response ok=false'
        except Exception as ex:
            error = f'json parse failed: {ex}'
    else:
        error = (res.stderr or 'curl failed')[:300]
    return {'ok': ok, 'latency_ms': latency_ms, 'error': error, 'body_preview': body if not ok else None}


def check_openai_transport(proxy_url: str, url: str, timeout_seconds: int) -> dict[str, Any]:
    started = time.monotonic()
    cmd = [
        'curl', '-sS', '--max-time', str(timeout_seconds), '--proxy', proxy_url,
        '-o', '/dev/null', '-w', '%{http_code}', url,
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds + 10)
    except subprocess.TimeoutExpired:
        latency_ms = int((time.monotonic() - started) * 1000)
        return {'ok': False, 'latency_ms': latency_ms, 'error': f'openai transport timeout after {timeout_seconds}s', 'http_code': None}
    latency_ms = int((time.monotonic() - started) * 1000)
    if res.returncode != 0:
        return {'ok': False, 'latency_ms': latency_ms, 'error': (res.stderr or 'curl failed')[:300], 'http_code': None}
    code = (res.stdout or '').strip()
    try:
        code_int = int(code)
    except Exception:
        code_int = None
    ok = code_int is not None and code_int > 0
    return {'ok': ok, 'latency_ms': latency_ms, 'error': None if ok else f'unexpected http code: {code}', 'http_code': code_int}


def check_codex_full(proxy_url: str, command: str, timeout_seconds: int) -> dict[str, Any]:
    started = time.monotonic()
    env = os.environ.copy()
    env['ALL_PROXY'] = proxy_url
    env['HTTPS_PROXY'] = proxy_url
    env['HTTP_PROXY'] = proxy_url
    try:
        res = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout_seconds, env=env)
        latency_ms = int((time.monotonic() - started) * 1000)
        ok = res.returncode == 0
        return {
            'ok': ok,
            'latency_ms': latency_ms,
            'error': None if ok else ((res.stderr or res.stdout or 'codex command failed')[-400:]),
            'stdout_preview': (res.stdout or '')[:120] if ok else None,
        }
    except subprocess.TimeoutExpired:
        latency_ms = int((time.monotonic() - started) * 1000)
        return {'ok': False, 'latency_ms': latency_ms, 'error': f'codex command timeout after {timeout_seconds}s', 'stdout_preview': None}


def main() -> int:
    parser = argparse.ArgumentParser(description='Probe sing-box candidate configs in isolated localhost mode')
    parser.add_argument('--inventory', required=True)
    parser.add_argument('--config', help='probe only one config by name')
    parser.add_argument('--port-base', type=int, default=2080)
    parser.add_argument('--timeout', type=int, default=10)
    parser.add_argument('--codex-timeout', type=int, default=90)
    parser.add_argument('--openai-mode', choices=['transport', 'full'], default='transport')
    parser.add_argument('--out', default='/home/petrovov/configs/pool-probe-results.json')
    args = parser.parse_args()

    token = os.getenv('TELEGRAM_BOT_TOKEN')
    if not token:
        raise SystemExit('TELEGRAM_BOT_TOKEN is not set')
    transport_url = os.getenv('OPENAI_TRANSPORT_URL', 'https://chatgpt.com/backend-api/codex')
    codex_command = os.getenv('OPENAI_CHECK_COMMAND')
    if args.openai_mode == 'full' and not codex_command:
        raise SystemExit('OPENAI_CHECK_COMMAND is not set for full mode')

    inv = load_yaml(Path(args.inventory))
    configs = [cfg for cfg in inv['configs'] if cfg.get('enabled', True)]
    if args.config:
        configs = [cfg for cfg in configs if cfg['name'] == args.config]
    results: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix='singbox-probe-') as tmp:
        workdir = Path(tmp)
        for idx, cfg in enumerate(configs):
            port = find_free_port(args.port_base + idx)
            proxy_url = f'http://127.0.0.1:{port}'
            probe_config = build_probe_config(Path(cfg['path']), port, workdir)
            proc = subprocess.Popen(
                ['/usr/local/bin/sing-box', 'run', '-c', str(probe_config)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                started_ok = wait_port(port, timeout=8.0)
                if not started_ok:
                    stderr = ''
                    try:
                        stderr = (proc.stderr.read() or '')[:400]
                    except Exception:
                        pass
                    results.append({
                        'name': cfg['name'],
                        'tag': cfg.get('source_outbound_tag') or cfg.get('tags', [''])[0],
                        'server': cfg.get('server'),
                        'probe_started': False,
                        'telegram': {'ok': False, 'error': 'probe port not opened'},
                        'openai': {'ok': False, 'error': 'probe port not opened', 'mode': args.openai_mode},
                        'healthy': False,
                        'startup_error': stderr or None,
                    })
                    continue

                tg = check_telegram(proxy_url, token, args.timeout)
                if args.openai_mode == 'full':
                    oa = check_codex_full(proxy_url, codex_command, args.codex_timeout)
                else:
                    oa = check_openai_transport(proxy_url, transport_url, args.timeout)
                oa['mode'] = args.openai_mode
                results.append({
                    'name': cfg['name'],
                    'tag': cfg.get('source_outbound_tag') or cfg.get('tags', [''])[0],
                    'server': cfg.get('server'),
                    'probe_started': True,
                    'telegram': tg,
                    'openai': oa,
                    'healthy': bool(tg.get('ok') and oa.get('ok')),
                })
            finally:
                if proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)

    Path(args.out).write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    healthy = [r for r in results if r['healthy']]
    print(json.dumps({'checked': len(results), 'healthy': len(healthy), 'out': args.out, 'openai_mode': args.openai_mode}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
