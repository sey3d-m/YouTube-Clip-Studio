import json
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from studio import run_cmd, video_duration, write_progress


class DiagnosticsTests(unittest.TestCase):
    def test_probe_reports_valid_video(self):
        with TemporaryDirectory() as td:
            path = Path(td) / 'short.mp4'
            subprocess.run(['ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                            '-f', 'lavfi', '-i', 'color=c=black:s=160x90:r=10:d=1.5',
                            '-c:v', 'mpeg4', str(path)], check=True, capture_output=True)
            duration = video_duration(path, Path(td) / 'output')
            self.assertGreater(duration, 1)
            report = json.loads((Path(td) / 'output/media-probe.json').read_text())
            self.assertTrue(report['valid'])
            self.assertTrue(report['mp4_ftyp_present'])

    def test_damaged_file_reports_failure(self):
        with TemporaryDirectory() as td:
            path = Path(td) / 'bad.mp4'
            path.write_bytes(b'garbage' * 500)
            with self.assertRaisesRegex(RuntimeError, 'cannot be probed'):
                video_duration(path, Path(td) / 'output')
            report = json.loads((Path(td) / 'output/media-probe.json').read_text())
            self.assertFalse(report['valid'])

    def test_timeout_has_clear_message(self):
        with patch('studio.subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='ffprobe', timeout=90)):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                run_cmd(['ffprobe', '-version'])

    def test_progress_checkpoint_is_persistent(self):
        with TemporaryDirectory() as td:
            write_progress(Path(td), 'downloaded', bytes=123)
            report = json.loads((Path(td) / 'run-status.json').read_text())
            self.assertEqual((report['phase'], report['bytes']), ('downloaded', 123))


if __name__ == '__main__':
    unittest.main()
