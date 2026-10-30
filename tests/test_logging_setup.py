import logging
import unittest
from unittest import mock

from blackframe.logging_setup import RepeatFilter


def _record(msg="Polling fallito: %s", level=logging.WARNING, args=("timeout",)):
    return logging.LogRecord("blackframe.x", level, __file__, 1, msg, args, None)


class RepeatFilterTests(unittest.TestCase):
    def test_info_is_never_throttled(self):
        flt = RepeatFilter(burst=1, window_sec=60)
        self.assertTrue(all(flt.filter(_record(level=logging.INFO)) for _ in range(20)))

    def test_burst_then_marker_then_silence(self):
        flt = RepeatFilter(burst=3, window_sec=600)
        results = [flt.filter(_record(args=(str(i),))) for i in range(10)]
        self.assertEqual(results, [True] * 4 + [False] * 6)

    def test_marker_drops_traceback(self):
        flt = RepeatFilter(burst=1, window_sec=600)
        flt.filter(_record())
        marker = _record()
        marker.exc_info = (RuntimeError, RuntimeError("x"), None)
        self.assertTrue(flt.filter(marker))
        self.assertIsNone(marker.exc_info)
        self.assertIn("soppressi", marker.msg)

    def test_new_window_reports_suppressed_count(self):
        flt = RepeatFilter(burst=2, window_sec=10)
        clock = [100.0]
        with mock.patch("blackframe.logging_setup.time.monotonic", side_effect=lambda: clock[0]):
            for _ in range(7):
                flt.filter(_record())
            clock[0] += 11
            record = _record()
            self.assertTrue(flt.filter(record))
        self.assertIn("soppressi 5", record.msg)

    def test_different_call_sites_are_independent(self):
        flt = RepeatFilter(burst=1, window_sec=600)
        self.assertTrue(flt.filter(_record(msg="A %s")))
        self.assertTrue(flt.filter(_record(msg="B %s")))


if __name__ == "__main__":
    unittest.main()
