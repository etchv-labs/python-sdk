import json
import unittest
from datetime import datetime, timedelta, timezone

import httpx

from etchv import Etchv, PaymentRequiredError

AST = 'ast_' + 'e' * 64
LNK = 'lnk_' + 'a' * 32
LEV = 'lev_' + 'b' * 32
WH = 'wh_' + 'c' * 32
WID = 'ab' * 32
LINK = {'id': LNK, 'url': 'https://etchv.com/l/Yp3kQ8vN2rT6wX1zA4bC7d', 'name': 'Board pack', 'status': 'active', 'recipients': []}


def client(handler, **options):
    return Etchv('test-key', transport=httpx.MockTransport(handler), **options)


class LinkTests(unittest.TestCase):
    def test_create_sends_the_body_and_one_idempotency_key_across_retries(self):
        calls = []
        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(503, json={'detail': 'busy'})
            return httpx.Response(201, json=LINK)
        expires = datetime(2026, 11, 26, 14, 2, tzinfo=timezone.utc)
        with client(handle) as sdk:
            link = sdk.create_link(AST, 'Board pack', recipients=['alex@example.com'], expires_at=expires,
                                   prepare='on_open', download_limit=3, webhook_id=WH)
        self.assertEqual(link['url'], LINK['url'])
        self.assertEqual([(r.method, r.url.path) for r in calls], [('POST', '/links')] * 2)
        self.assertEqual(calls[0].headers['idempotency-key'], calls[1].headers['idempotency-key'])
        self.assertEqual(json.loads(calls[1].content), {
            'asset_id': AST, 'name': 'Board pack', 'recipients': ['alex@example.com'],
            'expires_at': '2026-11-26T14:02:00+00:00', 'prepare': 'on_open', 'download_limit': 3, 'webhook_id': WH})

    def test_create_for_domains_with_a_given_key_and_only_what_was_set(self):
        sent = []
        def handle(request):
            sent.append(request)
            return httpx.Response(200, json=LINK)  # a replay
        with client(handle) as sdk:
            sdk.create_link(AST, 'Board pack', allowed_domains=['example.com'], idempotency_key='board-pack-2026-10')
        self.assertEqual(sent[0].headers['idempotency-key'], 'board-pack-2026-10')
        self.assertEqual(json.loads(sent[0].content), {'asset_id': AST, 'name': 'Board pack', 'allowed_domains': ['example.com']})

    def test_not_enough_credits_is_payment_required_with_the_figures(self):
        detail = {'detail': {'code': 'insufficient_credits', 'message': 'This link needs 8 credits for its copies; 2 are available',
                             'estimated_credits': 8, 'available_credits': 2}}
        with client(lambda r: httpx.Response(402, json=detail)) as sdk:
            with self.assertRaises(PaymentRequiredError) as caught:
                sdk.create_link(AST, 'Board pack', recipients=['alex@example.com', 'sam@example.com'])
        self.assertEqual(caught.exception.code, 'insufficient_credits')

    def test_invalid_input_is_refused_before_any_request(self):
        def handle(request):
            raise AssertionError('no request expected')
        with client(handle) as sdk:
            for kwargs in [dict(), dict(recipients=['a@b.co'], allowed_domains=['b.co']), dict(recipients='a@b.co'),
                           dict(recipients=[]), dict(recipients=['a@b.co'], prepare='later'),
                           dict(recipients=['a@b.co'], download_limit=0), dict(recipients=['a@b.co'], download_limit=True),
                           dict(recipients=['a@b.co'], expires_at=datetime(2026, 11, 26)),
                           dict(recipients=['a@b.co'], webhook_id='wh_1'), dict(recipients=['a@b.co'], idempotency_key='short')]:
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    sdk.create_link(AST, 'Board pack', **kwargs)
            for asset_id, name in [('ast_1', 'Board pack'), (AST, ''), (AST, 'x' * 201)]:
                with self.assertRaises(ValueError):
                    sdk.estimate_link(asset_id, name, recipients=['a@b.co'])
            for call in [lambda: sdk.get_link('lnk_1'), lambda: sdk.list_links(limit=51), lambda: sdk.list_links(before='bat_1'),
                         lambda: sdk.list_link_events(LNK, limit=0), lambda: sdk.list_link_events(LNK, before=LNK)]:
                with self.assertRaises(ValueError):
                    call()

    def test_estimate_list_read_events_and_revoke(self):
        seen = []
        def handle(request):
            seen.append((request.method, request.url.path, dict(request.url.params),
                         json.loads(request.content) if request.content else None))
            if request.url.path == '/links/estimate':
                return httpx.Response(200, json={'estimated_credits': 8, 'available_credits': 100, 'enough_credits': True})
            if request.url.path.endswith('/events'):
                return httpx.Response(200, json={'data': [{'id': LEV, 'type': 'opened'}], 'next_cursor': None})
            if request.url.path == '/links':
                return httpx.Response(200, json={'data': [LINK], 'next_cursor': LNK})
            return httpx.Response(200, json={**LINK, 'status': 'revoked' if request.method == 'POST' else 'active'})
        soon = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        with client(handle) as sdk:
            self.assertTrue(sdk.estimate_link(AST, 'Board pack', recipients=['a@b.co'], expires_at=soon)['enough_credits'])
            self.assertEqual(sdk.list_links(limit=5, before=LNK)['next_cursor'], LNK)
            self.assertEqual(sdk.get_link(LNK)['status'], 'active')
            self.assertEqual(sdk.list_link_events(LNK, before=LEV)['data'][0]['type'], 'opened')
            self.assertEqual(sdk.revoke_link(LNK)['status'], 'revoked')
        self.assertEqual(seen, [
            ('POST', '/links/estimate', {}, {'asset_id': AST, 'name': 'Board pack', 'recipients': ['a@b.co'], 'expires_at': soon}),
            ('GET', '/links', {'limit': '5', 'before': LNK}, None),
            ('GET', f'/links/{LNK}', {}, None),
            ('GET', f'/links/{LNK}/events', {'limit': '50', 'before': LEV}, None),
            ('POST', f'/links/{LNK}/revoke', {}, None),
        ])


class DetectionRecordTests(unittest.TestCase):
    def test_detection_names_the_copy_or_asset_when_present(self):
        copy = {'asset_id': AST, 'link': {'id': LNK, 'name': 'Board pack'}, 'recipient': 'alex@example.com', 'opened_at': None}
        bodies = [{'watermarked': True, 'confidence': .9, 'watermark_id': WID, 'copy': copy},
                  {'watermarked': True, 'confidence': .9, 'watermark_id': WID, 'asset': {'asset_id': AST, 'name': 'a.png'}},
                  {'watermarked': True, 'confidence': .9, 'watermark_id': WID}]
        results = []
        for body in bodies:
            with client(lambda r, body=body: httpx.Response(200, json=body)) as sdk:
                results.append(sdk.detect_image(b'\x89PNG\r\n\x1a\n' + b'0' * 32))
        self.assertEqual((results[0].copy, results[0].asset), (copy, None))
        self.assertEqual((results[1].copy, results[1].asset['name']), (None, 'a.png'))
        self.assertEqual((results[2].copy, results[2].asset), (None, None))
        self.assertEqual(hash(results[0]), hash(results[2]))  # results stay hashable with a record

    def test_an_async_detection_result_names_the_copy_too(self):
        req = 'req_' + 'f' * 64
        copy = {'asset_id': AST, 'link': {'id': LNK, 'name': 'Board pack'}, 'recipient': 'alex@example.com', 'opened_at': None}
        def handle(request):
            self.assertEqual(request.url.path, f'/watermarks/detection-jobs/{req}/result')
            return httpx.Response(200, json={'watermarked': True, 'confidence': .9, 'watermark_id': WID, 'copy': copy})
        with client(handle) as sdk:
            self.assertEqual(sdk.get_detection_result(req).copy, copy)


if __name__ == '__main__':
    unittest.main()
