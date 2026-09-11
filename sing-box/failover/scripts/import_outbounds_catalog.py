#!/usr/bin/env python3
"""Build complete sing-box candidates from a base config and outbound catalog.

The module is also used by singbox_subscription_refresh.py. It never removes
old generated files: the inventory is published only after new files exist.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import ipaddress
import json
import os
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any


class CatalogError(ValueError):
    """Raised when a catalog cannot safely become a candidate inventory."""


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def atomic_write_text(path: Path, contents: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(mode)
        temporary_path.replace(path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def outbound_identity(outbound: dict[str, Any]) -> str:
    """Stable identity independent of the display tag supplied by a provider."""
    normalized = {key: value for key, value in outbound.items() if key != 'tag'}
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def candidate_name(outbound: dict[str, Any]) -> str:
    return f"cfg-{outbound_identity(outbound)[:16]}"


def direct_cidr(value: str) -> str:
    address = ipaddress.ip_address(value)
    return f'{address}/{32 if address.version == 4 else 128}'


def resolve_server_cidrs(outbounds: list[dict[str, Any]]) -> list[str]:
    cidrs: set[str] = set()
    unresolved: list[str] = []
    for outbound in outbounds:
        server = outbound.get('server')
        if not isinstance(server, str) or not server:
            raise CatalogError('outbound has no server')
        try:
            cidrs.add(direct_cidr(server))
            continue
        except ValueError:
            pass
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(server, outbound.get('server_port', 443), type=socket.SOCK_STREAM)
            }
        except OSError as exc:
            raise CatalogError(f'cannot resolve proxy server {server!r}: {exc}') from exc
        if not addresses:
            unresolved.append(server)
            continue
        cidrs.update(direct_cidr(address) for address in addresses)
    if unresolved:
        raise CatalogError(f'cannot resolve proxy servers: {", ".join(sorted(unresolved))}')
    return sorted(cidrs, key=lambda item: (ipaddress.ip_network(item, strict=False).version, item))


def validate_catalog(catalog: Any) -> list[dict[str, Any]]:
    if not isinstance(catalog, dict) or not isinstance(catalog.get('outbounds'), list):
        raise CatalogError('catalog must be a JSON object with an outbounds array')
    outbounds = catalog['outbounds']
    if not outbounds:
        raise CatalogError('catalog contains no outbounds')
    if not all(isinstance(outbound, dict) for outbound in outbounds):
        raise CatalogError('catalog outbounds must be objects')
    if not all(outbound.get('type') == 'vless' for outbound in outbounds):
        raise CatalogError('only VLESS outbounds are supported by this importer')
    return copy.deepcopy(outbounds)


def build_candidates(
    base: dict[str, Any],
    catalog: Any,
    generated_dir: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    outbounds = validate_catalog(catalog)
    direct_rule = next(
        (
            rule
            for rule in base.get('route', {}).get('rules', [])
            if rule.get('outbound') == 'direct' and isinstance(rule.get('ip_cidr'), list)
        ),
        None,
    )
    if direct_rule is None:
        raise CatalogError('base config has no direct route rule with ip_cidr')

    non_proxy_outbounds = [
        outbound for outbound in base.get('outbounds', [])
        if outbound.get('tag') not in {'proxy-out', 'proxy-fallback-nl'}
    ]
    if not non_proxy_outbounds:
        raise CatalogError('base config has no non-proxy outbound')

    existing_cidrs = list(direct_rule['ip_cidr'])
    endpoint_cidrs = resolve_server_cidrs(outbounds)
    merged_cidrs = list(dict.fromkeys(existing_cidrs + endpoint_cidrs))

    configs: list[dict[str, Any]] = []
    candidate_documents: dict[str, dict[str, Any]] = {}
    for index, source_outbound in enumerate(outbounds):
        source_tag = str(source_outbound.get('tag') or f'subscription-{index + 1}')
        name = candidate_name(source_outbound)
        path = generated_dir / f'{name}.json'
        candidate = copy.deepcopy(base)
        proxy_outbound = copy.deepcopy(source_outbound)
        proxy_outbound['tag'] = 'proxy-out'
        candidate['outbounds'] = [proxy_outbound] + copy.deepcopy(non_proxy_outbounds)
        candidate.setdefault('route', {})['final'] = 'proxy-out'
        for rule in candidate['route'].get('rules', []):
            if rule.get('outbound') == 'direct' and isinstance(rule.get('ip_cidr'), list):
                rule['ip_cidr'] = merged_cidrs
                break
        candidate_documents[name] = candidate
        configs.append({
            'name': name,
            'path': str(path),
            'priority': max(1, 100 - index),
            'enabled': True,
            'tags': [source_tag],
            'source_outbound_tag': source_tag,
            'server': proxy_outbound.get('server'),
            'server_port': proxy_outbound.get('server_port'),
        })
    return configs, candidate_documents


def write_candidates(candidate_documents: dict[str, dict[str, Any]], generated_dir: Path) -> None:
    for name, candidate in candidate_documents.items():
        output = generated_dir / f'{name}.json'
        atomic_write_text(output, json.dumps(candidate, ensure_ascii=False, indent=2) + '\n')


def load_runtime(path: Path | None, fallback: dict[str, Any]) -> dict[str, Any]:
    if path is None:
        return fallback
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise CatalogError('PyYAML is required to preserve runtime from inventory') from exc
    document = yaml.safe_load(path.read_text(encoding='utf-8'))
    runtime = document.get('runtime') if isinstance(document, dict) else None
    if not isinstance(runtime, dict):
        raise CatalogError('runtime section is missing in existing inventory')
    return copy.deepcopy(runtime)


def build_inventory_yaml(configs: list[dict[str, Any]], runtime: dict[str, Any]) -> str:
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise CatalogError('PyYAML is required to write inventory') from exc
    return yaml.safe_dump(
        {'configs': configs, 'runtime': runtime},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


def default_runtime(args: argparse.Namespace) -> dict[str, Any]:
    return {
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
        'lock_path': '/run/singbox-failover.lock',
    }


def main() -> int:
    parser = argparse.ArgumentParser(description='Import a VLESS outbound catalog into complete sing-box candidates')
    parser.add_argument('--base-config', required=True)
    parser.add_argument('--outbounds-catalog', required=True)
    parser.add_argument('--generated-dir', required=True)
    parser.add_argument('--inventory-out', required=True)
    parser.add_argument('--runtime-from', help='existing inventory whose runtime section must be preserved')
    parser.add_argument('--active-config-path', default='/etc/sing-box/config.json')
    parser.add_argument('--restart-command', default='sudo -n systemctl restart sing-box-tun')
    parser.add_argument('--check-command', default='/usr/local/bin/sing-box check -c {config_path}')
    parser.add_argument('--service-name', default='sing-box-tun')
    parser.add_argument('--state-path', default='/home/petrovov/configs/state.json')
    parser.add_argument('--log-path', default='/home/petrovov/configs/failover.log')
    args = parser.parse_args()

    try:
        base = load_json(Path(args.base_config))
        configs, documents = build_candidates(base, load_json(Path(args.outbounds_catalog)), Path(args.generated_dir))
        runtime = load_runtime(Path(args.runtime_from) if args.runtime_from else None, default_runtime(args))
        write_candidates(documents, Path(args.generated_dir))
        atomic_write_text(Path(args.inventory_out), build_inventory_yaml(configs, runtime))
    except (CatalogError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f'import failed: {exc}', file=sys.stderr)
        return 1
    print(json.dumps({'generated': len(configs), 'inventory': args.inventory_out}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
