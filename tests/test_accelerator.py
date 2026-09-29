import unittest
from unittest import mock
import httpx
from etchv import Etchv, EtchvError, RateLimitError

PNG = b'\x89PNG\r\n\x1a\nimage'
ID = 'ab' * 32
JOB = 'req_' + 'c' * 64


class AcceleratorTests(unittest.TestCase):
    def test_query_only_when_set_and_header_surfaced(self):
        seen = []
        def handle(request):
            seen.append(request.url)
            if request.url.path.endswith('/detect'):
                return httpx.Response(200, json={'watermarked': False, 'confidence': .5, 'watermark_id': None},
                                      headers={'x-etchv-accelerator': 'gpu'})
            return httpx.Response(200, content=PNG, headers={'content-type': 'image/png', 'x-watermark-id': ID,
                                                             'x-etchv-accelerator': 'cpu'})
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            self.assertEqual(sdk.embed_image(PNG, {'a': 1}, accelerator='gpu').accelerator, 'cpu')
            self.assertEqual(sdk.detect_image(PNG, accelerator='gpu').accelerator, 'gpu')
            sdk.embed_image(PNG, {'a': 1}, storage_destination_id='dst_' + 'd' * 32, accelerator='cpu')
            sdk.detect_document(b'%PDF-1.7')
        self.assertEqual(seen[0].params['accelerator'], 'gpu')
        self.assertEqual(seen[1].params['accelerator'], 'gpu')
        self.assertEqual(dict(seen[2].params), {'storage_destination_id': 'dst_' + 'd' * 32, 'accelerator': 'cpu'})
        self.assertNotIn('accelerator', seen[3].params)

    def test_async_receipts_and_invalid_value(self):
        seen = []
        def handle(request):
            seen.append(request.url)
            return httpx.Response(202, json={'request_id': JOB, 'status': 'queued', 'accelerator_requested': 'gpu'})
        webhook = 'wh_' + 'a' * 32
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            receipt = sdk.submit_embed('videos', b'v', {'a': 1}, webhook_id=webhook, accelerator='gpu')
            self.assertEqual(receipt['accelerator_requested'], 'gpu')
            sdk.submit_detection('images', b'i', accelerator='gpu')
            with self.assertRaises(ValueError):
                sdk.embed_image(PNG, {'a': 1}, accelerator='tpu')  # type: ignore[arg-type]
        self.assertEqual(dict(seen[0].params), {'webhook_id': webhook, 'accelerator': 'gpu'})
        self.assertEqual(seen[1].path, '/watermarks/images/detect/async')
        self.assertEqual(seen[1].params['accelerator'], 'gpu')
        self.assertEqual(len(seen), 2)

    def test_video_detection_with_accelerator_polls_detection_job(self):
        calls = []
        def handle(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                self.assertEqual(request.url.params['accelerator'], 'gpu')
                return httpx.Response(202, json={'request_id': JOB}, headers={'retry-after': '0.01'})
            return httpx.Response(200, json={'watermarked': False, 'confidence': .5, 'watermark_id': None,
                                             'accelerator_requested': 'gpu', 'accelerator': 'cpu'})
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            self.assertEqual(sdk.detect_video(b'video', accelerator='gpu').accelerator, 'cpu')
        self.assertEqual(calls, ['/watermarks/videos/detect', f'/watermarks/detection-jobs/{JOB}/result'])

    def test_unknown_accelerator_is_ignored(self):
        def handle(request):
            return httpx.Response(200, content=PNG, headers={'content-type': 'image/png', 'x-watermark-id': ID,
                                                             'x-etchv-accelerator': 'quantum'})
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            self.assertIsNone(sdk.embed_image(PNG, {'a': 1}).accelerator)


class RateLimitRetryTests(unittest.TestCase):
    def test_durable_429_honors_retry_after(self):
        calls = []
        def handle(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(429, json={'detail': {'code': 'concurrency_limited'}}, headers={'retry-after': '0.02'})
            return httpx.Response(200, content=PNG, headers={'content-type': 'image/png', 'x-watermark-id': ID})
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            self.assertEqual(sdk.embed_image(PNG, {'a': 1}).image, PNG)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].headers['idempotency-key'], calls[1].headers['idempotency-key'])

    def test_retry_after_wait_is_capped(self):
        responses = [httpx.Response(429, json={'detail': {'code': 'rate_limited'}}, headers={'retry-after': '3600'}),
                     httpx.Response(429, json={'detail': {'code': 'rate_limited'}}, headers={'retry-after': 'soon'}),
                     httpx.Response(200, content=PNG, headers={'content-type': 'image/png', 'x-watermark-id': ID})]
        waits = []
        with mock.patch('etchv.client.time.sleep', waits.append):
            with Etchv('test-key', timeout=60, transport=httpx.MockTransport(lambda r: responses.pop(0))) as sdk:
                self.assertEqual(sdk.embed_image(PNG, {'a': 1}).image, PNG)
        self.assertEqual(len(waits), 2)
        self.assertAlmostEqual(waits[0], 5, places=3)
        self.assertAlmostEqual(waits[1], 1, places=3)

    def test_rate_limit_error_exposes_code_message_and_retry_after(self):
        body = {'detail': {'message': 'Too many requests', 'code': 'concurrency_limited', 'limit': 2}}
        def handle(request):
            return httpx.Response(429, json=body, headers={'retry-after': '7', 'x-request-id': 'req_limited'})
        with Etchv('test-key', transport=httpx.MockTransport(handle)) as sdk:
            with self.assertRaises(RateLimitError) as caught:
                sdk.detect_image(PNG)
        error = caught.exception
        self.assertEqual((error.code, error.message, error.limit, error.retry_after), ('concurrency_limited', 'Too many requests', 2, 7))
        self.assertEqual(error.detail, body)
        self.assertEqual(str(error), 'Etchv request failed (HTTP 429): Too many requests; request ID req_limited')
        with Etchv('test-key', transport=httpx.MockTransport(lambda r: httpx.Response(403, json={'detail': 'plain'}))) as sdk:
            with self.assertRaises(EtchvError) as caught:
                sdk.detect_image(PNG)
        self.assertEqual((caught.exception.code, caught.exception.message, caught.exception.limit, caught.exception.retry_after), (None, 'plain', None, None))
        self.assertNotIn('plain', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
