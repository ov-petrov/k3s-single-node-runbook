#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def slugify(text: str) -> str:
    allowed: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            allowed.append(ch)
        elif ch in (' ', '-', '_', '§'):
            allowed.append('-')
    slug = ''.join(allowed)
    while '--' in slug:
        slug = slug.replace('--', '-')
    return slug.strip('-') or 'config'


def yaml_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def build_inventory_yaml(configs: list[dict[str, Any]], runtime: dict[str, Any]) -> str:
    lines = ['configs:']
    for cfg in configs:
        lines.append(f"  - name: {cfg['name']}")
        lines.append(f"    path: {yaml_quote(cfg['path'])}")
        lines.append(f"    priority: {cfg['priority']}")
        lines.append(f"    enabled: {'true' if cfg['enabled'] else 'false'}")
        lines.append('    tags:')
        for tag in cfg['tags']:
            lines.append(f"      - {yaml_quote(tag)}")
        lines.append(f"    source_outbound_tag: {yaml_quote(cfg['source_outbound_tag'])}")
        lines.append(f"    server: {yaml_quote(cfg['server'])}")
        lines.append(f"    server_port: {cfg['server_port']}")
    lines.append('runtime:')
    for k, v in runtime.items():
        if isinstance(v, bool):
            sval = 'true' if v else 'false'
        elif isinstance(v, int):
            sval = str(v)
        else:
            sval = yaml_quote(v)
        lines.append(f"  {k}: {sval}")
    return '\n'.join(lines) + '\n'


def main() -> int:
    parser = argparse.ArgumentParser(description='Import sing-box outbounds catalog into full candidate configs + inventory')
    parser.add_argument('--base-config', required=True)
    parser.add_argument('--outbounds-catalog', required=True)
    parser.add_argument('--generated-dir', required=True)
    parser.add_argument('--inventory-out', required=True)
    parser.add_argument('--active-config-path', default='/etc/sing-box/config.json')
    parser.add_argument('--restart-command', default='sudo -n systemctl restart sing-box-tun')
    parser.add_argument('--check-command', default='/usr/local/bin/sing-box check -c {config_path}')
    parser.add_argument('--service-name', default='sing-box-tun')
    parser.add_argument('--state-path', default='/home/petrovov/configs/state.json')
    parser.add_argument('--log-path', default='/home/petrovov/configs/failover.log')
    args = parser.parse_args()

    base = load_json(Path(args.base_config))
    catalog = load_json(Path(args.outbounds_catalog))
    outbounds = catalog['outbounds']
    generated_dir = Path(args.generated_dir)
    generated_dir.mkdir(parents=True, exist_ok=True)

    base_outbounds = base.get('outbounds', [])
    non_proxy_outbounds = [o for o in base_outbounds if o.get('tag') not in {'proxy-out', 'proxy-fallback-nl'}]

    base_rules = base.get('route', {}).get('rules', [])
    direct_rule = next((r for r in base_rules if r.get('outbound') == 'direct' and 'ip_cidr' in r), None)
    existing_ip_cidrs = list(direct_rule.get('ip_cidr', [])) if direct_rule else []
    server_ip_cidrs = sorted({f"{ob['server']}/32" for ob in outbounds if 'server' in ob})

    merged_ip_cidrs: list[str] = []
    for item in existing_ip_cidrs + server_ip_cidrs:
        if item not in merged_ip_cidrs:
            merged_ip_cidrs.append(item)

    configs: list[dict[str, Any]] = []
    for idx, outbound in enumerate(outbounds):
        tag = outbound['tag']
        path = generated_dir / f"{idx:02d}-{slugify(tag)}.json"
        candidate = json.loads(json.dumps(base))
        outbound_copy = json.loads(json.dumps(outbound))
        outbound_copy['tag'] = 'proxy-out'
        candidate['outbounds'] = [outbound_copy] + json.loads(json.dumps(non_proxy_outbounds))
        candidate.setdefault('route', {})['final'] = 'proxy-out'
        for rule in candidate.setdefault('route', {}).get('rules', []):
            if rule.get('outbound') == 'direct' and 'ip_cidr' in rule:
                rule['ip_cidr'] = merged_ip_cidrs
                break
        path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        configs.append({
            'name': f'cfg-{idx:02d}',
            'path': str(path),
            'priority': max(1, 100 - idx),
            'enabled': True,
            'tags': [tag],
            'source_outbound_tag': tag,
            'server': outbound.get('server'),
            'server_port': outbound.get('server_port'),
        })

    runtime = {
        'active_config_path': args.active_config_path,
        'switch_method': 'copy-and-restart',
        'singbox_service_name': args.service_name,
        'singbox_check_command': args.check_command,
        'singbox_restart_command': args.restart_command,
        'stabilization_delay_seconds': 5,
        'request_timeout_seconds': 10,
        'cooldown_seconds': 900,
        'consecutive_failures_before_switch': 2,
        'require_both_apis': True,
        'state_path': args.state_path,
        'log_path': args.log_path,
    }

    Path(args.inventory_out).write_text(build_inventory_yaml(configs, runtime), encoding='utf-8')
    print(json.dumps({'generated': len(configs), 'inventory': args.inventory_out}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
