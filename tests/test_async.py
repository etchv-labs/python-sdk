import unittest
import httpx
from etchv import Etchv


class AsyncTests(unittest.TestCase):
    def test_all_media_receipts_do_not_poll(self):
        calls = []
        webhook = 'wh_' + 'a' * 32
        def handle(request):
            calls.append(request.url.path)
            assert request.method == 'POST'
            assert request.url.params['webhook_id'] == webhook
            assert request.headers['Idempotency-Key'] == 'stable_test_key'
            assert b'file-bytes' in request.content
            return httpx.Response(202, json={'request_id': 'req_' + 'b' * 64, 'status': 'queued'})
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as client:
            for media in ('images', 'documents', 'videos'):
                options = dict(webhook_id=webhook, idempotency_key='stable_test_key')
                assert client.submit_embed(media, b'file-bytes', {'asset': 'test'}, **options)['status'] == 'queued'
                assert client.submit_detection(media, b'file-bytes', **options)['status'] == 'queued'
        assert len(calls) == 6
        assert all(path.endswith('/async') for path in calls)
