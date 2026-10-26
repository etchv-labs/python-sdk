import json
import unittest
from unittest import mock

import httpx

from etchv import Etchv

from test_batches import PDF, ZIP, batch, draft

ID = 'ab' * 32
JOB = 'req_' + 'c' * 64


class ActiveContentTests(unittest.TestCase):
    def test_document_requests_send_the_query_only_when_set(self):
        seen = []
        def handle(request):
            seen.append(request.url)
            if request.url.path.endswith('/async'):
                return httpx.Response(202, json={'request_id': JOB, 'status': 'queued'})
            return httpx.Response(200, content=PDF, headers={'content-type': 'application/pdf', 'x-watermark-id': ID})
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            sdk.embed_document(PDF, {'a': 1}, active_content='remove_auto_run', accelerator='gpu')
            sdk.submit_embed('documents', PDF, {'a': 1}, active_content='keep')
            sdk.embed_document(PDF, {'a': 1})
        self.assertEqual(dict(seen[0].params), {'accelerator': 'gpu', 'active_content': 'remove_auto_run'})
        self.assertEqual((seen[1].path, dict(seen[1].params)), ('/watermarks/documents/async', {'active_content': 'keep'}))
        self.assertNotIn('active_content', seen[2].params)

    def test_invalid_values_and_other_media_are_refused_before_any_request(self):
        def handle(request):
            raise AssertionError('no request expected')
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            with self.assertRaisesRegex(ValueError, 'active_content must be "keep" or "remove_auto_run"'):
                sdk.embed_document(PDF, {'a': 1}, active_content='strip')  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValueError, 'PDF documents only'):
                sdk.submit_embed('videos', b'v', {'a': 1}, active_content='keep')
            with self.assertRaisesRegex(ValueError, 'active_content must be'):
                sdk.submit_batch([{'filename': 'a.pdf', 'file': PDF, 'data': {'a': 1}}], active_content='none')  # type: ignore[arg-type]

    @mock.patch('etchv.client.time.sleep')
    def test_batches_send_the_option_and_report_it(self, sleep):
        bodies = []
        def handle(request):
            if request.url.host == 'uploads.etchv.com':
                return httpx.Response(200)
            if request.url.path == '/watermarks/batches':
                bodies.append(json.loads(request.read()))
                return httpx.Response(201, json=draft('a.pdf'))
            if request.url.path == '/watermarks/batches/zip':
                bodies.append(request.read())
            return httpx.Response(202, json=batch('starting', active_content='remove_auto_run'))
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            started = sdk.submit_batch([{'filename': 'a.pdf', 'file': PDF, 'data': {'a': 1}}], active_content='remove_auto_run')
            zipped = sdk.submit_batch_zip(ZIP, [{'filename': 'a.pdf', 'data': {'a': 1}}], active_content='remove_auto_run')
        self.assertEqual(bodies[0]['active_content'], 'remove_auto_run')
        self.assertIn(b'"active_content": "remove_auto_run"', bodies[1])
        self.assertEqual((started.active_content, zipped.active_content), ('remove_auto_run', 'remove_auto_run'))
        # A batch from an API that does not report the option has none.
        with Etchv('test-key', transport=httpx.MockTransport(lambda request: httpx.Response(200, json=batch()))) as sdk:
            self.assertIsNone(sdk.get_batch(batch()['batch_id']).active_content)


if __name__ == '__main__':
    unittest.main()
