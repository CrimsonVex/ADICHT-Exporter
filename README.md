# ADICHT Exporter

A Windows desktop app for exporting ADInstruments LabChart `.adicht` recordings
to **CSV**, **Parquet**, or **Excel (.xlsx)** — without needing LabChart installed.

Raw signals are averaged into time buckets (default 1 minute), turning
multi-gigabyte high-frequency recordings into compact, analysis-ready tables.

## Download

Grab `AdichtExporter.exe` from the [Releases](../../releases) page.
It is fully standalone — no Python, no LabChart, no installation required.
Windows 64-bit only.

> Windows SmartScreen may warn on first run because the exe is unsigned.
> Choose **More info → Run anyway**.

## Features

- Open one or more `.adicht` files via the file picker, by dragging them from
  Explorer onto the window (folders work too), or by dropping them on the exe icon
- Per-file channel selection with checkboxes
- Time-bucket averaging: each output row is the true mean of every sample in
  its bucket (seconds, minutes, or hours; default 1 minute)
- Channels with different sample rates are aligned by time into one wide table
- LabChart comments included as a `comments` column (each comment lands in the
  bucket containing its timestamp) — or untick to omit them
- Export to CSV (UTF-8, Excel-friendly), Parquet (zstd), or xlsx
- Files export in parallel with live progress and cancel

## Output

One file per record, named `<name>[_recordN]_avg<X>s.<ext>`:

| time_s | Blood flow (BPU) | Oxygen (mmHg) | Temperature (°C) | comments |
|-------:|-----------------:|--------------:|-----------------:|----------|
| 0      | 1410.52          | 11.63         | 37.28            |          |
| 60     | 1631.64          | 15.02         | 37.24            | Baseline: room air, MAP 60 mmHg |
| 120    | 1655.17          | 17.30         | 37.25            |          |

`time_s` is the bucket start time in seconds from the start of the record.
Column headers carry each channel's name and units. Empty cells mean the
channel has no data in that bucket. The final bucket may be a partial average
if the recording does not divide evenly.

## Running from source

```
pip install adi-reader numpy pyarrow openpyxl tkinterdnd2
python adicht_exporter.py
```

Requires 64-bit Python on Windows (the ADInstruments SDK DLL bundled with
`adi-reader` is Windows-only).

## Building the exe

```
pip install pyinstaller
python -m PyInstaller AdichtExporter.spec --noconfirm
```

The spec file trims unused components (pyarrow Flight/Dataset engines,
optional openpyxl dependencies, non-Windows binaries) to keep the exe small.
After building, verify the frozen app:

```
dist\AdichtExporter.exe --selftest out.log path\to\some.adicht
dist\AdichtExporter.exe --smoketest
```

Both must exit 0; the selftest writes `SELFTEST PASS` to its log after
exercising every export format against the given file.

## Notes

- Reads are chunked and retried: the ADInstruments SDK can fail transiently on
  large reads, so the exporter retries and subdivides automatically.
- Excel's sheet limit is 1,048,575 data rows; choose a larger bucket (or CSV/
  Parquet) if an export would exceed it.

## License

[GPL-3.0](LICENSE)
