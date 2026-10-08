"""Picoscope Spectrum Manager - GUI in the style of LabSpectrumManager.

Loads the CSV files exported by the Picoscope software (columns: Time, Channel A, Channel B),
converts time to wavelength, applies smoothing and resampling, and manages several spectra at
once: data table, plot with cursor, list of the loaded spectra with metadata, merged CSV export.

It also recognises CSV files that are already converted (two columns: wavelength,
intensity, no header) and loads them directly.

If the raw file name ends with _<WL>_<scanspeed> (e.g.
"pyranine_ex_300_240.csv") the conversion parameters are read from the
name; otherwise the values of the fields in the right panel are used.

Dependencies: numpy, pandas, matplotlib, tkinterdnd2 (optional, for drag and drop).
The plots follow the shared Origin style of PlotStyleKit when the sibling repo is present.
"""

import csv
import os
import pickle
import re
import sys
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk, Listbox

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:
    HAS_DND = False


def _load_origin_style():
    """Load the shared Origin plot style (PlotStyleKit): a local copy first, then the sibling repo."""
    import importlib.util as ilu
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(here, 'origin_style.py'),
                 os.path.join(here, '..', 'PlotStyleKit', 'origin_style.py')):
        if os.path.isfile(path):
            spec = ilu.spec_from_file_location('origin_style', path)
            mod = ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


try:
    origin_style = _load_origin_style()
    if origin_style is not None:
        origin_style.applica_rcparams()
except Exception:
    origin_style = None

SMOOTH_WINDOW = 100   # like smooth(x,100) in MATLAB (centred moving average)
WL_STEP = 1.0         # output grid: exactly 1 nm, integer wavelengths
ASSUMED_FINAL_WL = 1000.0  # cut assumed at every load; the Final WL field is
                           # then updated to the actual end

# file name "sample_<initial WL>_<scanspeed>" (e.g. "sample_ex_300_240"
# -> scan starting at 300 nm at 240 nm/min)
RE_PARAMS_IN_NAME = re.compile(r'_(\d{3,4})_(\d{2,4})$')


def moving_average(x, window):
    """Centred moving average, equivalent to MATLAB's smooth().

    At the edges the window shrinks (mean of the available points),
    as MATLAB's smooth() does.
    """
    n = len(x)
    if n == 0 or window <= 1:
        return np.asarray(x, dtype=float)
    half = window // 2
    cum = np.concatenate(([0.0], np.cumsum(x, dtype=float)))
    out = np.empty(n)
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        out[i] = (cum[hi] - cum[lo]) / (hi - lo)
    return out


def resample_integer_wl(x, y, x_last=None):
    """Resample a spectrum on the WL_STEP grid with integer wavelengths.

    The first point is rounded to the nearest nm (any extrapolation is a
    fraction of a nm, which np.interp limits to the first value).
    """
    wl_first = np.floor(x[0] + 0.5)
    if x_last is None:
        x_last = x[-1]
    wl = np.arange(wl_first, np.floor(x_last) + WL_STEP / 2, WL_STEP)
    if len(wl) < 2:
        raise ValueError("Range too short for the 1 nm grid.")
    yi = np.round(np.interp(wl, x, y), 6)
    if float(WL_STEP).is_integer():
        wl = wl.astype(int)   # 300, 301, ... without decimals in table/export
    return wl, yi


def convert_time_to_wl(time_s, intensity, start_wl, scanspeed, end_wl):
    """Convert time -> wavelength and resample on an integer grid.

    Moving-average smoothing like the original MATLAB GUI, then interpolation
    of the signal on the integer wavelengths at WL_STEP steps (300, 301, ...).
    Returns (wl, intensity).
    """
    smoothed = moving_average(intensity, SMOOTH_WINDOW)
    t = np.asarray(time_s, dtype=float)

    # keep only the positive times (start of the scan)
    mask = t > 0
    if not mask.any():
        raise ValueError("No point with time > 0 in the file.")

    # time -> wavelength conversion (scanspeed in nm/min)
    wl_cont = start_wl + t[mask] * scanspeed / 60.0
    y_cont = smoothed[mask]

    wl_last = wl_cont[-1] if end_wl is None else min(wl_cont[-1], end_wl)
    return resample_integer_wl(wl_cont, y_cont, x_last=wl_last)


def read_picoscope_csv(path):
    """Read a CSV and recognise the file type.

    Returns ("raw", time, [channels]) for the raw Picoscope files
    (2 header lines, then Time, Channel A[, Channel B]) or
    ("spectrum", wl, intensity) for the already converted CSV files (two
    numeric columns without header).
    """
    with open(path, newline="") as f:
        rows = [row for row in csv.reader(f) if row and row[0].strip()]
    if not rows:
        raise ValueError("Empty file.")

    def is_number(s):
        try:
            float(s)
            return True
        except ValueError:
            return False

    raw = not is_number(rows[0][0])  # text header -> raw file
    data_rows = [r for r in rows if is_number(r[0])]
    if not data_rows:
        raise ValueError("No numeric data found in the file.")

    ncol = min(len(r) for r in data_rows)
    if ncol < 2:
        raise ValueError("At least two columns (X, Y) are needed.")
    cols = [np.array([float(r[c]) for r in data_rows]) for c in range(ncol)]

    if raw:
        return "raw", cols[0], cols[1:]
    return "spectrum", cols[0], cols[1]


class PicoscopeManager:
    def __init__(self, root, files=()):
        self.root = root
        self.root.title("Picoscope Spectrum Manager")
        self.root.geometry("1400x800")

        self.spectra = {}
        self.v_line = None
        self.v_text = None
        self.current_dir = os.getcwd()
        self._cursor_data = []   # cache (name, x_array, y_array) for the cursor
        self._pe_module = None   # plot_editor module (PlotStyleKit), loaded on first use

        if HAS_DND:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind('<<Drop>>', self.handle_drop)

        # --- MENU ---
        self.menu_bar = tk.Menu(root)
        self.file_menu = tk.Menu(self.menu_bar, tearoff=0)
        self.file_menu.add_command(label="Open Files (.csv)", command=self.load_from_dialog)
        self.file_menu.add_command(label="Export CSV", command=self.export_csv)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Save Figure Image (PNG, PDF, SVG)...", command=self.save_figure_image)
        self.file_menu.add_command(label="Save Figure (pickle)...", command=self.save_figure_pickle)
        self.file_menu.add_command(label="Edit Figure...", command=self.open_figure_editor)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Exit", command=root.quit)
        self.menu_bar.add_cascade(label="File", menu=self.file_menu)
        self.root.config(menu=self.menu_bar)

        self.paned = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=4)
        self.paned.pack(fill=tk.BOTH, expand=True)

        # 1. LEFT: DATA TABLE
        self.f_data = tk.Frame(self.paned, bg='#ffffff')
        tk.Label(self.f_data, text="DATA TABLE", font=('Arial', 10, 'bold'), bg='#ffffff').pack(pady=5)
        self.data_box = scrolledtext.ScrolledText(self.f_data, width=40, font=('Consolas', 10), bd=0)
        self.data_box.pack(padx=2, pady=2, fill=tk.BOTH, expand=True)
        self.paned.add(self.f_data, width=360)

        # 2. CENTRE: plot
        self.f_plot = tk.Frame(self.paned)
        self.fig, self.ax = plt.subplots(figsize=(6, 6))
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.f_plot)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.toolbar = NavigationToolbar2Tk(self.canvas, self.f_plot)
        self.canvas.mpl_connect('motion_notify_event', self.on_mouse_move)
        self.canvas.mpl_connect('axes_leave_event', self.on_mouse_leave)
        self.paned.add(self.f_plot, width=680)

        # 3. RIGHT: conversion parameters + file management + metadata
        self.f_right = tk.Frame(self.paned, bg='#f0f4f7')

        tk.Label(self.f_right, text="CONVERSION PARAMETERS",
                 font=('Arial', 10, 'bold'), bg='#f0f4f7').pack(pady=(10, 2))
        par = tk.Frame(self.f_right, bg='#f0f4f7')
        par.pack(padx=10, pady=2)

        self.var_start = tk.StringVar(value="500")
        self.var_speed = tk.StringVar(value="240")
        self.var_end = tk.StringVar(value="700")
        self.var_channel = tk.StringVar(value="A")

        for col, (label, var, width) in enumerate([
                ("Initial WL", self.var_start, 6),
                ("Scanspeed", self.var_speed, 6),
                ("Final WL", self.var_end, 6),
                ("Channel", self.var_channel, 3)]):
            tk.Label(par, text=label, bg='#f0f4f7', font=('Consolas', 9)).grid(row=0, column=col, padx=4)
            if label == "Channel":
                ttk.Combobox(par, textvariable=var, values=("A", "B"), width=width,
                             state="readonly").grid(row=1, column=col, padx=4)
            else:
                tk.Entry(par, textvariable=var, width=width,
                         font=('Consolas', 10)).grid(row=1, column=col, padx=4)

        tk.Label(self.f_right,
                 text="Parameters from the file name (_WL_speed) if present;\n"
                      "Final WL shows the actual end of the last spectrum",
                 font=('Consolas', 7), bg='#f0f4f7', fg='#777777', justify='left').pack()

        tk.Label(self.f_right, text="LOADED SPECTRA", font=('Arial', 10, 'bold'), bg='#f0f4f7').pack(pady=(12, 5))
        self.file_listbox = Listbox(self.f_right, selectmode=tk.MULTIPLE, height=8, font=('Arial', 9))
        self.file_listbox.pack(padx=10, pady=5, fill=tk.X)
        self.file_listbox.bind('<Button-3>', self._listbox_right_click)

        btn_frame = tk.Frame(self.f_right, bg='#f0f4f7')
        btn_frame.pack(fill=tk.X, padx=10)
        tk.Button(btn_frame, text="Remove Selected", command=self.remove_selected).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(btn_frame, text="Clear All", command=self.clear_all).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        # --- METADATA PANEL ---
        tk.Label(self.f_right, text="METADATA", font=('Arial', 10, 'bold'), bg='#f0f4f7').pack(pady=(15, 5))
        self.param_box = scrolledtext.ScrolledText(self.f_right, width=45, font=('Consolas', 9), bg='#f0f4f7', bd=0)
        self.param_box.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)

        self.paned.add(self.f_right, width=360)

        if files:
            for f in files:
                self.process_file(f)
            self.refresh_view()

    # --- CURSOR ---
    def on_mouse_move(self, event):
        if not event.inaxes or not self.spectra:
            return
        x = event.xdata
        if self.v_line: self.v_line.remove()
        if self.v_text: self.v_text.remove()
        self.v_line = self.ax.axvline(x=x, color='red', linestyle='--', lw=0.7)
        cursor_txt = f"X: {x:.2f}\n"
        for name, xs, ys in self._cursor_data:
            idx = np.argmin(np.abs(xs - x))
            cursor_txt += f"{name[:15]}: {ys[idx]:.4f}\n"
        self.v_text = self.ax.text(
            0.02, 0.98, cursor_txt, transform=self.ax.transAxes,
            verticalalignment='top', fontsize=8,
            bbox=dict(facecolor='white', alpha=0.7)
        )
        self.canvas.draw_idle()

    def on_mouse_leave(self, event):
        if self.v_line: self.v_line.remove(); self.v_line = None
        if self.v_text: self.v_text.remove(); self.v_text = None
        self.canvas.draw_idle()

    # --- FILE I/O ---
    def handle_drop(self, event):
        files = self.root.tk.splitlist(event.data)
        for f in files:
            self.process_file(f.strip('{}'))
        self.current_dir = os.path.dirname(files[-1].strip('{}'))
        self.refresh_view()

    def load_from_dialog(self):
        paths = filedialog.askopenfilenames(initialdir=self.current_dir,
                                            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")])
        if paths:
            for p in paths: self.process_file(p)
            self.current_dir = os.path.dirname(paths[-1])
            self.refresh_view()

    def _conversion_params(self, filename_base):
        """Conversion parameters: from the file name if encoded there, otherwise from the fields.

        File name convention: _<initial WL>_<scanspeed> at the end.
        The final cut is always ASSUMED_FINAL_WL; the Final WL field is
        informative and is updated after loading.
        """
        m = RE_PARAMS_IN_NAME.search(filename_base)
        if m:
            return float(m.group(1)), float(m.group(2)), 'filename'
        try:
            return float(self.var_start.get()), float(self.var_speed.get()), 'fields'
        except ValueError:
            raise ValueError("Initial WL and Scanspeed must be numbers.")

    def process_file(self, path):
        try:
            kind, x, y = read_picoscope_csv(path)
            filename_base = os.path.splitext(os.path.basename(path))[0]

            if kind == "raw":
                start_wl, scanspeed, source = self._conversion_params(filename_base)
                end_wl = ASSUMED_FINAL_WL
                if source == 'filename':
                    # align the fields with the parameters actually used
                    self.var_start.set(f"{start_wl:g}")
                    self.var_speed.set(f"{scanspeed:g}")
                chan = 0 if self.var_channel.get() == "A" else 1
                if chan >= len(y):
                    raise ValueError(f"The file has no channel {self.var_channel.get()}.")
                wl, intensity = convert_time_to_wl(x, y[chan], start_wl, scanspeed, end_wl)
                # range actually covered by the recording (t_max)
                wl_max_rec = start_wl + x.max() * scanspeed / 60.0
                # update Final WL to the actual end of the loaded spectrum
                self.var_end.set(f"{wl[-1]:.1f}")
                label = filename_base if chan == 0 else f"{filename_base}_chB"
                info = {
                    'Sample':        label,
                    'File':          os.path.basename(path),
                    'Type':          'Picoscope raw',
                    'Channel':       self.var_channel.get(),
                    'Initial WL':    f"{start_wl:g} nm ({source})",
                    'Scanspeed':     f"{scanspeed:g} nm/min ({source})",
                    'Scanned range': f"{start_wl:g} - {wl_max_rec:.1f} nm (from recording)",
                    'Final WL':      f"{wl[-1]:.1f} nm (cut assumed at {end_wl:g})",
                    'Smoothing':     f"moving average {SMOOTH_WINDOW} pts",
                    'Grid':          f"{WL_STEP:g} nm (interpolated)",
                }
            else:
                # already converted spectrum: only resampling on integer WL
                wl, intensity = resample_integer_wl(x, y)
                label = filename_base
                info = {
                    'Sample': label,
                    'File':   os.path.basename(path),
                    'Type':   'Converted spectrum',
                    'Grid':   f"{WL_STEP:g} nm (interpolated)",
                }

            info['Area'] = f"{float(np.trapezoid(intensity, wl)):.4f}"

            # Avoid collisions with spectra already loaded
            final_label = label
            counter = 2
            while final_label in self.spectra:
                final_label = f"{label}_{counter}"
                counter += 1
            info['Sample'] = final_label

            self.spectra[final_label] = {
                'df': pd.DataFrame({final_label: intensity}, index=wl),
                'info': info,
            }
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Error", f"Could not read {os.path.basename(path)}: {e}")

    # --- VIEW ---
    def refresh_view(self):
        self.file_listbox.delete(0, tk.END)
        self.data_box.delete(1.0, tk.END)
        self.param_box.delete(1.0, tk.END)
        self.ax.clear()
        self.v_line = self.v_text = None

        if not self.spectra:
            self._cursor_data = []
            self.canvas.draw()
            return

        for sname in self.spectra.keys():
            self.file_listbox.insert(tk.END, sname)

        # numpy array cache for the cursor (avoids pandas overhead on every mouse move)
        self._cursor_data = [
            (name, d['df'].index.to_numpy(dtype=float), d['df'][name].to_numpy(dtype=float))
            for name, d in self.spectra.items()
        ]

        master_df = pd.concat([s['df'] for s in self.spectra.values()], axis=1).sort_index()

        # Tab-separated table: the data are already on the 1 nm grid
        table_df = master_df.copy()
        table_df.index.name = 'Wavelength (nm)'
        self.data_box.insert(tk.END, table_df.round(4).to_csv(sep='\t', lineterminator='\n'))

        for col in master_df.columns:
            valid = master_df[col].dropna()
            self.ax.plot(valid.index, valid.values, label=col, lw=1.5)

        self.ax.set_xlim(master_df.index.min(), master_df.index.max())
        self.ax.set_xlabel("Wavelength (nm)")
        self.ax.set_ylabel("Intensity (V)")
        # fixed position: with 'best' matplotlib recomputes it at every redraw, taking the
        # cursor line into account too, and the legend would jump when the cursor passes over it
        self.ax.legend(loc='upper right')
        if origin_style is not None:
            origin_style.applica_stile_origin(self.ax, self.fig, set_size=False)
            # the paper font sizes (8-9 pt) look tiny when the plot fills the pane
            self.ax.xaxis.label.set_fontsize(12)
            self.ax.yaxis.label.set_fontsize(12)
            self.ax.tick_params(axis='both', labelsize=10)
            for t in self.ax.get_legend().get_texts():
                t.set_fontsize(10)
            self.fig.tight_layout()
        else:
            self.ax.grid(True, linestyle=':', alpha=0.6)
        self.canvas.draw()

        # Metadata panel — generic display of all the info keys
        for name, data in self.spectra.items():
            self.param_box.insert(tk.END, f"{'='*38}\n")
            for k, v in data['info'].items():
                self.param_box.insert(tk.END, f"{k:<12}: {v}\n")
            self.param_box.insert(tk.END, "\n")

    # --- LISTBOX CONTEXT MENU ---
    def _listbox_right_click(self, event):
        idx = self.file_listbox.nearest(event.y)
        if idx < 0 or idx >= self.file_listbox.size():
            return
        name = self.file_listbox.get(idx)
        # If the clicked item is part of a multiple selection, act on all of it
        sel = self.file_listbox.curselection()
        names = [self.file_listbox.get(i) for i in sel] if idx in sel else [name]

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Copy X,Y values to clipboard",
                         command=lambda: self._copy_xy_clipboard(name))
        label_text = "Copy for Origin (Long Name/Units/Comment)" if len(names) == 1 \
            else f"Copy {len(names)} spectra for Origin"
        menu.add_command(label=label_text,
                         command=lambda: self._copy_for_origin(names))
        menu.tk_popup(event.x_root, event.y_root)

    def _copy_xy_clipboard(self, name):
        df = self.spectra[name]['df']
        text = "\n".join(f"{x}\t{y}" for x, y in zip(df.index, df[name]))
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _copy_for_origin(self, names):
        if isinstance(names, str):
            names = [names]

        # Align by index: if X is identical the result is a single shared column,
        # otherwise the union of the indexes with empty cells where a value is missing
        merged = pd.concat([self.spectra[n]['df'] for n in names], axis=1).sort_index()

        long_row = ['Wavelength']
        unit_row = ['nm']
        comm_row = ['']
        for n in names:
            info = self.spectra[n]['info']
            long_row.append('Intensity')
            unit_row.append('V')
            comm_row.append(info.get('File') or n)

        rows = ['\t'.join(long_row), '\t'.join(unit_row), '\t'.join(comm_row)]
        for idx, row in zip(merged.index, merged[names].to_numpy()):
            cells = [f"{v}" if pd.notna(v) else '' for v in row]
            rows.append(f"{idx}\t" + '\t'.join(cells))

        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(rows))

    # --- LIST MANAGEMENT ---
    def remove_selected(self):
        selected = self.file_listbox.curselection()
        for i in reversed(selected):
            name = self.file_listbox.get(i)
            if name in self.spectra: del self.spectra[name]
        self.refresh_view()

    def clear_all(self):
        self.spectra = {}
        self.refresh_view()

    def export_csv(self):
        if not self.spectra: return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialdir=self.current_dir)
        if path:
            dfs = [s['df'] for s in self.spectra.values()]
            pd.concat(dfs, axis=1).round(4).to_csv(path)

    # --- FIGURE (PlotStyleKit) ---
    def _copy_figure(self):
        """Copy of the current figure for saving/editor: without the cursor, restyled
        Origin 'single' (4:3) instead of the panel size."""
        self.on_mouse_leave(None)
        fig = pickle.loads(pickle.dumps(self.fig))
        if origin_style is not None and fig.axes:
            origin_style.applica_stile_origin(fig.axes[0], fig, set_size=True, preset='single')
        return fig

    def save_figure_image(self):
        if not self.spectra:
            messagebox.showwarning("Save Figure Image", "No spectrum loaded.")
            return
        path = filedialog.asksaveasfilename(
            title="Save Figure Image", defaultextension=".png", initialdir=self.current_dir,
            filetypes=[("PNG", "*.png"), ("PDF", "*.pdf"), ("SVG", "*.svg"), ("All Files", "*.*")])
        if not path:
            return
        try:
            self._copy_figure().savefig(path, dpi=300)
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Save Figure Image", f"Save failed:\n{e}")
            return
        self.current_dir = os.path.dirname(path)
        messagebox.showinfo("Save Figure Image", f"Figure saved:\n{path}")

    def save_figure_pickle(self):
        """Save the current figure as a matplotlib pickle: reopenable with plot_editor.pyw as a
        live Figure/Axes object (not a raster)."""
        if not self.spectra:
            messagebox.showwarning("Save Figure", "No spectrum loaded.")
            return
        path = filedialog.asksaveasfilename(
            title="Save Figure", defaultextension=".fig.pickle", initialdir=self.current_dir,
            filetypes=[("Matplotlib Figure (pickle)", "*.pickle *.pkl"), ("All Files", "*.*")])
        if not path:
            return
        try:
            with open(path, 'wb') as f:
                pickle.dump(self._copy_figure(), f)
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Save Figure", f"Save failed:\n{e}")
            return
        self.current_dir = os.path.dirname(path)
        messagebox.showinfo("Save Figure", f"Figure saved:\n{path}")

    def _load_plot_editor(self):
        """Import plot_editor (only once): first a local copy, then the sibling repo PlotStyleKit."""
        if self._pe_module is None:
            import importlib.util
            here = os.path.dirname(os.path.abspath(__file__))
            candidates = [os.path.join(here, 'plot_editor.pyw'),
                          os.path.join(here, '..', 'PlotStyleKit', 'plot_editor.pyw')]
            path = next((c for c in candidates if os.path.isfile(c)), None)
            if path is None:
                raise FileNotFoundError(
                    "plot_editor.pyw not found (PlotStyleKit repo missing next to this project)")
            spec = importlib.util.spec_from_file_location('plot_editor', path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._pe_module = mod
        return self._pe_module

    def open_figure_editor(self):
        """Open the PlotStyleKit Plot Editor on the current figure (an independent copy)."""
        if not self.spectra:
            messagebox.showwarning("Edit Figure", "No spectrum loaded.")
            return
        try:
            pe = self._load_plot_editor()
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Edit Figure", f"plot_editor.pyw not available:\n{e}")
            return
        try:
            fig = self._copy_figure()
        except Exception as e:
            traceback.print_exc()
            messagebox.showerror("Edit Figure", f"Cannot duplicate the figure:\n{e}")
            return
        top = tk.Toplevel(self.root)
        top.geometry("1300x820")
        editor = pe.PlotEditor(top)
        editor.carica_figura(fig, title="current figure")


if __name__ == "__main__":
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    app = PicoscopeManager(root, files=sys.argv[1:])
    root.mainloop()
