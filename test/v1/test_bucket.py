######################################################################
#
# File: test/v1/test_bucket.py
#
# Copyright 2019 Backblaze Inc. All Rights Reserved.
#
# License https://www.backblaze.com/using_b2_code.html
#
######################################################################

from __future__ import absolute_import, division

from nose import SkipTest
import os
import platform

import six

from .test_base import TestBase

from .deps_exception import (
    AlreadyFailed,
    B2Error,
    InvalidAuthToken,
    InvalidMetadataDirective,
    InvalidRange,
    InvalidUploadSource,
    MaxRetriesExceeded,
    UnsatisfiableRange,
)
from .deps import B2Api
from .deps import LargeFileUploadState
from .deps import DownloadDestBytes, PreSeekedDownloadDest
from .deps import FileVersionInfo
from .deps import MetadataDirectiveMode
from .deps import Part
from .deps import AbstractProgressListener
from .deps import StubAccountInfo, RawSimulator, BucketSimulator, FakeResponse
from .deps import ParallelDownloader
from .deps import SimpleDownloader
from .deps import UploadSourceBytes
from .deps import hex_sha1_of_bytes, TempDir

try:
    import unittest.mock as mock
except ImportError:
    import mock


def write_file(path, data):
    with open(path, 'wb') as f:
        f.write(data)


class StubProgressListener(AbstractProgressListener):
    """
    Implementation of a progress listener that remembers what calls were made,
    and returns them as a short string to use in unit tests.

    For a total byte count of 100, and updates at 33 and 66, the returned
    string looks like: "100: 33 66"
    """

    def __init__(self):
        self.total = None
        self.history = []
        self.last_byte_count = 0

    def get_history(self):
        return ' '.join(self.history)

    def set_total_bytes(self, total_byte_count):
        assert total_byte_count is not None
        assert self.total is None, 'set_total_bytes called twice'
        self.total = total_byte_count
        assert len(self.history) == 0, self.history
        self.history.append('%d:' % (total_byte_count,))

    def bytes_completed(self, byte_count):
        assert byte_count >= self.last_byte_count
        self.last_byte_count = byte_count
        self.history.append(str(byte_count))

    def is_valid(self):
        return self.total == self.last_byte_count

    def close(self):
        self.history.append('closed')


class CanRetry(B2Error):
    """
    An exception that can be retryable, or not.
    """

    def __init__(self, can_retry):
        super(CanRetry, self).__init__(None, None, None, None, None)
        self.can_retry = can_retry

    def should_retry_upload(self):
        return self.can_retry


class TestCaseWithBucket(TestBase):
    RAW_SIMULATOR_CLASS = RawSimulator

    def setUp(self):
        self.bucket_name = 'my-bucket'
        self.simulator = self.RAW_SIMULATOR_CLASS()
        self.account_info = StubAccountInfo()
        self.api = B2Api(self.account_info, raw_api=self.simulator)
        (self.account_id, self.master_key) = self.simulator.create_account()
        self.api.authorize_account('production', self.account_id, self.master_key)
        self.api_url = self.account_info.get_api_url()
        self.account_auth_token = self.account_info.get_account_auth_token()
        self.bucket = self.api.create_bucket('my-bucket', 'allPublic')
        self.bucket_id = self.bucket.id_

    def assertBucketContents(self, expected, *args, **kwargs):
        """
        *args and **kwargs are passed to self.bucket.ls()
        """
        actual = [
            (info.file_name, info.size, info.action, folder)
            for (info, folder) in self.bucket.ls(*args, **kwargs)
        ]
        self.assertEqual(expected, actual)


class TestReauthorization(TestCaseWithBucket):
    def testCreateBucket(self):
        class InvalidAuthTokenWrapper(object):
            def __init__(self, original_function):
                self.__original_function = original_function
                self.__name__ = original_function.__name__
                self.__called = False

            def __call__(self, *args, **kwargs):
                if self.__called:
                    return self.__original_function(*args, **kwargs)
                self.__called = True
                raise InvalidAuthToken('message', 401)

        self.simulator.create_bucket = InvalidAuthTokenWrapper(self.simulator.create_bucket)
        self.bucket = self.api.create_bucket('your-bucket', 'allPublic')


class TestListParts(TestCaseWithBucket):
    def testEmpty(self):
        file1 = self.bucket.start_large_file('file1.txt', 'text/plain', {})
        self.assertEqual([], list(self.bucket.list_parts(file1.file_id, batch_size=1)))

    def testThree(self):
        file1 = self.bucket.start_large_file('file1.txt', 'text/plain', {})
        content = six.b('hello world')
        content_sha1 = hex_sha1_of_bytes(content)
        large_file_upload_state = mock.MagicMock()
        large_file_upload_state.has_error.return_value = False
        self.bucket._upload_part(
            file1.file_id, 1, (0, 11), UploadSourceBytes(content), large_file_upload_state
        )
        self.bucket._upload_part(
            file1.file_id, 2, (0, 11), UploadSourceBytes(content), large_file_upload_state
        )
        self.bucket._upload_part(
            file1.file_id, 3, (0, 11), UploadSourceBytes(content), large_file_upload_state
        )
        expected_parts = [
            Part('9999', 1, 11, content_sha1),
            Part('9999', 2, 11, content_sha1),
            Part('9999', 3, 11, content_sha1),
        ]
        self.assertEqual(expected_parts, list(self.bucket.list_parts(file1.file_id, batch_size=1)))


class TestUploadPart(TestCaseWithBucket):
    def test_error_in_state(self):
        file1 = self.bucket.start_large_file('file1.txt', 'text/plain', {})
        content = six.b('hello world')
        file_progress_listener = mock.MagicMock()
        large_file_upload_state = LargeFileUploadState(file_progress_listener)
        large_file_upload_state.set_error('test error')
        try:
            self.bucket._upload_part(
                file1.file_id, 1, (0, 11), UploadSourceBytes(content), large_file_upload_state
            )
            self.fail('should have thrown')
        except AlreadyFailed:
            pass


class TestListUnfinished(TestCaseWithBucket):
    def test_empty(self):
        self.assertEqual([], list(self.bucket.list_unfinished_large_files()))

    def test_one(self):
        file1 = self.bucket.start_large_file('file1.txt', 'text/plain', {})
        self.assertEqual([file1], list(self.bucket.list_unfinished_large_files()))

    def test_three(self):
        file1 = self.bucket.start_large_file('file1.txt', 'text/plain', {})
        file2 = self.bucket.start_large_file('file2.txt', 'text/plain', {})
        file3 = self.bucket.start_large_file('file3.txt', 'text/plain', {})
        self.assertEqual(
            [file1, file2, file3], list(self.bucket.list_unfinished_large_files(batch_size=1))
        )

    def _make_file(self, file_id, file_name):
        return self.bucket.start_large_file(file_name, 'text/plain', {})


class TestLs(TestCaseWithBucket):
    def test_empty(self):
        self.assertEqual([], list(self.bucket.ls('foo')))

    def test_one_file_at_root(self):
        data = six.b('hello world')
        self.bucket.upload_bytes(data, 'hello.txt')
        expected = [('hello.txt', 11, 'upload', None)]
        self.assertBucketContents(expected, '')

    def test_three_files_at_root(self):
        data = six.b('hello world')
        self.bucket.upload_bytes(data, 'a')
        self.bucket.upload_bytes(data, 'bb')
        self.bucket.upload_bytes(data, 'ccc')
        expected = [
            ('a', 11, 'upload', None), ('bb', 11, 'upload', None), ('ccc', 11, 'upload', None)
        ]
        self.assertBucketContents(expected, '')

    def test_three_files_in_dir(self):
        data = six.b('hello world')
        self.bucket.upload_bytes(data, 'a')
        self.bucket.upload_bytes(data, 'bb/1')
        self.bucket.upload_bytes(data, 'bb/2/sub1')
        self.bucket.upload_bytes(data, 'bb/2/sub2')
        self.bucket.upload_bytes(data, 'bb/3')
        self.bucket.upload_bytes(data, 'ccc')
        expected = [
            ('bb/1', 11, 'upload', None), ('bb/2/sub1', 11, 'upload', 'bb/2/'),
            ('bb/3', 11, 'upload', None)
        ]
        self.assertBucketContents(expected, 'bb', fetch_count=1)

    def test_three_files_multiple_versions(self):
        data = six.b('hello world')
        self.bucket.upload_bytes(data, 'a')
        self.bucket.upload_bytes(data, 'bb/1')
        self.bucket.upload_bytes(data, 'bb/2')
        self.bucket.upload_bytes(data, 'bb/2')
        self.bucket.upload_bytes(data, 'bb/2')
        self.bucket.upload_bytes(data, 'bb/3')
        self.bucket.upload_bytes(data, 'ccc')
        expected = [
            ('9998', 'bb/1', 11, 'upload', None),
            ('9995', 'bb/2', 11, 'upload', None),
            ('9996', 'bb/2', 11, 'upload', None),
            ('9997', 'bb/2', 11, 'upload', None),
            ('9994', 'bb/3', 11, 'upload', None),
        ]
        actual = [
            (info.id_, info.file_name, info.size, info.action, folder)
            for (info, folder) in self.bucket.ls('bb', show_versions=True, fetch_count=1)
        ]
        self.assertEqual(expected, actual)

    def test_started_large_file(self):
        self.bucket.start_large_file('hello.txt')
        expected = [('hello.txt', 0, 'start', None)]
        self.assertBucketContents(expected, '', show_versions=True)

    def test_hidden_file(self):
        data = six.b('hello world')
        self.bucket.upload_bytes(data, 'hello.txt')
        self.bucket.hide_file('hello.txt')
        expected = [('hello.txt', 0, 'hide', None), ('hello.txt', 11, 'upload', None)]
        self.assertBucketContents(expected, '', show_versions=True)

    def test_delete_file_version(self):
        data = six.b('hello world')

        file_id = self.bucket.upload_bytes(data, 'hello.txt').id_

        data = six.b('hello new world')
        self.bucket.upload_bytes(data, 'hello.txt')
        self.bucket.delete_file_version(file_id, 'hello.txt')

        expected = [('hello.txt', 15, 'upload', None)]
        self.assertBucketContents(expected, '', show_versions=True)


class TestCopyFile(TestCaseWithBucket):
    def test_copy_without_optional_params(self):
        file_id = self._make_file()
        self.bucket.copy_file(file_id, 'hello_new.txt')
        expected = [('hello.txt', 11, 'upload', None), ('hello_new.txt', 11, 'copy', None)]
        self.assertBucketContents(expected, '', show_versions=True)

    def test_copy_with_range(self):
        file_id = self._make_file()
        self.bucket.copy_file(file_id, 'hello_new.txt', bytes_range=(3, 9))
        expected = [('hello.txt', 11, 'upload', None), ('hello_new.txt', 6, 'copy', None)]
        self.assertBucketContents(expected, '', show_versions=True)

    def test_copy_with_invalid_metadata(self):
        file_id = self._make_file()
        try:
            self.bucket.copy_file(
                file_id,
                'hello_new.txt',
                metadata_directive=MetadataDirectiveMode.COPY,
                content_type='application/octet-stream',
            )
            self.fail('should have raised InvalidMetadataDirective')
        except InvalidMetadataDirective as e:
            self.assertEqual(
                'content_type and file_info should be None when metadata_directive is COPY',
                str(e),
            )
        expected = [('hello.txt', 11, 'upload', None)]
        self.assertBucketContents(expected, '', show_versions=True)

    def test_copy_with_invalid_metadata_replace(self):
        file_id = self._make_file()
        try:
            self.bucket.copy_file(
                file_id,
                'hello_new.txt',
                metadata_directive=MetadataDirectiveMode.REPLACE,
            )
            self.fail('should have raised InvalidMetadataDirective')
        except InvalidMetadataDirective as e:
            self.assertEqual(
                'content_type cannot be None when metadata_directive is REPLACE',
                str(e),
            )
        expected = [('hello.txt', 11, 'upload', None)]
        self.assertBucketContents(expected, '', show_versions=True)

    def test_copy_with_replace_metadata(self):
        file_id = self._make_file()
        self.bucket.copy_file(
            file_id,
            'hello_new.txt',
            metadata_directive=MetadataDirectiveMode.REPLACE,
            content_type='text/plain',
        )
        expected = [
            ('hello.txt', 11, 'upload', 'b2/x-auto', None),
            ('hello_new.txt', 11, 'copy', 'text/plain', None),
        ]
        actual = [
            (info.file_name, info.size, info.action, info.content_type, folder)
            for (info, folder) in self.bucket.ls(show_versions=True)
        ]
        self.assertEqual(expected, actual)

    def test_copy_with_unsatisfied_range(self):
        file_id = self._make_file()
        try:
            self.bucket.copy_file(
                file_id,
                'hello_new.txt',
                bytes_range=(12, 15),
            )
            self.fail('should have raised UnsatisfiableRange')
        except UnsatisfiableRange as e:
            self.assertEqual(
                'The range in the request is outside the size of the file',
                str(e),
            )
        expected = [('hello.txt', 11, 'upload', None)]
        self.assertBucketContents(expected, '', show_versions=True)

    def test_copy_with_different_bucket(self):
        source_bucket = self.api.create_bucket('source-bucket', 'allPublic')
        file_id = self._make_file(source_bucket)
        self.bucket.copy_file(file_id, 'hello_new.txt')

        def ls(bucket):
            return [
                (info.file_name, info.size, info.action, folder)
                for (info, folder) in bucket.ls(show_versions=True)
            ]

        expected = [('hello.txt', 11, 'upload', None)]
        self.assertEqual(expected, ls(source_bucket))
        expected = [('hello_new.txt', 11, 'copy', None)]
        self.assertBucketContents(expected, '', show_versions=True)

    def _make_file(self, bucket=None):
        data = six.b('hello world')
        actual_bucket = bucket or self.bucket
        return actual_bucket.upload_bytes(data, 'hello.txt').id_


class TestUpload(TestCaseWithBucket):
    def test_upload_bytes(self):
        data = six.b('hello world')
        file_info = self.bucket.upload_bytes(data, 'file1')
        self.assertTrue(isinstance(file_info, FileVersionInfo))
        self._check_file_contents('file1', data)

    def test_upload_bytes_progress(self):
        data = six.b('hello world')
        progress_listener = StubProgressListener()
        self.bucket.upload_bytes(data, 'file1', progress_listener=progress_listener)
        self.assertEqual("11: 11 closed", progress_listener.get_history())

    def test_upload_local_file(self):
        with TempDir() as d:
            path = os.path.join(d, 'file1')
            data = six.b('hello world')
            write_file(path, data)
            self.bucket.upload_local_file(path, 'file1')
            self._check_file_contents('file1', data)

    def test_upload_fifo(self):
        if platform.system() == 'Windows':
            raise SkipTest('no os.mkfifo() on Windows')
        with TempDir() as d:
            path = os.path.join(d, 'file1')
            os.mkfifo(path)
            with self.assertRaises(InvalidUploadSource):
                self.bucket.upload_local_file(path, 'file1')

    def test_upload_dead_symlink(self):
        if platform.system() == 'Windows':
            raise SkipTest('no os.symlink() on Windows')
        with TempDir() as d:
            path = os.path.join(d, 'file1')
            os.symlink('non-existing', path)
            with self.assertRaises(InvalidUploadSource):
                self.bucket.upload_local_file(path, 'file1')

    def test_upload_one_retryable_error(self):
        self.simulator.set_upload_errors([CanRetry(True)])
        data = six.b('hello world')
        self.bucket.upload_bytes(data, 'file1')

    def test_upload_file_one_fatal_error(self):
        self.simulator.set_upload_errors([CanRetry(False)])
        data = six.b('hello world')
        with self.assertRaises(CanRetry):
            self.bucket.upload_bytes(data, 'file1')

    def test_upload_file_too_many_retryable_errors(self):
        self.simulator.set_upload_errors([CanRetry(True)] * 6)
        data = six.b('hello world')
        with self.assertRaises(MaxRetriesExceeded):
            self.bucket.upload_bytes(data, 'file1')

    def test_upload_large(self):
        data = self._make_data(self.simulator.MIN_PART_SIZE * 3)
        progress_listener = StubProgressListener()
        self.bucket.upload_bytes(data, 'file1', progress_listener=progress_listener)
        self._check_file_contents('file1', data)
        self.assertEqual("600: 200 400 600 closed", progress_listener.get_history())

    def test_upload_large_resume(self):
        part_size = self.simulator.MIN_PART_SIZE
        data = self._make_data(part_size * 3)
        large_file_id = self._start_large_file('file1')
        self._upload_part(large_file_id, 1, data[:part_size])
        progress_listener = StubProgressListener()
        file_info = self.bucket.upload_bytes(data, 'file1', progress_listener=progress_listener)
        self.assertEqual(large_file_id, file_info.id_)
        self._check_file_contents('file1', data)
        self.assertEqual("600: 200 400 600 closed", progress_listener.get_history())

    def test_upload_large_resume_no_parts(self):
        part_size = self.simulator.MIN_PART_SIZE
        data = self._make_data(part_size * 3)
        large_file_id = self._start_large_file('file1')
        progress_listener = StubProgressListener()
        file_info = self.bucket.upload_bytes(data, 'file1', progress_listener=progress_listener)
        self.assertNotEqual(large_file_id, file_info.id_)  # it's not a match if there are no parts
        self._check_file_contents('file1', data)
        self.assertEqual("600: 200 400 600 closed", progress_listener.get_history())

    def test_upload_large_resume_all_parts_there(self):
        part_size = self.simulator.MIN_PART_SIZE
        data = self._make_data(part_size * 3)
        large_file_id = self._start_large_file('file1')
        self._upload_part(large_file_id, 1, data[:part_size])
        self._upload_part(large_file_id, 2, data[part_size:2 * part_size])
        self._upload_part(large_file_id, 3, data[2 * part_size:])
        progress_listener = StubProgressListener()
        file_info = self.bucket.upload_bytes(data, 'file1', progress_listener=progress_listener)
        self.assertEqual(large_file_id, file_info.id_)
        self._check_file_contents('file1', data)
        self.assertEqual("600: 200 400 600 closed", progress_listener.get_history())

    def test_upload_large_resume_part_does_not_match(self):
        part_size = self.simulator.MIN_PART_SIZE
        data = self._make_data(part_size * 3)
        large_file_id = self._start_large_file('file1')
        self._upload_part(large_file_id, 3, data[:part_size])  # wrong part number for this data
        progress_listener = StubProgressListener()
        file_info = self.bucket.upload_bytes(data, 'file1', progress_listener=progress_listener)
        self.assertNotEqual(large_file_id, file_info.id_)
        self._check_file_contents('file1', data)
        self.assertEqual("600: 200 400 600 closed", progress_listener.get_history())

    def test_upload_large_resume_wrong_part_size(self):
        part_size = self.simulator.MIN_PART_SIZE
        data = self._make_data(part_size * 3)
        large_file_id = self._start_large_file('file1')
        self._upload_part(large_file_id, 1, data[:part_size + 1])  # one byte to much
        progress_listener = StubProgressListener()
        file_info = self.bucket.upload_bytes(data, 'file1', progress_listener=progress_listener)
        self.assertNotEqual(large_file_id, file_info.id_)
        self._check_file_contents('file1', data)
        self.assertEqual("600: 200 400 600 closed", progress_listener.get_history())

    def test_upload_large_resume_file_info(self):
        part_size = self.simulator.MIN_PART_SIZE
        data = self._make_data(part_size * 3)
        large_file_id = self._start_large_file('file1', {'property': 'value1'})
        self._upload_part(large_file_id, 1, data[:part_size])
        progress_listener = StubProgressListener()
        file_info = self.bucket.upload_bytes(
            data, 'file1', progress_listener=progress_listener, file_infos={'property': 'value1'}
        )
        self.assertEqual(large_file_id, file_info.id_)
        self._check_file_contents('file1', data)
        self.assertEqual("600: 200 400 600 closed", progress_listener.get_history())

    def test_upload_large_resume_file_info_does_not_match(self):
        part_size = self.simulator.MIN_PART_SIZE
        data = self._make_data(part_size * 3)
        large_file_id = self._start_large_file('file1', {'property': 'value1'})
        self._upload_part(large_file_id, 1, data[:part_size])
        progress_listener = StubProgressListener()
        file_info = self.bucket.upload_bytes(
            data, 'file1', progress_listener=progress_listener, file_infos={'property': 'value2'}
        )
        self.assertNotEqual(large_file_id, file_info.id_)
        self._check_file_contents('file1', data)
        self.assertEqual("600: 200 400 600 closed", progress_listener.get_history())

    def _start_large_file(self, file_name, file_info=None):
        if file_info is None:
            file_info = {}
        large_file_info = self.simulator.start_large_file(
            self.api_url, self.account_auth_token, self.bucket_id, file_name, None, file_info
        )
        return large_file_info['fileId']

    def _upload_part(self, large_file_id, part_number, part_data):
        part_stream = six.BytesIO(part_data)
        upload_info = self.simulator.get_upload_part_url(
            self.api_url, self.account_auth_token, large_file_id
        )
        self.simulator.upload_part(
            upload_info['uploadUrl'], upload_info['authorizationToken'], part_number,
            len(part_data), hex_sha1_of_bytes(part_data), part_stream
        )

    def _check_file_contents(self, file_name, expected_contents):
        download = DownloadDestBytes()
        self.bucket.download_file_by_name(file_name, download)
        self.assertEqual(expected_contents, download.get_bytes_written())

    def _make_data(self, approximate_length):
        """
        Generate a sequence of bytes to use in testing an upload.
        Don't repeat a short pattern, so we're sure that the different
        parts of a large file are actually different.

        Returns bytes.
        """
        fragments = []
        so_far = 0
        while so_far < approximate_length:
            fragment = ('%d:' % so_far).encode('utf-8')
            so_far += len(fragment)
            fragments.append(fragment)
        return six.b('').join(fragments)


# Downloads


class DownloadTests(object):
    DATA = 'abcdefghijklmnopqrs'

    def setUp(self):
        super(DownloadTests, self).setUp()
        self.file_info = self.bucket.upload_bytes(six.b(self.DATA), 'file1')
        self.download_dest = DownloadDestBytes()
        self.progress_listener = StubProgressListener()

    def _verify(self, expected_result, check_progress_listener=True):
        assert self.download_dest.get_bytes_written() == six.b(expected_result)
        if check_progress_listener:
            assert self.progress_listener.is_valid()

    def test_download_by_id_no_progress(self):
        self.bucket.download_file_by_id(self.file_info.id_, self.download_dest)
        self._verify(self.DATA, check_progress_listener=False)

    def test_download_by_name_no_progress(self):
        self.bucket.download_file_by_name('file1', self.download_dest)
        self._verify(self.DATA, check_progress_listener=False)

    def test_download_by_name_progress(self):
        self.bucket.download_file_by_name('file1', self.download_dest, self.progress_listener)
        self._verify(self.DATA)

    def test_download_by_id_progress(self):
        self.bucket.download_file_by_id(
            self.file_info.id_, self.download_dest, self.progress_listener
        )
        self._verify(self.DATA)

    def test_download_by_id_progress_partial(self):
        self.bucket.download_file_by_id(
            self.file_info.id_, self.download_dest, self.progress_listener, range_=(3, 9)
        )
        self._verify('defghij')

    def test_download_by_id_progress_exact_range(self):
        self.bucket.download_file_by_id(
            self.file_info.id_, self.download_dest, self.progress_listener, range_=(0, 18)
        )
        self._verify(self.DATA)

    def test_download_by_id_progress_range_one_off(self):
        with self.assertRaises(
            InvalidRange,
            msg='A range of 0-19 was requested (size of 20), but cloud could only serve 19 of that',
        ):
            self.bucket.download_file_by_id(
                self.file_info.id_,
                self.download_dest,
                self.progress_listener,
                range_=(0, 19),
            )

    def test_download_by_id_progress_partial_inplace_overwrite(self):
        # LOCAL is
        # 12345678901234567890
        #
        # and then:
        #
        # abcdefghijklmnopqrs
        #    |||||||
        #    |||||||
        #    vvvvvvv
        #
        # 123defghij1234567890

        with TempDir() as d:
            path = os.path.join(d, 'file2')
            download_dest = PreSeekedDownloadDest(seek_target=3, local_file_path=path)
            data = six.b('12345678901234567890')
            write_file(path, data)
            self.bucket.download_file_by_id(
                self.file_info.id_,
                download_dest,
                self.progress_listener,
                range_=(3, 9),
            )
            self._check_local_file_contents(path, six.b('123defghij1234567890'))

    def test_download_by_id_progress_partial_shifted_overwrite(self):
        # LOCAL is
        # 12345678901234567890
        #
        # and then:
        #
        # abcdefghijklmnopqrs
        #    |||||||
        #    \\\\\\\
        #     \\\\\\\
        #      \\\\\\\
        #       \\\\\\\
        #        \\\\\\\
        #        |||||||
        #        vvvvvvv
        #
        # 1234567defghij567890

        with TempDir() as d:
            path = os.path.join(d, 'file2')
            download_dest = PreSeekedDownloadDest(seek_target=7, local_file_path=path)
            data = six.b('12345678901234567890')
            write_file(path, data)
            self.bucket.download_file_by_id(
                self.file_info.id_,
                download_dest,
                self.progress_listener,
                range_=(3, 9),
            )
            self._check_local_file_contents(path, six.b('1234567defghij567890'))

    def _check_local_file_contents(self, path, expected_contents):
        with open(path, 'rb') as f:
            contents = f.read()
            self.assertEqual(contents, expected_contents)


# download empty file


class EmptyFileDownloadScenarioMixin(object):
    """ use with DownloadTests, but not for TestDownloadParallel as it does not like empty files """

    def test_download_by_name_empty_file(self):
        self.file_info = self.bucket.upload_bytes(six.b(''), 'empty')
        self.bucket.download_file_by_name('empty', self.download_dest, self.progress_listener)
        self._verify('')


# actual tests


class TestDownloadDefault(DownloadTests, EmptyFileDownloadScenarioMixin, TestCaseWithBucket):
    pass


class TestDownloadSimple(DownloadTests, EmptyFileDownloadScenarioMixin, TestCaseWithBucket):
    def setUp(self):
        super(TestDownloadSimple, self).setUp()
        self.bucket.api.transferer.strategies = [SimpleDownloader(force_chunk_size=20,)]


class TestDownloadParallel(DownloadTests, TestCaseWithBucket):
    def setUp(self):
        super(TestDownloadParallel, self).setUp()
        self.bucket.api.transferer.strategies = [
            ParallelDownloader(
                force_chunk_size=2,
                max_streams=999,
                min_part_size=2,
            )
        ]


# Truncated downloads


class TruncatedFakeResponse(FakeResponse):
    """
    A special FakeResponse class which returns only the first 4 bytes of data.
    Use it to test followup retries for truncated download issues.
    """

    def __init__(self, *args, **kwargs):
        super(TruncatedFakeResponse, self).__init__(*args, **kwargs)
        self.data_bytes = self.data_bytes[:4]


class TruncatedDownloadBucketSimulator(BucketSimulator):
    RESPONSE_CLASS = TruncatedFakeResponse


class TruncatedDownloadRawSimulator(RawSimulator):
    BUCKET_SIMULATOR_CLASS = TruncatedDownloadBucketSimulator


class TestCaseWithTruncatedDownloadBucket(TestCaseWithBucket):
    RAW_SIMULATOR_CLASS = TruncatedDownloadRawSimulator


####### actual tests of truncated downloads


class TestTruncatedDownloadSimple(DownloadTests, TestCaseWithTruncatedDownloadBucket):
    def setUp(self):
        super(TestTruncatedDownloadSimple, self).setUp()
        self.bucket.api.transferer.strategies = [SimpleDownloader(force_chunk_size=20,)]


class TestTruncatedDownloadParallel(DownloadTests, TestCaseWithTruncatedDownloadBucket):
    def setUp(self):
        super(TestTruncatedDownloadParallel, self).setUp()
        self.bucket.api.transferer.strategies = [
            ParallelDownloader(
                force_chunk_size=3,
                max_streams=2,
                min_part_size=2,
            )
        ]
