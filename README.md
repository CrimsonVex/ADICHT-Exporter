# ADICHT Exporter

Windows GUI tool for getting data out of ADInstruments LabChart `.adicht`
files without needing LabChart. Signals are averaged into time buckets
(1 minute by default) and written to CSV, Parquet or Excel.

<img width="702" height="552" alt="image" src="https://github.com/user-attachments/assets/a555e883-0d1e-4381-8ea7-7405715d0971" />


## Download

Get `AdichtExporter.exe` from the [releases page](../../releases). Single
exe, nothing to install. 64-bit Windows only.

The exe is unsigned, so SmartScreen will complain the first time you run
it. Click "More info" then "Run anyway".

## Usage

1. Add `.adicht` files with the file picker, or drag them in from Explorer
   (dropping a folder adds every `.adicht` file in it).
2. Untick any channels you don't want.
3. Pick a bucket size, format and output folder, then hit Export.

Each row in the output is the average of all samples in that bucket.
`time_s` is the bucket start time in seconds from the start of the
recording. Channels with different sample rates end up in the same table,
aligned by time. LabChart comments go in a `comments` column on the row
whose bucket contains them; untick the checkbox if you don't want them.

Output files are named `<name>_avg<X>s.<ext>`. Example with 60 second
buckets:

| time_s | Blood flow (BPU) | Oxygen (mmHg) | comments |
|-------:|-----------------:|--------------:|----------|
| 0      | 1410.52          | 11.63         |          |
| 60     | 1631.64          | 15.02         | Baseline: room air, MAP 60 mmHg |
| 120    | 1655.17          | 17.30         |          |

A blank cell means the channel has no data in that bucket, and the last
bucket is usually a partial average. Excel sheets max out at 1,048,575
rows; use a bigger bucket or CSV/Parquet if you hit that.

### Paused recordings

Every time sampling is stopped and started again, LabChart begins a new
record (block). A file that was paused a few times, say around an
intervention, has one record per stretch of recording. The file list shows
how many pauses each file has.

By default all of it goes into one file, with a `pause_s` column:

| time_s  | Blood flow (BPU) | pause_s | comments |
|--------:|-----------------:|--------:|----------|
| 180     | 1655.17          |         |          |
| 240     | 1649.80          |         |          |
| 662.4   | 1402.33          | 372.4   | Clamp on |
| 722.4   | 1398.91          |         |          |

- `pause_s` is blank except on the first row after a pause, where it is
  how long the recording was stopped, in seconds.
- `time_s` is real elapsed time since the recording first started, so it
  jumps by the length of the pause. Subtract the running total of
  `pause_s` if you want recorded time only.
- Buckets restart when recording restarts, so no average mixes data from
  either side of a pause. The last bucket before a pause is usually a
  partial one.
- If a channel's units change from one record to the next, it gets a
  column per unit rather than mixing them in one.
- Files with no pauses have no `pause_s` column.

Tick "Split into a separate file at each pause" to get one file per record
instead, named `<name>_record<N>_avg<X>s.<ext>`, each with `time_s`
starting again from 0 and no `pause_s` column.

## Running from source

```
pip install adi-reader numpy pyarrow openpyxl tkinterdnd2
python adicht_exporter.py
```

Needs 64-bit Python on Windows. adi-reader bundles the ADInstruments SDK
DLL, which is Windows only.

The export logic has tests that run anywhere, against a stand-in for the
SDK:

```
python -m unittest discover tests
```

## Building the exe

```
pip install pyinstaller
python -m PyInstaller AdichtExporter.spec --noconfirm
```

Build from the spec file, not the .py directly. It strips a bunch of
things the app doesn't use (pyarrow's Flight and Dataset DLLs, optional
openpyxl dependencies, non-Windows binaries) and roughly halves the exe
size. To check a build works:

```
dist\AdichtExporter.exe --selftest out.log path\to\file.adicht
dist\AdichtExporter.exe --smoketest
```

Both should exit 0, and the selftest log should end with SELFTEST PASS.

The ADInstruments SDK occasionally fails reads for no apparent reason.
The exporter retries and subdivides chunks automatically, so this is
handled, but it's worth knowing about if you're changing the read code.

## License

[GPL-3.0](LICENSE)
