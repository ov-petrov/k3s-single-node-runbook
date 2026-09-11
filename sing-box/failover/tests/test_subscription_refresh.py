from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

from import_outbounds_catalog import build_candidates, outbound_identity
from singbox_subscription_refresh import SubscriptionError, parse_subscription_payload


class SubscriptionParserTest(unittest.TestCase):
    def test_parses_base64_vless_reality_grpc(self) -> None:
        uri = (
            'vless://11111111-1111-1111-1111-111111111111@edge.example:443'
            '?security=reality&sni=cdn.example&fp=firefox&pbk=public-key&sid=abcd'
            '&type=grpc&serviceName=grpc-service&flow=xtls-rprx-vision#Moscow'
        )
        payload = base64.b64encode(uri.encode('utf-8'))

        catalog, summary = parse_subscription_payload(payload)

        self.assertEqual(summary, {'format': 'base64-vless-uri-list', 'skipped': 0})
        outbound = catalog['outbounds'][0]
        self.assertEqual(outbound['server'], 'edge.example')
        self.assertEqual(outbound['server_port'], 443)
        self.assertEqual(outbound['flow'], 'xtls-rprx-vision')
        self.assertEqual(outbound['transport'], {'type': 'grpc', 'service_name': 'grpc-service'})
        self.assertEqual(outbound['tls']['server_name'], 'cdn.example')
        self.assertEqual(outbound['tls']['utls']['fingerprint'], 'firefox')
        self.assertEqual(outbound['tls']['reality'], {'enabled': True, 'public_key': 'public-key', 'short_id': 'abcd'})

    def test_keeps_vless_and_skips_other_protocols(self) -> None:
        payload = (
            'trojan://unrelated@server.example:443\n'
            'vless://22222222-2222-2222-2222-222222222222@198.51.100.10:8443'
            '?security=none&type=tcp#direct\n'
        ).encode('utf-8')

        catalog, summary = parse_subscription_payload(payload)

        self.assertEqual(len(catalog['outbounds']), 1)
        self.assertEqual(catalog['outbounds'][0]['server'], '198.51.100.10')
        self.assertEqual(summary, {'format': 'vless-uri-list', 'skipped': 1})

    def test_parses_singbox_profile_list(self) -> None:
        payload = json.dumps([
            {
                'remarks': 'first profile',
                'outbounds': [
                    {'type': 'direct', 'tag': 'direct'},
                    {
                        'type': 'vless',
                        'tag': 'first label',
                        'server': 'edge-one.example',
                        'server_port': 443,
                        'uuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                    },
                ],
            },
            {
                'remarks': 'duplicate profile label',
                'outbounds': [
                    {
                        'type': 'vless',
                        'tag': 'renamed label',
                        'server': 'edge-one.example',
                        'server_port': 443,
                        'uuid': 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa',
                    },
                    {
                        'type': 'vless',
                        'tag': 'second label',
                        'server': 'edge-two.example',
                        'server_port': 8443,
                        'uuid': 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb',
                    },
                ],
            },
        ]).encode('utf-8')

        catalog, summary = parse_subscription_payload(payload)

        self.assertEqual(summary, {'format': 'sing-box-json-profile-list', 'skipped': 2})
        self.assertEqual(len(catalog['outbounds']), 2)
        self.assertEqual(catalog['outbounds'][0]['server'], 'edge-one.example')
        self.assertEqual(catalog['outbounds'][1]['server'], 'edge-two.example')

    def test_rejects_payload_without_supported_entries(self) -> None:
        with self.assertRaises(SubscriptionError):
            parse_subscription_payload(b'trojan://password@example.com:443')

    def test_identity_does_not_depend_on_provider_label(self) -> None:
        first = {
            'type': 'vless',
            'tag': 'Moscow',
            'server': 'edge.example',
            'server_port': 443,
            'uuid': '33333333-3333-3333-3333-333333333333',
        }
        second = {**first, 'tag': 'New label'}

        self.assertEqual(outbound_identity(first), outbound_identity(second))

    def test_candidates_keep_stable_names_and_bypass_all_proxy_endpoints(self) -> None:
        base = {
            'outbounds': [
                {'type': 'vless', 'tag': 'proxy-out', 'server': 'old.example'},
                {'type': 'direct', 'tag': 'direct'},
            ],
            'route': {
                'rules': [
                    {'ip_cidr': ['10.0.0.0/8'], 'outbound': 'direct'},
                ],
            },
        }
        catalog = {
            'outbounds': [
                {
                    'type': 'vless',
                    'tag': 'one',
                    'server': '198.51.100.10',
                    'server_port': 443,
                    'uuid': '44444444-4444-4444-4444-444444444444',
                },
                {
                    'type': 'vless',
                    'tag': 'two',
                    'server': '2001:db8::10',
                    'server_port': 443,
                    'uuid': '55555555-5555-5555-5555-555555555555',
                },
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            configs, documents = build_candidates(base, catalog, Path(directory))

        self.assertEqual(len(configs), 2)
        self.assertTrue(all(config['name'].startswith('cfg-') for config in configs))
        direct_cidrs = documents[configs[0]['name']]['route']['rules'][0]['ip_cidr']
        self.assertEqual(direct_cidrs, ['10.0.0.0/8', '198.51.100.10/32', '2001:db8::10/128'])


if __name__ == '__main__':
    unittest.main()
