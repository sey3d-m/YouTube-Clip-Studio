"""Offline tests for documented Aparat response shape and fallback behavior."""
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from studio import aparat_video_hash, aparat_media_candidates, download_aparat_api, fetch_video


def mock_response(*, json_data=None, content=b'media', status=None):
    res = Mock()
    res.headers = {'Content-Type': 'video/mp4', 'Content-Length': str(len(content))}
    res.json.return_value = json_data
    res.iter_content.return_value = [content]
    res.__enter__ = Mock(return_value=res)
    res.__exit__ = Mock(return_value=False)
    if status:
        from requests.exceptions import HTTPError
        res.status_code = status
        res.raise_for_status.side_effect = HTTPError(f'{status} error', response=res)
    return res


class AparatApiTests(unittest.TestCase):
    def test_rejects_other_domain_and_wrong_page(self):
        self.assertEqual(aparat_video_hash('https://www.aparat.com/v/zrvlc0v'), 'zrvlc0v')
        with self.assertRaises(ValueError):
            aparat_video_hash('https://www.aparat.com/playlist/12345')
        with self.assertRaises(ValueError):
            aparat_video_hash('https://evil.tld/v/zrvlc0v')

    def test_parses_quality_mirrors_and_ignores_external_urls(self):
        data = {'data': {'attributes': {'file_link_all': [
            {'profile': '240p', 'urls': ['https://caspian28.asset.aparat.com/v-240.mp4']},
            {'profile': '720p', 'urls': ['https://caspian28.asset.aparat.com/v-720.mp4',
                                         'https://evil.tld/v-720.mp4']},
            {'profile': '2160p', 'urls': ['https://caspian28.asset.aparat.com/v-2160.mp4']},
        ]}}}
        self.assertEqual(aparat_media_candidates(data), [
            'https://caspian28.asset.aparat.com/v-720.mp4',
            'https://caspian28.asset.aparat.com/v-240.mp4'
        ])

    def test_success_api_download_without_ytdlp(self):
        api = mock_response(json_data={'data': {'attributes': {'file_link_all': [
            {'profile': '720p', 'urls': ['https://caspian28.asset.aparat.com/video.mp4']}
        ]}}})
        video = mock_response(content=b'abc123')
        with TemporaryDirectory() as tmp, patch('requests.get', side_effect=[api, video]) as getter:
            result = fetch_video('https://www.aparat.com/v/zrvlc0v', Path(tmp))
            self.assertEqual(result.read_bytes(), b'abc123')
            self.assertEqual(getter.call_args_list[0].args[0],
                             'https://www.aparat.com/api/fa/v1/video/video/show/videohash/zrvlc0v')
            self.assertEqual(getter.call_args_list[1].kwargs['headers']['Referer'],
                             'https://www.aparat.com/v/zrvlc0v')

    def test_first_media_403_then_second_quality(self):
        api = mock_response(json_data={'data': {'attributes': {'file_link_all': [
            {'profile': '720p', 'urls': ['https://cdn.asset.aparat.com/720.mp4']},
            {'profile': '240p', 'urls': ['https://cdn.asset.aparat.com/240.mp4']}
        ]}}})
        forbidden = mock_response(status=403)
        video = mock_response(content=b'fallback-media')
        with TemporaryDirectory() as tmp, patch('requests.get', side_effect=[api, forbidden, video]):
            result = download_aparat_api('https://www.aparat.com/v/zrvlc0v', Path(tmp))
            self.assertEqual(result.read_bytes(), b'fallback-media')

    def test_api_403_falls_back_to_ytdlp(self):
        api = mock_response(status=403)
        module = types.ModuleType('yt_dlp')
        class FakeYDL:
            def __init__(self, opts): self.opts = opts
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def download(self, urls):
                Path(self.opts['outtmpl'].replace('%(ext)s', 'mp4')).write_bytes(b'yt-dlp-result')
        module.YoutubeDL = FakeYDL
        with TemporaryDirectory() as tmp, patch('requests.get', return_value=api), patch.dict(sys.modules, {'yt_dlp': module}):
            result = fetch_video('https://www.aparat.com/v/zrvlc0v', Path(tmp))
            self.assertEqual(result.read_bytes(), b'yt-dlp-result')

    def test_both_methods_fail_with_actionable_message(self):
        api = mock_response(status=403)
        module = types.ModuleType('yt_dlp')
        class BadYDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def download(self, urls): raise ValueError('Unable to extract og:title')
        module.YoutubeDL = BadYDL
        with TemporaryDirectory() as tmp, patch('requests.get', return_value=api), patch.dict(sys.modules, {'yt_dlp': module}):
            with self.assertRaisesRegex(RuntimeError, 'Google Drive'):
                fetch_video('https://www.aparat.com/v/zrvlc0v', Path(tmp))


if __name__ == '__main__':
    unittest.main()
