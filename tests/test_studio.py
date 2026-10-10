import unittest
from studio import Candidate, normalize_clips, seconds_to_hms, youtube_timestamp_link


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

    def test_format_and_link(self):
        self.assertEqual(seconds_to_hms(3661.2), '01:01:01')
        self.assertEqual(youtube_timestamp_link('https://youtu.be/abcdefghijk', 70), 'https://www.youtube.com/watch?v=abcdefghijk&t=70s')
        self.assertEqual(youtube_timestamp_link('https://example.com/x.mp4', 70), '')


if __name__ == '__main__':
    unittest.main()
