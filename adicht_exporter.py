"""ADIcht Exporter - desktop app for exporting LabChart .adicht files.

Pick one or more .adicht files, choose channels per file, and export
time-bucket-averaged data (default 1 minute buckets) to CSV, Parquet, or
Excel (.xlsx). LabChart comments are included as a text column in the
main table (optional). Files are processed in parallel on background
threads; the GUI stays live.

Run directly:            python adicht_exporter.py [file1.adicht ...]
Headless build check:    AdichtExporter.exe --selftest out.log file1.adicht ...
Build (PyInstaller):     python -m PyInstaller AdichtExporter.spec --noconfirm
  (the spec file trims unused pyarrow/adi/tkinterdnd2 components; after
   building, verify with --selftest and --smoketest)
"""

import csv
import os
import queue
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np

APP_NAME = "ADIcht Exporter"
CHUNK = 2_000_000            # samples per SDK read
MAX_PARALLEL_FILES = 4
MAX_PARALLEL_CHANNELS = 4    # concurrent channel reads within one file
# Cap total concurrent SDK reads across all files/channels. The ADI DLL is
# safe with one handle per thread (verified empirically), but unbounded
# thread fan-out just thrashes the disk.
_READ_SLOTS = threading.BoundedSemaphore(8)
DEFAULT_BUCKET_VALUE = "1"
DEFAULT_BUCKET_UNIT = "minutes"

CHECKED, UNCHECKED, PARTIAL = "☑", "☐", "◫"


class ExportCancelled(Exception):
    pass


# --------------------------------------------------------------------------
# Engine (no GUI dependencies)
# --------------------------------------------------------------------------

def read_chunk(ch, rec_id, start, stop, retries=3):
    """get_data with retry; the ADI SDK occasionally fails transiently on
    large reads, so on repeated failure split the range and recurse."""
    for attempt in range(retries):
        try:
            return ch.get_data(rec_id, start_sample=start, stop_sample=stop)
        except Exception:
            time.sleep(0.4 * (attempt + 1))
    if stop - start < 1000:
        raise Exception(f"unable to read samples {start}-{stop} "
                        f"of channel id {ch.id} after retries")
    mid = (start + stop) // 2
    return np.concatenate([read_chunk(ch, rec_id, start, mid, retries),
                           read_chunk(ch, rec_id, mid + 1, stop, retries)])


def load_metadata(path):
    """Open a file, extract plain-dict metadata, release the handle."""
    import adi
    f = adi.read_file(path)
    try:
        records = [{"id": r.id, "n_ticks": r.n_ticks, "tick_dt": r.tick_dt}
                   for r in f.records]
        channels = []
        for i, ch in enumerate(f.channels):
            channels.append({
                "index": i,
                "id": ch.id,
                "name": str(ch.name),
                "units": [str(u) for u in ch.units],
                "fs": [float(x) for x in ch.fs],
                "n_samples": [int(x) for x in ch.n_samples],
            })
        n_comments = sum(len(r.comments or []) for r in f.records)
    finally:
        del f
    duration = sum(r["n_ticks"] * r["tick_dt"] for r in records)
    return {"path": path, "records": records, "channels": channels,
            "n_comments": n_comments, "duration_s": duration}


def _col_header(ch_meta, rec_idx):
    units = ch_meta["units"][rec_idx] if rec_idx < len(ch_meta["units"]) else ""
    return f"{ch_meta['name']} ({units})" if units else ch_meta["name"]


def _dedupe(names):
    seen, out = {}, []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n} ({seen[n]})")
        else:
            seen[n] = 1
            out.append(n)
    return out


# Column convention: numeric columns are numpy arrays (NaN = missing);
# text columns (comments) are Python lists of str|None.

def _to_arrow_table(headers, columns):
    import pyarrow as pa
    arrays = {}
    for h, c in zip(headers, columns):
        if isinstance(c, np.ndarray):
            arrays[h] = pa.array(np.asarray(c, dtype=np.float64),
                                 from_pandas=True)  # NaN -> null
        else:
            arrays[h] = pa.array([v if v else None for v in c],
                                 type=pa.string())
    return pa.table(arrays)


def _csv_cell(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    return "" if np.isnan(v) else f"{v:.10g}"


def _write_csv(path, headers, columns):
    if len(columns[0]) > 50_000:
        # pyarrow's multithreaded CSV writer; null -> empty cell
        from pyarrow import csv as pacsv
        with open(path, "wb") as fh:
            fh.write(b"\xef\xbb\xbf")  # BOM so Excel decodes UTF-8 headers
            pacsv.write_csv(_to_arrow_table(headers, columns), fh)
        return
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for row in zip(*columns):
            w.writerow([_csv_cell(v) for v in row])


def _write_parquet(path, headers, columns):
    import pyarrow.parquet as pq
    pq.write_table(_to_arrow_table(headers, columns), path,
                   compression="zstd")


def _write_xlsx(path, headers, columns):
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    if len(columns[0]) > 1_048_575:
        raise Exception(
            f"{len(columns[0]):,} rows exceeds the .xlsx sheet limit "
            f"(1,048,575 data rows); use a larger bucket, CSV, or Parquet")

    def cell(v):
        if v is None or isinstance(v, str):
            return v
        return None if np.isnan(v) else float(v)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("data")
    bold = Font(bold=True)
    head = []
    for h in headers:
        c = WriteOnlyCell(ws, value=h)
        c.font = bold
        head.append(c)
    ws.append(head)
    for row in zip(*columns):
        ws.append([cell(v) for v in row])
    wb.save(path)


def _average_channel(path, rec_id, ch_index, bucket_s, n_buckets,
                     emit, cancel):
    """Bucket-average one channel of one record. Opens its OWN file handle
    so multiple channels of the same file can be read in parallel."""
    import adi
    f = adi.read_file(path)
    try:
        ch = f.channels[ch_index]
        rec_idx = rec_id - 1
        n = ch.n_samples[rec_idx]
        fs = ch.fs[rec_idx]
        col = np.full(n_buckets, np.nan)
        if n and fs > 0:
            spb = bucket_s * fs  # samples per bucket (may be float)
            nb = int((n - 1) // spb) + 1
            sums = np.zeros(nb)
            counts = np.zeros(nb)
            for start in range(1, n + 1, CHUNK):
                if cancel.is_set():
                    raise ExportCancelled()
                stop = min(start + CHUNK - 1, n)
                ids = (np.arange(start - 1, stop, dtype=np.float64)
                       // spb).astype(np.int64)
                b0, local = ids[0], ids - ids[0]
                m = local[-1] + 1
                counts[b0:b0 + m] += np.bincount(local, minlength=m)
                with _READ_SLOTS:
                    data = read_chunk(ch, rec_id, start, stop)
                sums[b0:b0 + m] += np.bincount(local, weights=data,
                                               minlength=m)
                emit({"type": "progress", "samples": stop - start + 1})
            valid = counts > 0
            means = np.full(nb, np.nan)
            means[valid] = sums[valid] / counts[valid]
            col[:nb] = means
        return col
    finally:
        del f


def export_one(path, out_base, sel_indices, bucket_s, fmt, out_dir,
               include_comments, emit, cancel):
    """Export one .adicht file: per record, one wide table of per-bucket
    means for the selected channels. Channels may have different sample
    rates; buckets are aligned by time. Channels are read in parallel,
    each on its own thread with its own file handle.

    include_comments: add a "comments" text column to the table; each
    LabChart comment lands in the bucket containing its timestamp."""
    import adi
    f = adi.read_file(path)
    written = []
    try:
        ext = {"parquet": "parquet", "xlsx": "xlsx"}.get(fmt, "csv")
        writer = {"parquet": _write_parquet,
                  "xlsx": _write_xlsx}.get(fmt, _write_csv)
        for rec in f.records:
            if cancel.is_set():
                raise ExportCancelled()
            rec_idx = rec.id - 1
            sel = [f.channels[i] for i in sel_indices]

            n_buckets = 0
            for ch in sel:
                n = ch.n_samples[rec_idx]
                fs = ch.fs[rec_idx]
                if n and fs > 0:
                    n_buckets = max(n_buckets,
                                    int((n - 1) // (bucket_s * fs)) + 1)
            if n_buckets == 0:
                emit({"type": "log",
                      "msg": f"{out_base}: record {rec.id} has no data "
                             f"for the selected channels; skipped"})
                continue

            chan_headers = [_col_header(f_ch_meta, rec_idx) for f_ch_meta in
                            ({"name": str(c.name),
                              "units": [str(u) for u in c.units]}
                             for c in sel)]
            time_col = np.round(np.arange(n_buckets) * float(bucket_s), 6)

            with ThreadPoolExecutor(
                    max_workers=min(MAX_PARALLEL_CHANNELS,
                                    len(sel_indices))) as ex:
                chan_cols = list(ex.map(
                    lambda i: _average_channel(path, rec.id, i, bucket_s,
                                               n_buckets, emit, cancel),
                    sel_indices))
            columns = [time_col] + chan_cols

            if include_comments:
                chan_headers.append("comments")
                com_col = [None] * n_buckets
                for c in (rec.comments or []):
                    b = min(int(c.tick_position * rec.tick_dt // bucket_s),
                            n_buckets - 1)
                    com_col[b] = (f"{com_col[b]} | {c.text}"
                                  if com_col[b] else c.text)
                columns.append(com_col)
            headers = ["time_s"] + _dedupe(chan_headers)

            suffix = f"_record{rec.id}" if len(f.records) > 1 else ""
            out_path = os.path.join(
                out_dir, f"{out_base}{suffix}_avg{bucket_s:g}s.{ext}")
            writer(out_path, headers, columns)
            written.append(out_path)
            emit({"type": "log",
                  "msg": f"Wrote {out_path} ({n_buckets:,} rows)"})

    finally:
        del f
    return written


def run_export(jobs, emit, cancel):
    """Orchestrate parallel per-file exports. Runs on its own thread."""
    ok = err = 0
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_FILES,
                                            len(jobs))) as ex:
        futs = {ex.submit(export_one, emit=emit, cancel=cancel, **j): j
                for j in jobs}
        for fut in as_completed(futs):
            job = futs[fut]
            name = os.path.basename(job["path"])
            try:
                fut.result()
                ok += 1
            except ExportCancelled:
                emit({"type": "log", "msg": f"Cancelled: {name}"})
            except Exception as e:
                err += 1
                emit({"type": "log", "msg": f"ERROR in {name}: {e}"})
    emit({"type": "all_done", "ok": ok, "err": err,
          "cancelled": cancel.is_set()})


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

def main_gui(initial_files):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext

    try:
        from tkinterdnd2 import TkinterDnD, DND_FILES
        have_dnd = True
    except Exception:
        have_dnd = False

    try:  # crisp text on HiDPI displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = TkinterDnD.Tk() if have_dnd else tk.Tk()
    root.title(APP_NAME)
    root.geometry("880x640")
    root.minsize(700, 520)

    ui_queue = queue.Queue()
    cancel_event = threading.Event()
    state = {"files": {}, "next_key": 0, "running": False,
             "total_samples": 0, "done_samples": 0}
    # files[key] = {"path", "meta" or None, "iid", "checked": {ch_index: bool}}

    # ---- layout ----------------------------------------------------------
    top = ttk.Frame(root, padding=(10, 10, 10, 4))
    top.pack(fill="x")
    btn_add = ttk.Button(top, text="Add files…")
    btn_remove = ttk.Button(top, text="Remove selected")
    btn_clear = ttk.Button(top, text="Clear all")
    btn_add.pack(side="left")
    btn_remove.pack(side="left", padx=(6, 0))
    btn_clear.pack(side="left", padx=(6, 0))
    hint = ("Click checkboxes to include/exclude; drag .adicht files here "
            "from Explorer." if have_dnd else
            "Click the checkbox to include/exclude a file or channel.")
    ttk.Label(top, text=hint).pack(side="left", padx=(14, 0))

    mid = ttk.Frame(root, padding=(10, 4))
    mid.pack(fill="both", expand=True)
    tree = ttk.Treeview(mid, columns=("info",), selectmode="extended")
    tree.heading("#0", text="File / Channel", anchor="w")
    tree.heading("info", text="Details", anchor="w")
    tree.column("#0", width=380, stretch=True)
    tree.column("info", width=340, stretch=True)
    vsb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)
    tree.pack(side="left", fill="both", expand=True)
    vsb.pack(side="left", fill="y")

    opts = ttk.LabelFrame(root, text="Export options", padding=10)
    opts.pack(fill="x", padx=10, pady=4)

    row0 = ttk.Frame(opts)
    row0.grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(row0, text="Average over time buckets of").pack(side="left")
    bucket_var = tk.StringVar(value=DEFAULT_BUCKET_VALUE)
    ttk.Entry(row0, textvariable=bucket_var, width=8).pack(
        side="left", padx=(6, 4))
    unit_var = tk.StringVar(value=DEFAULT_BUCKET_UNIT)
    ttk.Combobox(row0, textvariable=unit_var, width=9, state="readonly",
                 values=("seconds", "minutes", "hours")).pack(side="left")
    ttk.Label(row0, text="Format:").pack(side="left", padx=(24, 4))
    fmt_var = tk.StringVar(value="csv")
    ttk.Radiobutton(row0, text="CSV", variable=fmt_var,
                    value="csv").pack(side="left")
    ttk.Radiobutton(row0, text="Parquet", variable=fmt_var,
                    value="parquet").pack(side="left", padx=(8, 0))
    ttk.Radiobutton(row0, text="Excel (.xlsx)", variable=fmt_var,
                    value="xlsx").pack(side="left", padx=(8, 0))

    comments_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(opts, text="Include LabChart comments "
                               "(as a comments column)",
                    variable=comments_var).grid(
        row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

    ttk.Label(opts, text="Output folder:").grid(
        row=2, column=0, sticky="w", pady=(6, 0))
    outdir_var = tk.StringVar(value="")
    out_entry = ttk.Entry(opts, textvariable=outdir_var)
    out_entry.grid(row=2, column=1, sticky="ew", padx=(6, 4), pady=(6, 0))
    btn_browse = ttk.Button(opts, text="Browse…")
    btn_browse.grid(row=2, column=2, sticky="w", pady=(6, 0))
    opts.columnconfigure(1, weight=1)

    bottom = ttk.Frame(root, padding=(10, 4, 10, 10))
    bottom.pack(fill="both")
    btn_export = ttk.Button(bottom, text="Export")
    btn_cancel = ttk.Button(bottom, text="Cancel", state="disabled")
    btn_cancel.pack(side="right")
    btn_export.pack(side="right", padx=(0, 6))
    progress = ttk.Progressbar(bottom, mode="determinate")
    progress.pack(side="left", fill="x", expand=True, padx=(0, 10))

    log_box = scrolledtext.ScrolledText(root, height=8, state="disabled",
                                        font=("Consolas", 9))
    log_box.pack(fill="both", padx=10, pady=(0, 10))

    def log(msg):
        log_box.configure(state="normal")
        log_box.insert("end", time.strftime("[%H:%M:%S] ") + msg + "\n")
        log_box.see("end")
        log_box.configure(state="disabled")

    # ---- checkbox tree ---------------------------------------------------
    def file_of_iid(iid):
        for key, info in state["files"].items():
            if info["iid"] == iid:
                return key, None
            for ci, cid in info.get("chan_iids", {}).items():
                if cid == iid:
                    return key, ci
        return None, None

    def set_label(iid, mark, label):
        tree.item(iid, text=f"{mark} {label}")

    def refresh_file_mark(key):
        info = state["files"][key]
        if not info.get("meta"):
            return
        vals = list(info["checked"].values())
        mark = CHECKED if all(vals) else UNCHECKED if not any(vals) else PARTIAL
        set_label(info["iid"], mark, os.path.basename(info["path"]))

    def toggle(iid):
        if state["running"]:
            return
        key, ci = file_of_iid(iid)
        if key is None or not state["files"][key].get("meta"):
            return
        info = state["files"][key]
        if ci is None:  # file row: set all channels to the new state
            new = not all(info["checked"].values())
            for i in info["checked"]:
                info["checked"][i] = new
                ch = info["meta"]["channels"][i]
                set_label(info["chan_iids"][i],
                          CHECKED if new else UNCHECKED, ch["name"])
        else:
            info["checked"][ci] = not info["checked"][ci]
            ch = info["meta"]["channels"][ci]
            set_label(info["chan_iids"][ci],
                      CHECKED if info["checked"][ci] else UNCHECKED,
                      ch["name"])
        refresh_file_mark(key)

    def on_click(event):
        if tree.identify_column(event.x) == "#0":
            iid = tree.identify_row(event.y)
            if iid:
                elem = tree.identify_element(event.x, event.y)
                if "indicator" not in elem:  # let expand arrows work
                    toggle(iid)

    tree.bind("<Button-1>", on_click)
    tree.bind("<space>", lambda e: [toggle(i) for i in tree.selection()])

    # ---- adding files ----------------------------------------------------
    def fmt_duration(s):
        h, rem = divmod(int(s), 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}"

    def add_paths(paths):
        expanded = []
        for p in paths:
            if os.path.isdir(p):  # dropped folder: take its .adicht files
                expanded.extend(sorted(
                    os.path.join(p, x) for x in os.listdir(p)
                    if x.lower().endswith(".adicht")))
            else:
                expanded.append(p)
        existing = {i["path"] for i in state["files"].values()}
        new_keys = []
        for p in expanded:
            p = os.path.abspath(p)
            if p in existing or not p.lower().endswith(".adicht"):
                continue
            existing.add(p)
            key = state["next_key"]
            state["next_key"] += 1
            iid = tree.insert("", "end",
                              text=f"{PARTIAL} {os.path.basename(p)}",
                              values=("loading…",), open=True)
            state["files"][key] = {"path": p, "meta": None, "iid": iid,
                                   "checked": {}, "chan_iids": {}}
            new_keys.append(key)
            if not outdir_var.get():
                outdir_var.set(os.path.dirname(p))

        def loader():
            for key in new_keys:
                path = state["files"][key]["path"]
                try:
                    meta = load_metadata(path)
                    ui_queue.put({"type": "meta", "key": key, "meta": meta})
                except Exception as e:
                    ui_queue.put({"type": "meta_error", "key": key,
                                  "error": str(e)})
        if new_keys:
            threading.Thread(target=loader, daemon=True).start()

    def on_meta(ev):
        key, meta = ev["key"], ev["meta"]
        if key not in state["files"]:
            return
        info = state["files"][key]
        info["meta"] = meta
        nrec = len(meta["records"])
        tree.item(info["iid"], values=(
            f"{nrec} record{'s' if nrec != 1 else ''}, "
            f"{fmt_duration(meta['duration_s'])}, "
            f"{meta['n_comments']} comments",))
        for ch in meta["channels"]:
            fs = max(ch["fs"]) if ch["fs"] else 0
            units = next((u for u in ch["units"] if u), "")
            detail = (f"{units + ', ' if units else ''}"
                      f"{fs:g} Hz, {sum(ch['n_samples']):,} samples")
            cid = tree.insert(info["iid"], "end",
                              text=f"{CHECKED} {ch['name']}",
                              values=(detail,))
            info["chan_iids"][ch["index"]] = cid
            info["checked"][ch["index"]] = True
        refresh_file_mark(key)
        log(f"Loaded {os.path.basename(meta['path'])}: "
            f"{len(meta['channels'])} channels, "
            f"{fmt_duration(meta['duration_s'])}")

    def on_meta_error(ev):
        key = ev["key"]
        if key not in state["files"]:
            return
        info = state["files"][key]
        tree.item(info["iid"], values=(f"ERROR: {ev['error']}",))
        log(f"Failed to load {os.path.basename(info['path'])}: "
            f"{ev['error']}")

    def remove_selected():
        if state["running"]:
            return
        for iid in tree.selection():
            key, ci = file_of_iid(iid)
            if key is not None and ci is None:
                tree.delete(iid)
                del state["files"][key]

    def clear_all():
        if state["running"]:
            return
        tree.delete(*tree.get_children())
        state["files"].clear()

    btn_add.configure(command=lambda: add_paths(
        filedialog.askopenfilenames(
            title="Select .adicht files",
            filetypes=[("LabChart files", "*.adicht"), ("All files", "*")])))
    btn_remove.configure(command=remove_selected)
    btn_clear.configure(command=clear_all)
    btn_browse.configure(command=lambda: (
        lambda d: outdir_var.set(d) if d else None)(
        filedialog.askdirectory(title="Choose output folder")))

    if have_dnd:
        def on_drop(event):
            if not state["running"]:
                add_paths(root.tk.splitlist(event.data))
            return "copy"
        for target in (root, tree, log_box):
            target.drop_target_register(DND_FILES)
            target.dnd_bind("<<Drop>>", on_drop)

    # ---- export ----------------------------------------------------------
    def set_running(running):
        state["running"] = running
        st = "disabled" if running else "normal"
        for b in (btn_add, btn_remove, btn_clear, btn_export, btn_browse):
            b.configure(state=st)
        btn_cancel.configure(state="normal" if running else "disabled")

    def start_export():
        try:
            val = float(bucket_var.get())
            if val <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Time bucket must be a positive "
                                           "number.")
            return
        bucket_s = val * {"seconds": 1.0, "minutes": 60.0,
                          "hours": 3600.0}[unit_var.get()]

        out_dir = outdir_var.get().strip()
        if not out_dir:
            messagebox.showerror(APP_NAME, "Choose an output folder.")
            return
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Cannot create output folder:\n{e}")
            return

        jobs, total = [], 0
        used_names = set()
        for info in state["files"].values():
            meta = info.get("meta")
            if not meta:
                continue
            sel = sorted(i for i, on in info["checked"].items() if on)
            if not sel:
                continue
            base = os.path.splitext(os.path.basename(info["path"]))[0]
            out_base, n = base, 2
            while out_base in used_names:
                out_base = f"{base}_{n}"
                n += 1
            used_names.add(out_base)
            for ci in sel:
                total += sum(meta["channels"][ci]["n_samples"])
            jobs.append({"path": info["path"], "out_base": out_base,
                         "sel_indices": sel, "bucket_s": bucket_s,
                         "fmt": fmt_var.get(), "out_dir": out_dir,
                         "include_comments": comments_var.get()})
        if not jobs:
            messagebox.showerror(APP_NAME, "No channels selected.")
            return

        cancel_event.clear()
        state["total_samples"] = max(total, 1)
        state["done_samples"] = 0
        progress.configure(maximum=state["total_samples"], value=0)
        set_running(True)
        log(f"Exporting {len(jobs)} file(s), bucket = {bucket_s:g} s, "
            f"format = {fmt_var.get().upper()} → {out_dir}")
        threading.Thread(target=run_export,
                         args=(jobs, ui_queue.put, cancel_event),
                         daemon=True).start()

    def do_cancel():
        cancel_event.set()
        log("Cancelling…")

    btn_export.configure(command=start_export)
    btn_cancel.configure(command=do_cancel)

    # ---- queue pump ------------------------------------------------------
    def poll():
        try:
            while True:
                ev = ui_queue.get_nowait()
                t = ev["type"]
                if t == "log":
                    log(ev["msg"])
                elif t == "progress":
                    state["done_samples"] += ev["samples"]
                    progress.configure(value=state["done_samples"])
                elif t == "meta":
                    on_meta(ev)
                elif t == "meta_error":
                    on_meta_error(ev)
                elif t == "all_done":
                    set_running(False)
                    progress.configure(value=0)
                    if ev["cancelled"]:
                        log("Export cancelled.")
                    else:
                        log(f"Finished: {ev['ok']} file(s) exported, "
                            f"{ev['err']} error(s).")
                        if ev["err"]:
                            messagebox.showwarning(
                                APP_NAME, f"Finished with {ev['err']} "
                                          f"error(s) - see the log.")
                        else:
                            messagebox.showinfo(
                                APP_NAME, f"Export complete "
                                          f"({ev['ok']} file(s)).")
        except queue.Empty:
            pass
        root.after(80, poll)

    def on_close():
        cancel_event.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    poll()
    if initial_files:
        add_paths(initial_files)
    smoke_issues = []
    if "--smoketest" in sys.argv:
        def check_layout():
            root.update_idletasks()
            win_w = root.winfo_width()
            for label, frame in (("toolbar", top), ("options", opts),
                                 ("buttons", bottom)):
                need = frame.winfo_reqwidth()
                if need > win_w:
                    smoke_issues.append(
                        f"{label} needs {need}px, window is {win_w}px")
        root.after(1200, check_layout)
        root.after(2500, on_close)
    root.mainloop()
    return have_dnd, smoke_issues


# --------------------------------------------------------------------------
# Headless selftest (verifies a frozen build: DLL, numpy, pyarrow, export)
# --------------------------------------------------------------------------

def run_selftest(log_path, files):
    lines = [f"python {sys.version}", f"frozen: {getattr(sys, 'frozen', False)}"]
    code = 0
    try:
        import adi  # noqa: F401
        lines.append("adi import: OK")
        import pyarrow  # noqa: F401
        lines.append(f"pyarrow import: OK ({pyarrow.__version__})")
        import openpyxl  # noqa: F401
        lines.append(f"openpyxl import: OK ({openpyxl.__version__})")
        for path in files:
            meta = load_metadata(path)
            lines.append(f"{path}: {len(meta['channels'])} channels, "
                         f"{meta['duration_s']:.1f} s")
            out_dir = os.path.dirname(os.path.abspath(log_path))
            base = "selftest_" + os.path.splitext(os.path.basename(path))[0]
            evs = []
            runs = [(base, f, True) for f in ("csv", "parquet", "xlsx")]
            runs.append((base + "_nc", "csv", False))
            for out_base, fmt, inc in runs:
                written = export_one(
                    path=path, out_base=out_base,
                    sel_indices=[c["index"] for c in meta["channels"]],
                    bucket_s=60.0, fmt=fmt, out_dir=out_dir,
                    include_comments=inc, emit=evs.append,
                    cancel=threading.Event())
                for w in written:
                    lines.append(f"  wrote {w} ({os.path.getsize(w):,} bytes)")
        lines.append("SELFTEST PASS")
    except Exception:
        lines.append(traceback.format_exc())
        lines.append("SELFTEST FAIL")
        code = 1
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return code


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "--selftest":
        sys.exit(run_selftest(argv[1], argv[2:]))
    files = [a for a in argv if a.lower().endswith(".adicht")]
    have_dnd, smoke_issues = main_gui(files)
    if "--smoketest" in argv:
        if not have_dnd:
            sys.exit(3)  # drag-and-drop support missing from this build
        if smoke_issues:
            sys.exit(4)  # widgets clipped at default window size


if __name__ == "__main__":
    main()
