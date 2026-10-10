import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
from studio import Candidate, normalize_clips, seconds_to_hms, youtube_timestamp_link, is_aparat, is_youtube, is_aparat_cdn, fetch_video, load_clip_settings


class StudioTests(unittest.TestCase):
    def test_valid_clip(self):
        items = [Candidate(start=6, end=51, title='تیتر', description='توضیح', hashtags=['#غدیر'])]
        valid, rejected = normalize_clips(items, 120, 25, 65, 4, 'automatic')
        self.assertEqual((len(valid), len(rejected)), (1, 0))

    def test_bad_timestamps_rejected(self):
        items = [Candidate(start=30, end=10, title='x'), Candidate(start=60, end=400, title='y')]
        valid, rejected = normalize_clips(items, 180, 20, 70, 4, 'automatic')
        self.assertEqual((len(valid), len(rejected)), (0, 2))

    def test_overlap_rejected(self):
        items = [Candidate(start=10, end=40, title='x'), Candidate(start=20, end=50, title='y')]
        valid, rejected = normalize_clips(items, 100, 15, 50, 4, 'automatic')
        self.assertEqual((len(valid), len(rejected)), (1, 1))

    def test_precut_full_video(self):
        items = [Candidate(start=20, end=25, title='x')]
        valid, _ = normalize_clips(items, 78.1, 25, 65, 4, 'precut')
        self.assertEqual((valid[0].start, valid[0].end), (0, 78.1))

    def test_aparat_page_urls(self):
        self.assertTrue(is_aparat('https://www.aparat.com/v/abc12'))
        self.assertTrue(is_aparat('https://aparat.com/v/abc12'))
        self.assertFalse(is_aparat('https://notaparat.com/v/abc12'))
        self.assertFalse(is_youtube('https://www.aparat.com/v/abc12'))

    def test_format_and_link(self):
        self.assertEqual(seconds_to_hms(3661.2), '01:01:01')
        self.assertEqual(youtube_timestamp_link('https://youtu.be/abcdefghijk', 70), 'https://www.youtube.com/watch?v=abcdefghijk&t=70s')
        self.assertEqual(youtube_timestamp_link('https://example.com/x.mp4', 70), '')


    def test_settings_defaults(self):
        with patch.dict('os.environ', {'STUDIO_COUNT': '', 'STUDIO_MIN_SECONDS': '',
                                     'STUDIO_MAX_SECONDS': ''}):
            self.assertEqual(load_clip_settings('automatic'), (4, 25, 65))

    def test_swaps_reversed_interval(self):
        with patch.dict('os.environ', {'STUDIO_COUNT': '3', 'STUDIO_MIN_SECONDS': '90',
                                     'STUDIO_MAX_SECONDS': '20'}):
            self.assertEqual(load_clip_settings('automatic'), (3, 20, 90))

    def test_precut_ignores_unused_values(self):
        with patch.dict('os.environ', {'STUDIO_COUNT': '1000', 'STUDIO_MIN_SECONDS': 'bad',
                                     'STUDIO_MAX_SECONDS': '0'}):
            self.assertEqual(load_clip_settings('precut'), (1, 25, 65))

    def test_count_error_is_descriptive(self):
        with patch.dict('os.environ', {'STUDIO_COUNT': '13'}):
            with self.assertRaisesRegex(ValueError, 'STUDIO_COUNT=13'):
                load_clip_settings('automatic')

    def test_duration_error_is_descriptive(self):
        with patch.dict('os.environ', {'STUDIO_MIN_SECONDS': '2', 'STUDIO_MAX_SECONDS': '20'}):
            with self.assertRaisesRegex(ValueError, 'min_seconds=2'):
                load_clip_settings('automatic')

    def test_time_notation_and_persian_digits(self):
        with patch.dict('os.environ', {'STUDIO_COUNT': '۳', 'STUDIO_MIN_SECONDS': '۰:۲۵',
                                     'STUDIO_MAX_SECONDS': '1:30'}):
            self.assertEqual(load_clip_settings('automatic'), (3, 25, 90))

    def test_bad_time_notation(self):
        with patch.dict('os.environ', {'STUDIO_MIN_SECONDS': '1:99'}):
            with self.assertRaisesRegex(ValueError, 'STUDIO_MIN_SECONDS'):
                load_clip_settings('automatic')


    def test_aparat_cdn_url_recognition(self):
        self.assertTrue(is_aparat_cdn('https://caspian28.asset.aparat.com/aparat-video/example-240p.mp4'))
        self.assertFalse(is_aparat_cdn('https://www.aparat.com/v/abc123'))
        self.assertFalse(is_aparat_cdn('https://fakeasset.aparat.com.evil.tld/x.mp4'))

    def test_aparat_cdn_403_has_actionable_message(self):
        from requests.exceptions import HTTPError
        response = Mock()
        response.status_code = 403
        response.raise_for_status.side_effect = HTTPError('403 Client Error', response=response)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with TemporaryDirectory() as tmp, patch('requests.get', return_value=response) as request_get:
            with self.assertRaisesRegex(RuntimeError, 'aparat.com/v/VIDEO_ID'):
                fetch_video('https://caspian28.asset.aparat.com/aparat-video/example.mp4', Path(tmp))
            self.assertEqual(request_get.call_args.kwargs['headers']['Referer'], 'https://www.aparat.com/')

    def test_aparat_cdn_accessible_video_still_works(self):
        response = Mock()
        response.headers = {'Content-Type': 'video/mp4', 'Content-Length': '3'}
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [b'abc']
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with TemporaryDirectory() as tmp, patch('requests.get', return_value=response):
            result = fetch_video('https://caspian28.asset.aparat.com/aparat-video/sample.mp4', Path(tmp))
            self.assertEqual(result.read_bytes(), b'abc')

    def test_aparat_page_is_sent_to_ytdlp(self):
        import sys
        from types import SimpleNamespace
        import types
        fake_module = types.ModuleType('yt_dlp')
        class FakeYoutubeDL:
            def __init__(self, opts):
                self.opts = opts
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
            def download(self, urls):
                self.__class__.last_urls = urls
                Path(self.opts['outtmpl'].replace('%(ext)s','mp4')).write_bytes(b'abc')
        fake_module.YoutubeDL = FakeYoutubeDL
        with TemporaryDirectory() as tmp, patch.dict(sys.modules, {'yt_dlp': fake_module}), patch('studio.download_aparat_api', side_effect=RuntimeError('mock API unavailable')):
            result = fetch_video('https://www.aparat.com/v/abc12', Path(tmp))
            self.assertEqual(FakeYoutubeDL.last_urls, ['https://www.aparat.com/v/abc12'])
            self.assertEqual(result.read_bytes(), b'abc')


if __name__ == '__main__':
    unittest.main()
