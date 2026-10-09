import io
import itertools
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import httpx

from etchv import Batch, BatchSubmitError, ConflictError, DeadlineExceededError, Etchv, EtchvError, GoneError

BATCH = 'bat_' + 'b' * 32
ID = 'ab' * 32
PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 50
PDF = b'%PDF-1.7\n' + b'y' * 80
ZIP = b'PK\x03\x04' + b'z' * 40


def request_id(n):
    return 'req_' + str(n) * 64


def upload_url(index):
    return f'https://uploads.etchv.com/image/upl_{index:032d}.png?Expires=1&Signature=s&Key-Pair-Id=K1'


def item(index, filename, status='pending', **extra):
    return {'index': index, 'filename': filename, 'size': 1, 'upload_id': f'upl_{index:032d}', 'request_id': None,
            'status': status, 'error_code': None, 'error_detail': None, 'credits': None, **extra}


def batch(status='draft', items=(), **extra):
    return {'batch_id': BATCH, 'status': status, 'item_count': len(items), 'archive': False, 'accelerator': 'cpu',
            'webhook_id': None, 'storage_destination_id': None,
            'counts': {'pending': 0, 'accepted': 0, 'rejected': 0, 'succeeded': 0, 'failed': 0, 'in_progress': 0},
            'credits': {'reserved': 0, 'charged': 0, 'refunded': 0}, 'cancel_requested': False,
            'created_at': '2026-10-09T00:00:00+00:00', 'started_at': None, 'completed_at': None,
            'upload_expires_at': '2026-10-10T00:00:00+00:00', 'status_url': f'/watermarks/batches/{BATCH}',
            'items': list(items), **extra}


def draft(*names):
    return batch(items=[item(i, name, upload={'method': 'PUT', 'url': upload_url(i), 'expires_at': 'x'})
                        for i, name in enumerate(names)])


def client(handler, **options):
    return Etchv('test-key', transport=httpx.MockTransport(handler), **options)


def clock():
    """A monotonic clock that advances one second per read."""
    ticks = itertools.count()
    return mock.patch('etchv.client.time.monotonic', side_effect=lambda: next(ticks))


@mock.patch('etchv.client.time.sleep')
class BatchTests(unittest.TestCase):
    def test_create_upload_start_without_the_api_key_on_uploads(self, sleep):
        calls, puts, lock = [], {}, threading.Lock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'contract.pdf')
            path.write_bytes(PDF)

            def handle(request):
                with lock:
                    calls.append((request.method, request.url.host, request.url.path))
                if request.url.host == 'uploads.etchv.com':
                    self.assertNotIn('x-api-key', request.headers)
                    self.assertNotIn('authorization', request.headers)
                    with lock:
                        puts[request.url.path] = request.read()
                    return httpx.Response(200)
                self.assertEqual(request.headers['x-api-key'], 'test-key')
                if request.url.path == '/watermarks/batches':
                    body = json.loads(request.read())
                    self.assertRegex(request.headers['idempotency-key'], r'^[a-f0-9]{32}$')
                    self.assertEqual(body, {'items': [{'filename': 'photo.png', 'size': len(PNG), 'data': {'recipient': 'a'}},
                                                      {'filename': 'contract.pdf', 'size': len(PDF), 'data': {'recipient': 'b'}}],
                                            'archive': True, 'accelerator': 'gpu'})
                    return httpx.Response(201, json=draft('photo.png', 'contract.pdf'))
                self.assertEqual(request.url.path, f'/watermarks/batches/{BATCH}/start')
                return httpx.Response(202, json=batch('starting'), headers={'retry-after': '2'})

            with client(handle) as sdk:
                result = sdk.submit_batch([{'filename': 'photo.png', 'file': PNG, 'data': {'recipient': 'a'}},
                                           {'filename': 'contract.pdf', 'file': path, 'data': {'recipient': 'b'}}],
                                          archive=True, accelerator='gpu')
        self.assertIsInstance(result, Batch)
        self.assertEqual((result.batch_id, result.status, result.done), (BATCH, 'starting', False))
        self.assertEqual(puts, {f'/image/upl_{0:032d}.png': PNG, f'/image/upl_{1:032d}.png': PDF})
        self.assertEqual(calls[0], ('POST', 'api.etchv.com', '/watermarks/batches'))
        self.assertEqual(calls[-1], ('POST', 'api.etchv.com', f'/watermarks/batches/{BATCH}/start'))
        self.assertEqual(len(calls), 4)

    def test_create_retries_reuse_the_idempotency_key(self, sleep):
        keys = []
        def handle(request):
            if request.url.path == '/watermarks/batches':
                keys.append(request.headers['idempotency-key'])
                if len(keys) == 1:
                    return httpx.Response(502, json={'detail': 'busy'})
                if len(keys) == 2:
                    raise httpx.ConnectError('reset')
                return httpx.Response(201, json=draft('photo.png'))
            if request.url.host == 'uploads.etchv.com':
                return httpx.Response(200)
            return httpx.Response(202, json=batch('starting'))
        with client(handle) as sdk:
            sdk.submit_batch([{'filename': 'photo.png', 'file': PNG, 'data': {'a': 1}}])
            self.assertEqual(len(keys), 3)
            self.assertEqual(len(set(keys)), 1)
            keys.clear()
            sdk.submit_batch([{'filename': 'photo.png', 'file': PNG, 'data': {'a': 1}}], idempotency_key='batch-0001')
        self.assertEqual(keys, ['batch-0001'] * 3)

    def test_uploads_unavailable_raises_at_once(self, sleep):
        calls = []
        def handle(request):
            calls.append(request.url.path)
            return httpx.Response(503, json={'detail': 'Batch uploads are unavailable; send one zip to /watermarks/batches/zip instead'})
        with client(handle) as sdk:
            with self.assertRaises(EtchvError) as error:
                sdk.submit_batch([{'filename': 'photo.png', 'file': PNG, 'data': {'a': 1}}])
        self.assertEqual((error.exception.status_code, calls), (503, ['/watermarks/batches']))

    def test_a_replayed_started_batch_is_returned_without_uploading(self, sleep):
        calls = []
        def handle(request):
            calls.append(request.url.path)
            return httpx.Response(200, json=batch('processing', [item(0, 'photo.png', 'queued')]))
        with client(handle) as sdk:
            result = sdk.submit_batch([{'filename': 'photo.png', 'file': PNG, 'data': {'a': 1}}], idempotency_key='batch-0001')
        self.assertEqual((result.status, calls), ('processing', ['/watermarks/batches']))

    def test_a_refused_upload_reports_how_to_resume(self, sleep):
        paths = []
        def handle(request):
            paths.append(request.url.path)
            if request.url.path == '/watermarks/batches':
                return httpx.Response(201, json=draft('photo.png'))
            return httpx.Response(403, text='Forbidden')
        with client(handle) as sdk:
            with self.assertRaises(BatchSubmitError) as error:
                sdk.submit_batch([{'filename': 'photo.png', 'file': PNG, 'data': {'a': 1}}], idempotency_key='batch-0001')
        self.assertEqual((error.exception.status_code, error.exception.code), (403, 'batch_upload_failed'))
        self.assertEqual((error.exception.batch_id, error.exception.idempotency_key), (BATCH, 'batch-0001'))
        self.assertEqual((error.exception.index, error.exception.filename), (0, 'photo.png'))
        self.assertIn("idempotency_key='batch-0001'", str(error.exception))
        self.assertNotIn(f'/watermarks/batches/{BATCH}/start', paths)

    def test_network_failures_after_the_deadline_still_say_how_to_resume(self, sleep):
        def handle(request):
            if request.url.path == '/watermarks/batches':
                return httpx.Response(201, json=draft('photo.png'))
            raise httpx.WriteTimeout('stalled')
        with clock(), client(handle, timeout=5) as sdk:
            with self.assertRaises(BatchSubmitError) as error:
                sdk.submit_batch([{'filename': 'photo.png', 'file': PNG, 'data': {'a': 1}}])
        self.assertEqual(error.exception.status_code, 0)
        self.assertEqual(error.exception.batch_id, BATCH)
        self.assertRegex(error.exception.idempotency_key, r'^[a-f0-9]{32}$')
        self.assertIsInstance(error.exception.__cause__, httpx.WriteTimeout)

    def test_unreadable_files_say_how_to_resume(self, sleep):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'gone.png')
            path.write_bytes(PNG)
            def handle(request):
                path.unlink()  # The file disappears after the batch was created.
                return httpx.Response(201, json=draft('gone.png'))
            with client(handle) as sdk:
                with self.assertRaises(BatchSubmitError) as error:
                    sdk.submit_batch([{'filename': 'gone.png', 'file': path, 'data': {'a': 1}}], idempotency_key='batch-0001')
        self.assertEqual((error.exception.batch_id, error.exception.idempotency_key), (BATCH, 'batch-0001'))
        self.assertIsInstance(error.exception.__cause__, FileNotFoundError)

    def test_a_failed_start_says_how_to_resume(self, sleep):
        def handle(request):
            if request.url.path == '/watermarks/batches':
                return httpx.Response(201, json=draft('photo.png'))
            if request.url.host == 'uploads.etchv.com':
                return httpx.Response(200)
            return httpx.Response(500, json={'detail': 'boom'})
        with client(handle) as sdk:
            with self.assertRaises(BatchSubmitError) as error:
                sdk.submit_batch([{'filename': 'photo.png', 'file': PNG, 'data': {'a': 1}}], idempotency_key='batch-0001')
        self.assertEqual((error.exception.code, error.exception.status_code), ('batch_start_failed', 500))
        self.assertEqual(error.exception.idempotency_key, 'batch-0001')

    def test_upload_time_scales_with_file_size(self, sleep):
        # 1.25 MB gets about ten seconds beyond the 5 s client timeout before retries stop.
        big = PNG + b'x' * (10 * 128 * 1024)
        attempts = []
        def handle(request):
            if request.url.path == '/watermarks/batches':
                return httpx.Response(201, json=draft('photo.png'))
            if request.url.host == 'uploads.etchv.com':
                attempts.append(1)
                if len(attempts) < 9:  # Eight failures: about 8 s, past the client timeout.
                    raise httpx.WriteTimeout('slow uplink')
                return httpx.Response(200)
            return httpx.Response(202, json=batch('starting'))
        with clock(), client(handle, timeout=5) as sdk:
            self.assertEqual(sdk.submit_batch([{'filename': 'photo.png', 'file': big, 'data': {'a': 1}}]).status, 'starting')
        self.assertEqual(len(attempts), 9)

    def test_resuming_skips_files_that_already_arrived(self, sleep):
        replay = batch(items=[item(0, 'photo.png', upload_received=True),
                              item(1, 'other.png', upload={'method': 'PUT', 'url': upload_url(1), 'expires_at': 'x'})])
        puts = []
        def handle(request):
            if request.url.path == '/watermarks/batches':
                return httpx.Response(200, json=replay)
            if request.url.host == 'uploads.etchv.com':
                puts.append(request.url.path)
                return httpx.Response(200)
            return httpx.Response(202, json=batch('starting'))
        with client(handle) as sdk:
            sdk.submit_batch([{'filename': 'photo.png', 'file': PNG, 'data': {'a': 1}},
                              {'filename': 'other.png', 'file': PNG, 'data': {'a': 2}}], idempotency_key='batch-0001')
        self.assertEqual(puts, [f'/image/upl_{1:032d}.png'])

    def test_resuming_an_expired_batch_raises(self, sleep):
        def handle(request):
            self.assertEqual(request.url.path, '/watermarks/batches')
            return httpx.Response(200, json=batch('expired', [item(0, 'photo.png')]))
        with client(handle) as sdk:
            with self.assertRaises(GoneError) as error:
                sdk.submit_batch([{'filename': 'photo.png', 'file': PNG, 'data': {'a': 1}}], idempotency_key='batch-0001')
        self.assertEqual(error.exception.code, 'batch_expired')
        self.assertIn('new idempotency_key', str(error.exception))

    def test_the_100_item_cap_is_enforced_before_any_request(self, sleep):
        def handle(request):
            raise AssertionError('no request expected')
        items = [{'filename': f'{i}.png', 'file': PNG, 'data': {'i': i}} for i in range(101)]
        with client(handle) as sdk:
            with self.assertRaisesRegex(ValueError, r'1 to 100 files; got 101\. Split larger sets'):
                sdk.submit_batch(items)
            with self.assertRaisesRegex(ValueError, '1 to 100'):
                sdk.submit_batch([])
            with self.assertRaisesRegex(ValueError, '1 to 100'):
                sdk.submit_batch_zip(ZIP, [{'filename': f'{i}.png', 'data': {'i': i}} for i in range(101)])
            with self.assertRaisesRegex(ValueError, 'data'):
                sdk.submit_batch([{'filename': 'a.png', 'file': PNG, 'data': {}}])
            with self.assertRaisesRegex(ValueError, 'cannot be combined'):
                sdk.submit_batch([{'filename': 'a.png', 'file': PNG, 'data': {'a': 1}}], archive=True,
                                 storage_destination_id='dst_' + '1' * 32)
            with self.assertRaisesRegex(ValueError, 'idempotency_key'):
                sdk.submit_batch([{'filename': 'a.png', 'file': PNG, 'data': {'a': 1}}], idempotency_key='short')
            with self.assertRaises(ValueError):
                sdk.get_batch('bat_nope')

    def test_wait_for_batch_honors_retry_after(self, sleep):
        states = iter([('starting', '3'), ('processing', '7'), ('completed', None)])
        def handle(request):
            self.assertEqual((request.method, request.url.path), ('GET', f'/watermarks/batches/{BATCH}'))
            status, retry_after = next(states)
            return httpx.Response(200, json=batch(status), headers={'retry-after': retry_after} if retry_after else {})
        with client(handle) as sdk:
            result = sdk.wait_for_batch(BATCH)
        self.assertEqual(result.status, 'completed')
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [3.0, 7.0])

    def test_retry_after_is_floored_at_one_second(self, sleep):
        states = iter(['processing', 'completed'])
        with client(lambda request: httpx.Response(200, json=batch(next(states)), headers={'retry-after': '0'})) as sdk:
            sdk.wait_for_batch(BATCH)
        archive = iter([httpx.Response(202, json=batch('processing'), headers={'retry-after': '0'}),
                        httpx.Response(200, content=ZIP)])
        with client(lambda request: next(archive)) as sdk:
            sdk.download_batch_archive(BATCH)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 1.0])

    def test_wait_for_batch_uses_the_longer_of_poll_interval_and_retry_after(self, sleep):
        states = iter(['processing', 'processing', 'failed'])
        def handle(request):
            return httpx.Response(200, json=batch(next(states)), headers={'retry-after': '2'})
        with client(handle) as sdk:
            self.assertEqual(sdk.wait_for_batch(BATCH, poll_interval=5).status, 'failed')
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 5])

    def test_wait_for_batch_deadline(self, sleep):
        ticks = iter(range(1000))  # One second passes per clock read.
        with mock.patch('etchv.client.time.monotonic', side_effect=lambda: next(ticks)):
            with client(lambda request: httpx.Response(200, json=batch('processing'), headers={'retry-after': '1'})) as sdk:
                with self.assertRaises(DeadlineExceededError) as error:
                    sdk.wait_for_batch(BATCH, timeout=5)
        self.assertEqual(error.exception.detail['batch_id'], BATCH)

    def test_partial_failure_is_reported_per_item(self, sleep):
        finished = batch('completed', [
            item(0, 'photo.png', 'succeeded', request_id=request_id(1), credits=1, result_url=f'/watermarks/jobs/{request_id(1)}/result'),
            item(1, 'missing.png', 'rejected', error_code='upload_not_received', error_detail='The file was never uploaded'),
            item(2, 'broken.pdf', 'failed', request_id=request_id(2), credits=0, error_code='invalid_input', error_detail='Not a PDF'),
        ])
        paths = []
        def handle(request):
            paths.append(request.url.path)
            if request.url.path == f'/watermarks/batches/{BATCH}':
                return httpx.Response(200, json=finished)
            return httpx.Response(200, content=PNG, headers={'content-type': 'image/png', 'x-watermark-id': ID,
                                                              'x-request-id': request_id(1)})
        with client(handle) as sdk:
            results = list(sdk.iter_batch_results(BATCH))
        self.assertEqual([r.ok for r in results], [True, False, False])
        self.assertEqual((results[0].result.image, results[0].result.watermark_id), (PNG, ID))
        self.assertEqual([r.error_code for r in results], [None, 'upload_not_received', 'invalid_input'])
        self.assertEqual(results[2].error_detail, 'Not a PDF')
        self.assertEqual(paths, [f'/watermarks/batches/{BATCH}', f'/watermarks/jobs/{request_id(1)}/result'])

    def test_cancelled_items_without_a_code_report_cancelled(self, sleep):
        def handle(request):
            return httpx.Response(200, json=batch('cancelled', [item(0, 'photo.png')]))
        with client(handle) as sdk:
            self.assertEqual([r.error_code for r in sdk.iter_batch_results(BATCH)], ['cancelled'])

    def test_archive_waits_while_202(self, sleep):
        responses = iter([httpx.Response(202, json=batch('processing'), headers={'retry-after': '4'}),
                          httpx.Response(202, json=batch('assembling')),
                          httpx.Response(200, content=ZIP, headers={'content-type': 'application/zip'})])
        def handle(request):
            self.assertEqual(request.url.path, f'/watermarks/batches/{BATCH}/archive')
            return next(responses)
        with client(handle) as sdk:
            self.assertEqual(sdk.download_batch_archive(BATCH), ZIP)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [4.0, 2])

    def test_archive_conflicts_raise_with_their_code(self, sleep):
        for code in ('archive_not_requested', 'batch_not_started'):
            def handle(request):
                return httpx.Response(409, json={'detail': {'code': code, 'message': 'No archive'}})
            with client(handle) as sdk:
                with self.assertRaises(ConflictError) as error:
                    sdk.download_batch_archive(BATCH)
            self.assertEqual(error.exception.code, code)
        with client(lambda request: httpx.Response(410, json={'detail': 'The batch expired'})) as sdk:
            with self.assertRaises(GoneError):
                sdk.download_batch_archive_to(BATCH, io.BytesIO())

    def test_archive_streams_to_a_path_or_file(self, sleep):
        big = ZIP + b'q' * (3 * 1024 * 1024)
        responses = iter([httpx.Response(202, json=batch('assembling'), headers={'retry-after': '1'}),
                          httpx.Response(200, content=big), httpx.Response(200, content=big)])
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder, 'out.zip')
            with client(lambda request: next(responses)) as sdk:
                self.assertEqual(sdk.download_batch_archive_to(BATCH, target), len(big))
                sink = io.BytesIO()
                self.assertEqual(sdk.download_batch_archive_to(BATCH, sink), len(big))
            self.assertEqual(target.read_bytes(), big)
            self.assertEqual(sink.getvalue(), big)
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['out.zip'])

    def test_archives_above_the_cap_are_refused_without_retrying(self, sleep):
        import etchv.client
        self.assertEqual(etchv.client.ARCHIVE_MAX_BYTES, 1024 ** 3 + 64 * 1024 ** 2)
        calls = []
        def declared(request):
            calls.append(1)
            return httpx.Response(200, content=ZIP, headers={'content-length': str(etchv.client.ARCHIVE_MAX_BYTES + 1)})
        with client(declared) as sdk:
            with self.assertRaises(EtchvError) as error:
                sdk.download_batch_archive(BATCH)
        self.assertEqual((error.exception.code, len(calls)), ('archive_too_large', 1))

        def undeclared(request):  # Streamed without a length: the running total is capped.
            calls.append(1)
            return httpx.Response(200, stream=httpx.ByteStream(ZIP + b'x' * 100))
        calls.clear()
        with tempfile.TemporaryDirectory() as folder, mock.patch('etchv.client.ARCHIVE_MAX_BYTES', 64):
            with client(undeclared) as sdk:
                with self.assertRaises(EtchvError) as error:
                    sdk.download_batch_archive_to(BATCH, Path(folder, 'out.zip'))
            self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertEqual((error.exception.code, len(calls)), ('archive_too_large', 1))

    def test_a_failed_archive_download_leaves_no_partial_file(self, sleep):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder, 'out.zip')
            with client(lambda request: httpx.Response(200, content=b'not a zip')) as sdk:
                with self.assertRaises(EtchvError) as error:
                    sdk.download_batch_archive_to(BATCH, target)
            self.assertEqual(error.exception.detail, 'Invalid archive response')
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_cancel_and_list(self, sleep):
        def handle(request):
            if request.url.path.endswith('/cancel'):
                self.assertEqual(request.method, 'POST')
                return httpx.Response(200, json=batch('cancelled', cancel_requested=True))
            self.assertEqual(dict(request.url.params), {'limit': '5', 'before': BATCH})
            return httpx.Response(200, json={'data': [{k: v for k, v in batch('completed').items() if k != 'items'}],
                                             'next_cursor': None})
        with client(handle) as sdk:
            cancelled = sdk.cancel_batch(BATCH)
            page = sdk.list_batches(limit=5, before=BATCH)
        self.assertEqual((cancelled.status, cancelled.cancel_requested), ('cancelled', True))
        self.assertEqual((page.data[0].status, page.data[0].items, page.next_cursor), ('completed', (), None))

    def test_zip_batches_send_the_zip_and_manifest(self, sleep):
        def handle(request):
            self.assertEqual(request.url.path, '/watermarks/batches/zip')
            self.assertEqual(request.headers['idempotency-key'], 'zip-batch-1')
            body = request.read()
            self.assertIn(b'name="archive"; filename="batch.zip"', body)
            self.assertIn(ZIP, body)
            manifest = json.dumps({'items': [{'filename': 'in/a.png', 'data': {'r': 'a'}}], 'archive': True})
            self.assertIn(manifest.encode(), body)
            return httpx.Response(202, json=batch('starting', [item(0, 'in/a.png', 'queued')]))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, 'in.zip')
            path.write_bytes(ZIP)
            with client(handle) as sdk:
                result = sdk.submit_batch_zip(path, [{'filename': 'in/a.png', 'data': {'r': 'a'}}], archive=True,
                                              idempotency_key='zip-batch-1')
        self.assertEqual((result.status, result.items[0].filename), ('starting', 'in/a.png'))


if __name__ == '__main__':
    unittest.main()
