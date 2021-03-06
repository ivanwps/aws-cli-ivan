# Copyright 2013 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.
from awscli.testutils import unittest, temporary_file
import argparse
import errno
import os
import tempfile
import shutil
import ntpath
import time
import datetime
import io

import mock
from dateutil.tz import tzlocal
from nose.tools import assert_equal
from s3transfer.futures import TransferMeta, TransferFuture
from s3transfer.compat import seekable
from botocore.hooks import HierarchicalEmitter

from awscli.compat import queue
from awscli.compat import StringIO
from awscli.testutils import FileCreator
from awscli.customizations.s3.utils import (
    find_bucket_key, find_chunksize, ReadFileChunk,
    guess_content_type, relative_path,
    StablePriorityQueue, BucketLister, get_file_stat, AppendFilter,
    create_warning, human_readable_size, human_readable_to_bytes,
    MAX_SINGLE_UPLOAD_SIZE, MIN_UPLOAD_CHUNKSIZE, MAX_UPLOAD_SIZE,
    set_file_utime, SetFileUtimeError, RequestParamsMapper, uni_print,
    StdoutBytesWriter, ProvideSizeSubscriber, OnDoneFilteredSubscriber,
    ProvideUploadContentTypeSubscriber, ProvideCopyContentTypeSubscriber,
    ProvideLastModifiedTimeSubscriber, DirectoryCreatorSubscriber,
    NonSeekableStream, CreateDirectoryError)
from awscli.customizations.s3.results import WarningResult
from tests.unit.customizations.s3 import FakeTransferFuture
from tests.unit.customizations.s3 import FakeTransferFutureMeta
from tests.unit.customizations.s3 import FakeTransferFutureCallArgs



def test_human_readable_size():
    yield _test_human_size_matches, 1, '1 Byte'
    yield _test_human_size_matches, 10, '10 Bytes'
    yield _test_human_size_matches, 1000, '1000 Bytes'
    yield _test_human_size_matches, 1024, '1.0 KiB'
    yield _test_human_size_matches, 1024 ** 2, '1.0 MiB'
    yield _test_human_size_matches, 1024 ** 2, '1.0 MiB'
    yield _test_human_size_matches, 1024 ** 3, '1.0 GiB'
    yield _test_human_size_matches, 1024 ** 4, '1.0 TiB'
    yield _test_human_size_matches, 1024 ** 5, '1.0 PiB'
    yield _test_human_size_matches, 1024 ** 6, '1.0 EiB'

    # Round to the nearest block.
    yield _test_human_size_matches, 1024 ** 2 - 1, '1.0 MiB'
    yield _test_human_size_matches, 1024 ** 3 - 1, '1.0 GiB'


def _test_human_size_matches(bytes_int, expected):
    assert_equal(human_readable_size(bytes_int), expected)


def test_convert_human_readable_to_bytes():
    yield _test_convert_human_readable_to_bytes, "1", 1
    yield _test_convert_human_readable_to_bytes, "1024", 1024
    yield _test_convert_human_readable_to_bytes, "1KB", 1024
    yield _test_convert_human_readable_to_bytes, "1kb", 1024
    yield _test_convert_human_readable_to_bytes, "1MB", 1024 ** 2
    yield _test_convert_human_readable_to_bytes, "1GB", 1024 ** 3
    yield _test_convert_human_readable_to_bytes, "1TB", 1024 ** 4

    # Also because of the "ls" output for s3, we support
    # the IEC "mebibyte" format (MiB).
    yield _test_convert_human_readable_to_bytes, "1KiB", 1024
    yield _test_convert_human_readable_to_bytes, "1kib", 1024
    yield _test_convert_human_readable_to_bytes, "1MiB", 1024 ** 2
    yield _test_convert_human_readable_to_bytes, "1GiB", 1024 ** 3
    yield _test_convert_human_readable_to_bytes, "1TiB", 1024 ** 4


def _test_convert_human_readable_to_bytes(size_str, expected):
    assert_equal(human_readable_to_bytes(size_str), expected)


class AppendFilterTest(unittest.TestCase):
    def test_call(self):
        parser = argparse.ArgumentParser()

        parser.add_argument('--include', action=AppendFilter, nargs=1,
                            dest='path')
        parser.add_argument('--exclude', action=AppendFilter, nargs=1,
                            dest='path')
        parsed_args = parser.parse_args(['--include', 'a', '--exclude', 'b'])
        self.assertEqual(parsed_args.path, [['--include', 'a'],
                                            ['--exclude', 'b']])


class FindBucketKey(unittest.TestCase):
    """
    This test ensures the find_bucket_key function works when
    unicode is used.
    """
    def test_unicode(self):
        s3_path = '\u1234' + u'/' + '\u5678'
        bucket, key = find_bucket_key(s3_path)
        self.assertEqual(bucket, '\u1234')
        self.assertEqual(key, '\u5678')


class TestCreateWarning(unittest.TestCase):
    def test_create_warning(self):
        path = '/foo/'
        error_message = 'There was an error'
        warning_message = create_warning(path, error_message)
        self.assertEqual(warning_message.message,
                         'warning: Skipping file /foo/. There was an error')
        self.assertFalse(warning_message.error)
        self.assertTrue(warning_message.warning)


class FindChunksizeTest(unittest.TestCase):
    """
    This test ensures that the ``find_chunksize`` function works
    as expected.
    """
    def test_valid_chunk(self):
        """
        This test ensures if the ``chunksize`` is appropriate to begin with,
        it does not change.
        """
        chunksize = 7 * (1024 ** 2)
        size = 8 * (1024 ** 2)
        self.assertEqual(find_chunksize(size, chunksize), chunksize)

    def test_small_chunk(self):
        """
        This test ensures that if the ``chunksize`` is below the minimum
        threshold, it is automatically raised to the minimum.
        """
        chunksize = MIN_UPLOAD_CHUNKSIZE - 1
        size = 3 * MIN_UPLOAD_CHUNKSIZE
        self.assertEqual(find_chunksize(size, chunksize), MIN_UPLOAD_CHUNKSIZE)

    def test_large_chunk(self):
        """
        This test ensures if the ``chunksize`` adapts to an appropriate
        size because the original ``chunksize`` is too small.
        """
        chunksize = 7 * (1024 ** 2)
        size = 5 * (1024 ** 4)
        # If we try to upload a 5TB file, we'll need to use 896MB part
        # sizes.
        self.assertEqual(find_chunksize(size, chunksize), 896 * (1024 ** 2))

    def test_super_chunk(self):
        """
        This tests to ensure that the ``chunksize can never be larger than
        the ``MAX_SINGLE_UPLOAD_SIZE``
        """
        chunksize = MAX_SINGLE_UPLOAD_SIZE + 1
        size = MAX_SINGLE_UPLOAD_SIZE * 2
        self.assertEqual(find_chunksize(size, chunksize),
                         MAX_SINGLE_UPLOAD_SIZE)

    def test_file_too_large(self):
        size = MAX_UPLOAD_SIZE + 1
        chunksize = 1
        with self.assertRaises(ValueError):
            find_chunksize(size, chunksize)


class TestReadFileChunk(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tempdir)

    def test_read_entire_chunk(self):
        filename = os.path.join(self.tempdir, 'foo')
        f = open(filename, 'wb')
        f.write(b'onetwothreefourfivesixseveneightnineten')
        f.flush()
        chunk = ReadFileChunk(filename, start_byte=0, size=3)
        self.assertEqual(chunk.read(), b'one')
        self.assertEqual(chunk.read(), b'')

    def test_read_with_amount_size(self):
        filename = os.path.join(self.tempdir, 'foo')
        f = open(filename, 'wb')
        f.write(b'onetwothreefourfivesixseveneightnineten')
        f.flush()
        chunk = ReadFileChunk(filename, start_byte=11, size=4)
        self.assertEqual(chunk.read(1), b'f')
        self.assertEqual(chunk.read(1), b'o')
        self.assertEqual(chunk.read(1), b'u')
        self.assertEqual(chunk.read(1), b'r')
        self.assertEqual(chunk.read(1), b'')

    def test_reset_stream_emulation(self):
        filename = os.path.join(self.tempdir, 'foo')
        f = open(filename, 'wb')
        f.write(b'onetwothreefourfivesixseveneightnineten')
        f.flush()
        chunk = ReadFileChunk(filename, start_byte=11, size=4)
        self.assertEqual(chunk.read(), b'four')
        chunk.seek(0)
        self.assertEqual(chunk.read(), b'four')

    def test_read_past_end_of_file(self):
        filename = os.path.join(self.tempdir, 'foo')
        f = open(filename, 'wb')
        f.write(b'onetwothreefourfivesixseveneightnineten')
        f.flush()
        chunk = ReadFileChunk(filename, start_byte=36, size=100000)
        self.assertEqual(chunk.read(), b'ten')
        self.assertEqual(chunk.read(), b'')
        self.assertEqual(len(chunk), 3)

    def test_tell_and_seek(self):
        filename = os.path.join(self.tempdir, 'foo')
        f = open(filename, 'wb')
        f.write(b'onetwothreefourfivesixseveneightnineten')
        f.flush()
        chunk = ReadFileChunk(filename, start_byte=36, size=100000)
        self.assertEqual(chunk.tell(), 0)
        self.assertEqual(chunk.read(), b'ten')
        self.assertEqual(chunk.tell(), 3)
        chunk.seek(0)
        self.assertEqual(chunk.tell(), 0)


class TestGuessContentType(unittest.TestCase):
    def test_guess_content_type(self):
        self.assertEqual(guess_content_type('foo.txt'), 'text/plain')

    def test_guess_content_type_with_no_valid_matches(self):
        self.assertEqual(guess_content_type('no-extension'), None)

    def test_guess_content_type_with_unicode_error_returns_no_match(self):
        with mock.patch('mimetypes.guess_type') as guess_type_patch:
            # This should throw a UnicodeDecodeError.
            guess_type_patch.side_effect = lambda x: b'\xe2'.decode('ascii')
            self.assertEqual(guess_content_type('foo.txt'), None)


class TestRelativePath(unittest.TestCase):
    def test_relpath_normal(self):
        self.assertEqual(relative_path('/tmp/foo/bar', '/tmp/foo'),
                         '.' + os.sep + 'bar')

    # We need to patch out relpath with the ntpath version so
    # we can simulate testing drives on windows.
    @mock.patch('os.path.relpath', ntpath.relpath)
    def test_relpath_with_error(self):
        # Just want to check we don't get an exception raised,
        # which is what was happening previously.
        self.assertIn(r'foo\bar', relative_path(r'c:\foo\bar'))


class TestStablePriorityQueue(unittest.TestCase):
    def test_fifo_order_of_same_priorities(self):
        a = mock.Mock()
        a.PRIORITY = 5
        b = mock.Mock()
        b.PRIORITY = 5
        c = mock.Mock()
        c.PRIORITY = 1

        q = StablePriorityQueue(maxsize=10, max_priority=20)
        q.put(a)
        q.put(b)
        q.put(c)

        # First we should get c because it's the lowest priority.
        # We're using assertIs because we want the *exact* object.
        self.assertIs(q.get(), c)
        # Then a and b are the same priority, but we should get
        # a first because it was inserted first.
        self.assertIs(q.get(), a)
        self.assertIs(q.get(), b)

    def test_queue_length(self):
        a = mock.Mock()
        a.PRIORITY = 5

        q = StablePriorityQueue(maxsize=10, max_priority=20)
        self.assertEqual(q.qsize(), 0)

        q.put(a)
        self.assertEqual(q.qsize(), 1)

        q.get()
        self.assertEqual(q.qsize(), 0)

    def test_insert_max_priority_capped(self):
        q = StablePriorityQueue(maxsize=10, max_priority=20)
        a = mock.Mock()
        a.PRIORITY = 100
        q.put(a)

        self.assertIs(q.get(), a)

    def test_priority_attr_is_missing(self):
        # If priority attr is missing, we should add it
        # to the lowest priority.
        q = StablePriorityQueue(maxsize=10, max_priority=20)
        a = object()
        b = mock.Mock()
        b.PRIORITY = 5

        q.put(a)
        q.put(b)

        self.assertIs(q.get(), b)
        self.assertIs(q.get(), a)


class TestBucketList(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        self.emitter = HierarchicalEmitter()
        self.client.meta.events = self.emitter
        self.date_parser = mock.Mock()
        self.date_parser.return_value = mock.sentinel.now
        self.responses = []

    def fake_paginate(self, *args, **kwargs):
        for response in self.responses:
            self.emitter.emit('after-call.s3.ListObjects', parsed=response)
        return self.responses

    def test_list_objects(self):
        now = mock.sentinel.now
        self.client.get_paginator.return_value.paginate = self.fake_paginate
        individual_response_elements = [
            {'LastModified': '2014-02-27T04:20:38.000Z',
             'Key': 'a', 'Size': 1},
            {'LastModified': '2014-02-27T04:20:38.000Z',
                 'Key': 'b', 'Size': 2},
            {'LastModified': '2014-02-27T04:20:38.000Z',
                 'Key': 'c', 'Size': 3}
        ]
        self.responses = [
            {'Contents': individual_response_elements[0:2]},
            {'Contents': [individual_response_elements[2]]}
        ]
        lister = BucketLister(self.client, self.date_parser)
        objects = list(lister.list_objects(bucket='foo'))
        self.assertEqual(objects,
            [('foo/a', individual_response_elements[0]),
             ('foo/b', individual_response_elements[1]),
             ('foo/c', individual_response_elements[2])])
        for individual_response in individual_response_elements:
            self.assertEqual(individual_response['LastModified'], now)


class TestGetFileStat(unittest.TestCase):

    def test_get_file_stat(self):
        now = datetime.datetime.now(tzlocal())
        epoch_now = time.mktime(now.timetuple())
        with temporary_file('w') as f:
            f.write('foo')
            f.flush()
            os.utime(f.name, (epoch_now, epoch_now))
            size, update_time = get_file_stat(f.name)
            self.assertEqual(size, 3)
            self.assertEqual(time.mktime(update_time.timetuple()), epoch_now)

    def test_get_file_stat_error_message(self):
        with mock.patch('os.stat', mock.Mock(side_effect=IOError('msg'))):
            with self.assertRaisesRegexp(ValueError, 'myfilename\.txt'):
                get_file_stat('myfilename.txt')

    def test_get_file_stat_returns_epoch_on_invalid_timestamp(self):
        patch_attribute = 'awscli.customizations.s3.utils.datetime'
        with mock.patch(patch_attribute) as datetime_mock:
            with temporary_file('w') as temp_file:
                temp_file.write('foo')
                temp_file.flush()
                datetime_mock.fromtimestamp.side_effect = ValueError()
                size, update_time = get_file_stat(temp_file.name)
                self.assertIsNone(update_time)



class TestSetsFileUtime(unittest.TestCase):

    def test_successfully_sets_utime(self):
        now = datetime.datetime.now(tzlocal())
        epoch_now = time.mktime(now.timetuple())
        with temporary_file('w') as f:
            set_file_utime(f.name, epoch_now)
            _, update_time = get_file_stat(f.name)
            self.assertEqual(time.mktime(update_time.timetuple()), epoch_now)

    def test_throws_more_relevant_error_when_errno_1(self):
        now = datetime.datetime.now(tzlocal())
        epoch_now = time.mktime(now.timetuple())
        with mock.patch('os.utime') as utime_mock:
            utime_mock.side_effect = OSError(1, '')
            with self.assertRaises(SetFileUtimeError):
                set_file_utime('not_real_file', epoch_now)

    def test_passes_through_other_os_errors(self):
        now = datetime.datetime.now(tzlocal())
        epoch_now = time.mktime(now.timetuple())
        with mock.patch('os.utime') as utime_mock:
            utime_mock.side_effect = OSError(2, '')
            with self.assertRaises(OSError):
                set_file_utime('not_real_file', epoch_now)


class TestRequestParamsMapperSSE(unittest.TestCase):
    def setUp(self):
        self.cli_params = {
            'sse': 'AES256',
            'sse_kms_key_id': 'my-kms-key',
            'sse_c': 'AES256',
            'sse_c_key': 'my-sse-c-key',
            'sse_c_copy_source': 'AES256',
            'sse_c_copy_source_key': 'my-sse-c-copy-source-key'
        }

    def test_head_object(self):
        params = {}
        RequestParamsMapper.map_head_object_params(params, self.cli_params)
        self.assertEqual(
            params,
            {'SSECustomerAlgorithm': 'AES256',
             'SSECustomerKey': 'my-sse-c-key'}
        )

    def test_put_object(self):
        params = {}
        RequestParamsMapper.map_put_object_params(params, self.cli_params)
        self.assertEqual(
            params,
            {'SSECustomerAlgorithm': 'AES256',
             'SSECustomerKey': 'my-sse-c-key',
             'SSEKMSKeyId': 'my-kms-key',
             'ServerSideEncryption': 'AES256'}
        )

    def test_get_object(self):
        params = {}
        RequestParamsMapper.map_get_object_params(params, self.cli_params)
        self.assertEqual(
            params,
            {'SSECustomerAlgorithm': 'AES256',
             'SSECustomerKey': 'my-sse-c-key'}
        )

    def test_copy_object(self):
        params = {}
        RequestParamsMapper.map_copy_object_params(params, self.cli_params)
        self.assertEqual(
            params,
            {'CopySourceSSECustomerAlgorithm': 'AES256',
             'CopySourceSSECustomerKey': 'my-sse-c-copy-source-key',
             'SSECustomerAlgorithm': 'AES256',
             'SSECustomerKey': 'my-sse-c-key',
             'SSEKMSKeyId': 'my-kms-key',
             'ServerSideEncryption': 'AES256'}
        )

    def test_create_multipart_upload(self):
        params = {}
        RequestParamsMapper.map_create_multipart_upload_params(
            params, self.cli_params)
        self.assertEqual(
            params,
            {'SSECustomerAlgorithm': 'AES256',
             'SSECustomerKey': 'my-sse-c-key',
             'SSEKMSKeyId': 'my-kms-key',
             'ServerSideEncryption': 'AES256'}
        )

    def test_upload_part(self):
        params = {}
        RequestParamsMapper.map_upload_part_params(params, self.cli_params)
        self.assertEqual(
            params,
            {'SSECustomerAlgorithm': 'AES256',
             'SSECustomerKey': 'my-sse-c-key'}
        )

    def test_upload_part_copy(self):
        params = {}
        RequestParamsMapper.map_upload_part_copy_params(
            params, self.cli_params)
        self.assertEqual(
            params,
            {'CopySourceSSECustomerAlgorithm': 'AES256',
             'CopySourceSSECustomerKey': 'my-sse-c-copy-source-key',
             'SSECustomerAlgorithm': 'AES256',
             'SSECustomerKey': 'my-sse-c-key'})


class MockPipedStdout(io.BytesIO):
    '''Mocks `sys.stdout`.
    We can't use `TextIOWrapper` because calling
    `TextIOWrapper(.., encoding=None)` sets the ``encoding`` attribute to
    `UTF-8`.
    The attribute is also `readonly` in `TextIOWrapper` and `TextIOBase` so it
    cannot be overwritten in subclasses. For these reasons we mock `sys.stdout`.
    '''
    def __init__(self):
        self.encoding = None

        super(MockPipedStdout, self).__init__()

    def write(self, str):
        # sys.stdout.write() will default to encoding to ascii, when its
        # `encoding` is `None`.
        if self.encoding is None:
            str = str.encode('ascii')
        else:
            str = str.encode(self.encoding)
        super(MockPipedStdout, self).write(str)


class TestUniPrint(unittest.TestCase):

    def test_out_file_with_encoding_attribute(self):
        buf = io.BytesIO()
        out = io.TextIOWrapper(buf, encoding='utf-8')
        uni_print(u'\u2713', out)
        self.assertEqual(buf.getvalue(), u'\u2713'.encode('utf-8'))

    def test_encoding_with_encoding_none(self):
        '''When the output of the aws command is being piped,
        the `encoding` attribute of `sys.stdout` is `None`.'''
        out = MockPipedStdout()
        uni_print(u'SomeChars\u2713\u2714OtherChars', out)
        self.assertEqual(out.getvalue(), b'SomeChars??OtherChars')

    def test_encoding_statement_fails_are_replaced(self):
        buf = io.BytesIO()
        out = io.TextIOWrapper(buf, encoding='ascii')
        uni_print(u'SomeChars\u2713\u2714OtherChars', out)
        # We replace the characters that can't be encoded
        # with '?'.
        self.assertEqual(buf.getvalue(), b'SomeChars??OtherChars')


class TestBytesPrint(unittest.TestCase):
    def setUp(self):
        self.stdout = mock.Mock()
        self.stdout.buffer = self.stdout

    def test_stdout_wrapper(self):
        wrapper = StdoutBytesWriter(self.stdout)
        wrapper.write(b'foo')
        self.assertTrue(self.stdout.write.called)
        self.assertEqual(self.stdout.write.call_args[0][0], b'foo')


class TestProvideSizeSubscriber(unittest.TestCase):
    def setUp(self):
        self.transfer_future = mock.Mock(spec=TransferFuture)
        self.transfer_meta = TransferMeta()
        self.transfer_future.meta = self.transfer_meta

    def test_size_set(self):
        self.transfer_meta.provide_transfer_size(5)
        subscriber = ProvideSizeSubscriber(10)
        subscriber.on_queued(self.transfer_future)
        self.assertEqual(self.transfer_meta.size, 10)


class OnDoneFilteredRecordingSubscriber(OnDoneFilteredSubscriber):
    def __init__(self):
        self.on_success_calls = []
        self.on_failure_calls = []

    def _on_success(self, future):
        self.on_success_calls.append(future)

    def _on_failure(self, future, exception):
        self.on_failure_calls.append((future, exception))


class TestOnDoneFilteredSubscriber(unittest.TestCase):
    def test_on_success(self):
        subscriber = OnDoneFilteredRecordingSubscriber()
        future = FakeTransferFuture('return-value')
        subscriber.on_done(future)
        self.assertEqual(subscriber.on_success_calls, [future])
        self.assertEqual(subscriber.on_failure_calls, [])

    def test_on_failure(self):
        subscriber = OnDoneFilteredRecordingSubscriber()
        exception = Exception('my exception')
        future = FakeTransferFuture(exception=exception)
        subscriber.on_done(future)
        self.assertEqual(subscriber.on_failure_calls, [(future, exception)])
        self.assertEqual(subscriber.on_success_calls, [])


class TestProvideUploadContentTypeSubscriber(unittest.TestCase):
    def setUp(self):
        self.filename = 'myfile.txt'
        self.extra_args = {}
        self.future = self.set_future()
        self.subscriber = ProvideUploadContentTypeSubscriber()

    def set_future(self):
        call_args = FakeTransferFutureCallArgs(
            fileobj=self.filename, extra_args=self.extra_args)
        meta = FakeTransferFutureMeta(call_args=call_args)
        return FakeTransferFuture(meta=meta)

    def test_on_queued_provides_content_type(self):
        self.subscriber.on_queued(self.future)
        self.assertEqual(self.extra_args, {'ContentType': 'text/plain'})

    def test_on_queued_does_not_provide_content_type_when_unknown(self):
        self.filename = 'file-with-no-extension'
        self.future = self.set_future()
        self.subscriber.on_queued(self.future)
        self.assertEqual(self.extra_args, {})


class TestProvideCopyContentTypeSubscriber(
        TestProvideUploadContentTypeSubscriber):
    def setUp(self):
        self.filename = 'myfile.txt'
        self.extra_args = {}
        self.future = self.set_future()
        self.subscriber = ProvideCopyContentTypeSubscriber()

    def set_future(self):
        copy_source = {'Bucket': 'mybucket', 'Key': self.filename}
        call_args = FakeTransferFutureCallArgs(
            copy_source=copy_source, extra_args=self.extra_args)
        meta = FakeTransferFutureMeta(call_args=call_args)
        return FakeTransferFuture(meta=meta)


class BaseTestWithFileCreator(unittest.TestCase):
    def setUp(self):
        self.file_creator = FileCreator()

    def tearDown(self):
        self.file_creator.remove_all()


class TestProvideLastModifiedTimeSubscriber(BaseTestWithFileCreator):
    def setUp(self):
        super(TestProvideLastModifiedTimeSubscriber, self).setUp()
        self.filename = self.file_creator.create_file('myfile', 'my contents')
        self.desired_utime = datetime.datetime(
            2016, 1, 18, 7, 0, 0, tzinfo=tzlocal())
        self.result_queue = queue.Queue()
        self.subscriber = ProvideLastModifiedTimeSubscriber(
            self.desired_utime, self.result_queue)

        call_args = FakeTransferFutureCallArgs(fileobj=self.filename)
        meta = FakeTransferFutureMeta(call_args=call_args)
        self.future = FakeTransferFuture(meta=meta)

    def test_on_success_modifies_utime(self):
        self.subscriber.on_done(self.future)
        _, utime = get_file_stat(self.filename)
        self.assertEqual(utime, self.desired_utime)

    def test_on_success_failure_in_utime_mod_raises_warning(self):
        self.subscriber = ProvideLastModifiedTimeSubscriber(
            None, self.result_queue)
        self.subscriber.on_done(self.future)
        # Because the time to provide was None it will throw an exception
        # which results in the a warning about the utime not being able
        # to be set being placed in the result queue.
        result = self.result_queue.get()
        self.assertIsInstance(result, WarningResult)
        self.assertIn(
            'unable to update the last modified time', result.message)


class TestDirectoryCreatorSubscriber(BaseTestWithFileCreator):
    def setUp(self):
        super(TestDirectoryCreatorSubscriber, self).setUp()
        self.directory_to_create = os.path.join(
            self.file_creator.rootdir, 'new-directory')
        self.filename = os.path.join(self.directory_to_create, 'myfile')

        call_args = FakeTransferFutureCallArgs(fileobj=self.filename)
        meta = FakeTransferFutureMeta(call_args=call_args)
        self.future = FakeTransferFuture(meta=meta)

        self.subscriber = DirectoryCreatorSubscriber()

    def test_on_queued_creates_directories_if_do_not_exist(self):
        self.subscriber.on_queued(self.future)
        self.assertTrue(os.path.exists(self.directory_to_create))

    def test_on_queued_does_not_create_directories_if_exist(self):
        os.makedirs(self.directory_to_create)
        # This should not cause any issues if the directory already exists
        self.subscriber.on_queued(self.future)
        # The directory should still exist
        self.assertTrue(os.path.exists(self.directory_to_create))

    def test_on_queued_failure_propogates_create_directory_error(self):
        # If makedirs() raises an OSError of exception, we should
        # propogate the exception with a better worded CreateDirectoryError.
        with mock.patch('os.makedirs') as makedirs_patch:
            makedirs_patch.side_effect = OSError()
            with self.assertRaises(CreateDirectoryError):
                self.subscriber.on_queued(self.future)
        self.assertFalse(os.path.exists(self.directory_to_create))

    def test_on_queued_failure_propogates_clear_error_message(self):
        # If makedirs() raises an OSError of exception, we should
        # propogate the exception.
        with mock.patch('os.makedirs') as makedirs_patch:
            os_error = OSError()
            os_error.errno = errno.EEXIST
            makedirs_patch.side_effect = os_error
            # The on_queued should not raise an error if the directory
            # already exists
            try:
                self.subscriber.on_queued(self.future)
            except Exception as e:
                self.fail(
                    'on_queued should not have raised an exception related '
                    'to directory creation especially if one already existed '
                    'but got %s' % e)


class TestNonSeekableStream(unittest.TestCase):
    def test_can_make_stream_unseekable(self):
        fileobj = StringIO('foobar')
        self.assertTrue(seekable(fileobj))
        nonseekable_fileobj = NonSeekableStream(fileobj)
        self.assertFalse(seekable(nonseekable_fileobj))
        self.assertEqual(nonseekable_fileobj.read(), 'foobar')

    def test_can_specify_amount_for_nonseekable_stream(self):
        nonseekable_fileobj = NonSeekableStream(StringIO('foobar'))
        self.assertEqual(nonseekable_fileobj.read(3), 'foo')
