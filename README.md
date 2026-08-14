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
`time_s` is the bucket start time in seconds from the start of the record.
Channels with different sample rates end up in the same table, aligned by
time. LabChart comments go in a `comments` column on the row whose bucket
contains them; untick the checkbox if you don't want them.

Output files are named `<name>_avg<X>s.<ext>`, one per record. Example
with 60 second buckets:

| time_s | Blood flow (BPU) | Oxygen (mmHg) | comments |
|-------:|-----------------:|--------------:|----------|
| 0      | 1410.52          | 11.63         |          |
| 60     | 1631.64          | 15.02         | Baseline: room air, MAP 60 mmHg |
| 120    | 1655.17          | 17.30         |          |

A blank cell means the channel has no data in that bucket, and the last
bucket is usually a partial average. Excel sheets max out at 1,048,575
rows; use a bigger bucket or CSV/Parquet if you hit that.

## Running from source

```
pip install adi-reader numpy pyarrow openpyxl tkinterdnd2
python adicht_exporter.py
```

Needs 64-bit Python on Windows. adi-reader bundles the ADInstruments SDK
DLL, which is Windows only.

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
