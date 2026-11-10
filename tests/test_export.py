"""Engine tests for the export pipeline.

The ADInstruments SDK is a Windows DLL, so these run the real export code
against a small in-memory stand-in for the `adi` module and check the files
it writes. Run with:  python -m unittest discover tests
"""

import csv
import os
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timedelta

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import adicht_exporter as ex  # noqa: E402

T0 = datetime(2020, 1, 1, 9, 0, 0)


class FakeRecordTime:
    def __init__(self, trig_datetime, trig_start_delta=0):
        self.trig_datetime = trig_datetime
        self.trig_start_delta = trig_start_delta


class FakeComment:
    def __init__(self, tick_position, text):
        self.tick_position = tick_position
        self.text = text


class FakeRecord:
    def __init__(self, rec_id, n_ticks, start, tick_dt=1.0, comments=(),
                 trig_start_delta=0):
        self.id = rec_id
        self.n_ticks = n_ticks
        self.tick_dt = tick_dt
        self.comments = list(comments)
        if start is not None:
            self.record_time = FakeRecordTime(
                start + timedelta(seconds=trig_start_delta * tick_dt),
                trig_start_delta)


class FakeChannel:
    def __init__(self, ch_id, name, units, data, fs=1.0):
        """data: one array per record (empty = channel off in that record);
        units: one string per record, or a single string for all."""
        self.id = ch_id
        self.name = name
        self._data = [np.asarray(d, dtype=float) for d in data]
        self.units = ([units] * len(data) if isinstance(units, str)
                      else list(units))
        self.fs = [fs] * len(data)
        self.n_samples = [len(d) for d in self._data]

    def get_data(self, rec_id, start_sample, stop_sample):
        return self._data[rec_id - 1][start_sample - 1:stop_sample]


class FakeFile:
    def __init__(self, records, channels):
        self.records = records
        self.channels = channels


def records_with_pauses(durations, pauses, **first_kwargs):
    """Records of the given lengths (s, 1 tick/s) separated by `pauses`."""
    recs, start = [], T0
    for i, dur in enumerate(durations):
        recs.append(FakeRecord(i + 1, dur, start,
                               **(first_kwargs if i == 0 else {})))
        if i < len(pauses):
            start += timedelta(seconds=dur + pauses[i])
    return recs


class ExportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.files = {}
        fake = types.ModuleType("adi")
        fake.read_file = lambda path: self.files[path]
        self._real_adi = sys.modules.get("adi")
        sys.modules["adi"] = fake
        self.addCleanup(self._restore_adi)
        self.logs = []

    def _restore_adi(self):
        if self._real_adi is None:
            sys.modules.pop("adi", None)
        else:
            sys.modules["adi"] = self._real_adi

    def export(self, f, split, bucket_s=10.0, fmt="csv", comments=True,
               sel=None, name="rec"):
        self.files[name] = f
        if sel is None:
            sel = list(range(len(f.channels)))
        written = ex.export_one(
            path=name, out_base=name, sel_indices=sel,
            bucket_s=bucket_s, fmt=fmt, out_dir=self.tmp.name,
            include_comments=comments, split_on_pause=split,
            emit=lambda ev: self.logs.append(ev.get("msg", "")),
            cancel=threading.Event())
        return written

    @staticmethod
    def read_csv(path):
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        return rows[0], rows[1:]

    def column(self, path, header):
        head, rows = self.read_csv(path)
        return [r[head.index(header)] for r in rows]

    def floats(self, path, header):
        return [float(v) if v else None for v in self.column(path, header)]


class CombinedExport(ExportTestCase):
    def three_records(self):
        """25 s, 7 s and 30 s of data with 120.5 s and 3600 s pauses.
        Each record holds a constant, so any bucket that averaged across a
        pause would show up as a value between two constants."""
        recs = records_with_pauses([25, 7, 30], [120.5, 3600])
        recs[0].comments = [FakeComment(12, "baseline")]
        recs[2].comments = [FakeComment(1, "drug on"),
                            FakeComment(3, "dose 2"),
                            FakeComment(29, "end")]
        ch = FakeChannel(1, "Flow", "BPU",
                         [np.full(25, 1.0), np.full(7, 2.0), np.full(30, 3.0)])
        return FakeFile(recs, [ch])

    def test_one_file_on_elapsed_time_with_pause_column(self):
        written = self.export(self.three_records(), split=False)
        self.assertEqual([os.path.basename(w) for w in written],
                         ["rec_avg10s.csv"])
        head, rows = self.read_csv(written[0])
        self.assertEqual(head, ["time_s", "Flow (BPU)", "pause_s", "comments"])

        # rec 1 starts at 0; rec 2 at 25 + 120.5; rec 3 at 152.5 + 3600.
        self.assertEqual(self.floats(written[0], "time_s"),
                         [0, 10, 20, 145.5, 3752.5, 3762.5, 3772.5])
        self.assertEqual(self.floats(written[0], "pause_s"),
                         [None, None, None, 120.5, 3600, None, None])
        # Buckets restart at each record: no value mixes two records.
        self.assertEqual(self.floats(written[0], "Flow (BPU)"),
                         [1, 1, 1, 2, 3, 3, 3])
        self.assertEqual(self.column(written[0], "comments"),
                         ["", "baseline", "", "", "drug on | dose 2", "",
                          "end"])
        self.assertTrue(any("7 rows, 2 pauses" in m for m in self.logs))

    def test_split_writes_one_file_per_record_restarting_at_zero(self):
        written = self.export(self.three_records(), split=True)
        self.assertEqual([os.path.basename(w) for w in written],
                         ["rec_record1_avg10s.csv", "rec_record2_avg10s.csv",
                          "rec_record3_avg10s.csv"])
        for path, times, value in zip(written, ([0, 10, 20], [0], [0, 10, 20]),
                                      (1, 2, 3)):
            head, _ = self.read_csv(path)
            self.assertEqual(head, ["time_s", "Flow (BPU)", "comments"])
            self.assertEqual(self.floats(path, "time_s"), times)
            self.assertEqual(self.floats(path, "Flow (BPU)"),
                             [value] * len(times))
        self.assertEqual(self.column(written[2], "comments"),
                         ["drug on | dose 2", "", "end"])

    def test_combined_rows_equal_split_rows_in_order(self):
        f = self.three_records()
        combined = self.export(f, split=False, name="a")[0]
        split_vals = []
        for path in self.export(f, split=True, name="b"):
            split_vals += self.column(path, "Flow (BPU)")
        self.assertEqual(self.column(combined, "Flow (BPU)"), split_vals)

    def test_without_comments(self):
        written = self.export(self.three_records(), split=False,
                              comments=False)
        head, _ = self.read_csv(written[0])
        self.assertEqual(head, ["time_s", "Flow (BPU)", "pause_s"])

    def test_single_record_is_identical_in_both_modes(self):
        f = FakeFile([FakeRecord(1, 25, T0, comments=[FakeComment(3, "x")])],
                     [FakeChannel(1, "Flow", "BPU", [np.arange(25.0)])])
        a = self.export(f, split=False, name="a")[0]
        b = self.export(f, split=True, name="b")[0]
        self.assertEqual(os.path.basename(b), "b_avg10s.csv")
        with open(a, "rb") as fa, open(b, "rb") as fb:
            self.assertEqual(fa.read(), fb.read())
        head, _ = self.read_csv(a)
        self.assertEqual(head, ["time_s", "Flow (BPU)", "comments"])

    def test_bucket_means_and_mixed_sample_rates(self):
        recs = records_with_pauses([20, 20], [5])
        slow = FakeChannel(1, "Slow", "V", [np.arange(20.0), np.arange(20.0)])
        fast = FakeChannel(2, "Fast", "V",
                           [np.arange(40.0), np.arange(40.0) + 100], fs=2.0)
        path = self.export(FakeFile(recs, [slow, fast]), split=False)[0]
        self.assertEqual(self.floats(path, "time_s"), [0, 10, 25, 35])
        self.assertEqual(self.floats(path, "Slow (V)"), [4.5, 14.5, 4.5, 14.5])
        self.assertEqual(self.floats(path, "Fast (V)"),
                         [9.5, 29.5, 109.5, 129.5])

    def test_pretrigger_delay_is_part_of_the_recording_not_the_pause(self):
        # Record 1 starts 5 ticks before its trigger; the pause is still 60 s.
        recs = records_with_pauses([20, 20], [60], trig_start_delta=5)
        ch = FakeChannel(1, "Flow", "BPU", [np.ones(20), np.ones(20)])
        path = self.export(FakeFile(recs, [ch]), split=False)[0]
        self.assertEqual(self.floats(path, "pause_s"), [None, None, 60, None])
        self.assertEqual(self.floats(path, "time_s"), [0, 10, 80, 90])


class CombinedEdgeCases(ExportTestCase):
    def test_unknown_pause_length_is_written_as_zero_and_logged(self):
        for label, second_start in (("no record time", None),
                                    ("clock set back",
                                     T0 - timedelta(hours=1))):
            with self.subTest(label):
                self.logs.clear()
                recs = [FakeRecord(1, 20, T0), FakeRecord(2, 20, second_start)]
                ch = FakeChannel(1, "Flow", "BPU", [np.ones(20), np.ones(20)])
                path = self.export(FakeFile(recs, [ch]), split=False)[0]
                self.assertEqual(self.floats(path, "pause_s"),
                                 [None, None, 0, None])
                self.assertEqual(self.floats(path, "time_s"), [0, 10, 20, 30])
                self.assertTrue(any("pause before record 2 is unknown" in m
                                    for m in self.logs))

    def test_record_without_selected_data_is_skipped_and_spanned(self):
        # Channel is off in record 2, so the gap between the rows written
        # is pause + record 2 + pause.
        recs = records_with_pauses([20, 30, 20], [5, 7])
        ch = FakeChannel(1, "Flow", "BPU", [np.ones(20), [], np.ones(20)])
        path = self.export(FakeFile(recs, [ch]), split=False)[0]
        self.assertEqual(self.floats(path, "time_s"), [0, 10, 62, 72])
        self.assertEqual(self.floats(path, "pause_s"), [None, None, 42, None])
        self.assertTrue(any("record 2 has no data" in m for m in self.logs))

    def test_channel_off_in_one_record_stays_one_column(self):
        recs = records_with_pauses([20, 20], [5])
        a = FakeChannel(1, "A", "V", [np.ones(20), np.ones(20)])
        b = FakeChannel(2, "B", ["mmHg", ""], [np.full(20, 5.0), []])
        path = self.export(FakeFile(recs, [a, b]), split=False)[0]
        head, _ = self.read_csv(path)
        self.assertEqual(head, ["time_s", "A (V)", "B (mmHg)", "pause_s",
                                "comments"])
        self.assertEqual(self.floats(path, "B (mmHg)"), [5, 5, None, None])

    def test_units_changing_between_records_get_separate_columns(self):
        recs = records_with_pauses([20, 20], [5])
        a = FakeChannel(1, "A", ["V", "mmHg"],
                        [np.full(20, 1.0), np.full(20, 80.0)])
        b = FakeChannel(2, "B", "V", [np.ones(20), np.ones(20)])
        path = self.export(FakeFile(recs, [a, b]), split=False)[0]
        head, _ = self.read_csv(path)
        self.assertEqual(head, ["time_s", "A (V)", "A (mmHg)", "B (V)",
                                "pause_s", "comments"])
        self.assertEqual(self.floats(path, "A (V)"), [1, 1, None, None])
        self.assertEqual(self.floats(path, "A (mmHg)"), [None, None, 80, 80])
        self.assertTrue(any("units change between records" in m
                            for m in self.logs))

    def test_channel_named_like_a_built_in_column_is_renamed(self):
        recs = records_with_pauses([20, 20], [5])
        ch = FakeChannel(1, "pause_s", "", [np.ones(20), np.ones(20)])
        path = self.export(FakeFile(recs, [ch]), split=False)[0]
        head, _ = self.read_csv(path)
        self.assertEqual(head, ["time_s", "pause_s", "pause_s (2)",
                                "comments"])
        self.assertEqual(self.floats(path, "pause_s (2)"),
                         [None, None, 5, None])


class OtherFormats(ExportTestCase):
    def two_records(self):
        recs = records_with_pauses([20, 20], [90.25])
        recs[1].comments = [FakeComment(0, "restart")]
        ch = FakeChannel(1, "Flow", "BPU",
                         [np.full(20, 1.0), np.full(20, 2.0)])
        return FakeFile(recs, [ch])

    def test_parquet(self):
        import pyarrow.parquet as pq
        path = self.export(self.two_records(), split=False, fmt="parquet")[0]
        t = pq.read_table(path).to_pydict()
        self.assertEqual(t["time_s"], [0, 10, 110.25, 120.25])
        self.assertEqual(t["pause_s"], [None, None, 90.25, None])
        self.assertEqual(t["Flow (BPU)"], [1, 1, 2, 2])
        self.assertEqual(t["comments"], [None, None, "restart", None])

    def test_xlsx(self):
        from openpyxl import load_workbook
        path = self.export(self.two_records(), split=False, fmt="xlsx")[0]
        rows = list(load_workbook(path)["data"].iter_rows(values_only=True))
        self.assertEqual(rows[0], ("time_s", "Flow (BPU)", "pause_s",
                                   "comments"))
        self.assertEqual(rows[1:], [(0, 1, None, None), (10, 1, None, None),
                                    (110.25, 2, 90.25, "restart"),
                                    (120.25, 2, None, None)])


if __name__ == "__main__":
    unittest.main()
