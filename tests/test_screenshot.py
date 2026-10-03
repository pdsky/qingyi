import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import screenshot
from ai_backend import Cancelled


class ScreenshotTests(unittest.TestCase):
    def test_capture_only_selected_crop_without_clipboard_or_upload(self):
        cancel = threading.Event()
        png = screenshot.PNG_HEADER + b'test fixture'
        with patch('screenshot.run_process', return_value=(0, png, b'')) as run:
            self.assertEqual(screenshot.capture(cancel), png)
        args = run.call_args.args[0]
        self.assertEqual(args, ['flameshot', 'gui', '--raw', '--accept-on-select'])

    def test_cancel_is_distinct_from_capture_failure(self):
        with patch('screenshot.run_process', return_value=(1, b'', b'flameshot: Screenshot aborted.')):
            with self.assertRaises(Cancelled):
                screenshot.capture(threading.Event())
        with patch('screenshot.run_process', return_value=(1, b'', b'Unable to capture screen')):
            with self.assertRaises(screenshot.ScreenshotError):
                screenshot.capture(threading.Event())

    def test_temporary_image_is_private_and_removed_on_all_outcomes(self):
        for code, output in [(0, b'Hello world'), (1, b''), (0, b'')]:
            paths = []
            def engine(args, *_):
                image = Path(args[1]); paths.append(image)
                self.assertEqual(image.stat().st_mode & 0o777, 0o600)
                self.assertEqual(image.read_bytes(), screenshot.PNG_HEADER)
                return code, output, b''
            with patch('screenshot.run_process', side_effect=engine):
                if code or not output:
                    with self.assertRaises(screenshot.ScreenshotError):
                        screenshot.recognize(screenshot.PNG_HEADER, threading.Event(), ('engine', {}))
                else:
                    self.assertEqual(screenshot.recognize(screenshot.PNG_HEADER, threading.Event(), ('engine', {})), 'Hello world')
            self.assertFalse(paths[0].exists())
            self.assertFalse(paths[0].parent.exists())

    def test_canceled_ocr_removes_image(self):
        paths = []
        def engine(args, *_):
            paths.append(Path(args[1])); raise Cancelled()
        with patch('screenshot.run_process', side_effect=engine), self.assertRaises(Cancelled):
            screenshot.recognize(screenshot.PNG_HEADER, threading.Event(), ('engine', {}))
        self.assertFalse(paths[0].parent.exists())

    def test_child_process_is_stopped_on_cancel_and_timeout(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = str(Path(folder) / 'late-result')
            code = 'import time,pathlib; time.sleep(2); pathlib.Path(%r).touch()' % marker
            cancel = threading.Event()
            timer = threading.Timer(.15, cancel.set); timer.start()
            with self.assertRaises(Cancelled):
                screenshot.run_process([sys.executable, '-c', code], cancel, 5)
            timer.join()
            with self.assertRaises(screenshot.ScreenshotError):
                screenshot.run_process([sys.executable, '-c', code], threading.Event(), .1)
            self.assertFalse(Path(marker).exists())


if __name__ == '__main__':
    unittest.main()
