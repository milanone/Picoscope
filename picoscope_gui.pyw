"""Picoscope Spectrum Manager - GUI in stile LabSpectrumManager.

Carica i CSV esportati dal Picoscope (colonne: Time, Channel A, Channel B),
converte il tempo in lunghezza d'onda, applica smoothing e downsampling e
gestisce piu' spettri contemporaneamente: tabella dati, grafico con cursore,
lista degli spettri caricati con metadati, export CSV unito.

Riconosce anche i CSV gia' convertiti (due colonne: lunghezza d'onda,
intensita', senza intestazione) e li carica direttamente.

Se il nome del file grezzo termina con _<WL>_<scanspeed> (es.
"pyranine_ex_300_240.csv") i parametri di conversione vengono letti dal
nome; altrimenti si usano i valori dei campi nel pannello di destra.

Dipendenze: numpy, pandas, matplotlib, tkinterdnd2 (opzionale, per il drop)
"""

import csv
import os
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

SMOOTH_WINDOW = 100   # come smooth(x,100) in MATLAB (media mobile centrata)
WL_STEP = 1.0         # griglia di uscita: 1 nm esatto, lunghezze d'onda intere
FINAL_WL_ASSUNTO = 1000.0  # taglio assunto a ogni caricamento; il campo
                           # Final WL viene poi attualizzato alla fine effettiva

# nome file "campione_<WL iniziale>_<scanspeed>" (es. "campione_ex_300_240"
# -> scansione che parte da 300 nm a 240 nm/min)
RE_PARAMS_NEL_NOME = re.compile(r'_(\d{3,4})_(\d{2,4})$')


def moving_average(x, window):
    """Media mobile centrata, equivalente a smooth() di MATLAB.

    Ai bordi la finestra si restringe (media dei punti disponibili),
    come fa smooth() di MATLAB.
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


def ricampiona_wl_intere(x, y, x_last=None):
    """Ricampiona uno spettro sulla griglia a passo WL_STEP con WL intere.

    Il primo punto viene arrotondato al nm piu' vicino (l'eventuale
    estrapolazione e' di frazioni di nm, np.interp la limita al primo valore).
    """
    wl_first = np.floor(x[0] + 0.5)
    if x_last is None:
        x_last = x[-1]
    wl = np.arange(wl_first, np.floor(x_last) + WL_STEP / 2, WL_STEP)
    if len(wl) < 2:
        raise ValueError("Intervallo troppo corto per la griglia a 1 nm.")
    yi = np.round(np.interp(wl, x, y), 6)
    if float(WL_STEP).is_integer():
        wl = wl.astype(int)   # 300, 301, ... senza decimali in tabella/export
    return wl, yi


def converti_tempo_wl(time_s, intensity, start_wl, scanspeed, end_wl):
    """Converte tempo -> lunghezza d'onda e ricampiona su griglia intera.

    Smoothing a media mobile come la GUI MATLAB originale, poi interpolazione
    del segnale sulle lunghezze d'onda intere a passo WL_STEP (300, 301, ...).
    Ritorna (wl, intensita').
    """
    smoothed = moving_average(intensity, SMOOTH_WINDOW)
    t = np.asarray(time_s, dtype=float)

    # tiene solo i tempi positivi (inizio scansione)
    mask = t > 0
    if not mask.any():
        raise ValueError("Nessun punto con tempo > 0 nel file.")

    # conversione tempo -> lunghezza d'onda (scanspeed in nm/min)
    wl_cont = start_wl + t[mask] * scanspeed / 60.0
    y_cont = smoothed[mask]

    wl_last = wl_cont[-1] if end_wl is None else min(wl_cont[-1], end_wl)
    return ricampiona_wl_intere(wl_cont, y_cont, x_last=wl_last)


def leggi_csv_picoscope(path):
    """Legge un CSV e riconosce il tipo di file.

    Ritorna ("raw", time, [canali]) per i file grezzi del Picoscope
    (2 righe di intestazione, poi Time, Channel A[, Channel B]) oppure
    ("spectrum", wl, intensita') per i CSV gia' convertiti (due colonne
    numeriche senza intestazione).
    """
    with open(path, newline="") as f:
        rows = [row for row in csv.reader(f) if row and row[0].strip()]
    if not rows:
        raise ValueError("File vuoto.")

    def is_number(s):
        try:
            float(s)
            return True
        except ValueError:
            return False

    raw = not is_number(rows[0][0])  # intestazione testuale -> file grezzo
    data_rows = [r for r in rows if is_number(r[0])]
    if not data_rows:
        raise ValueError("Nessun dato numerico trovato nel file.")

    ncol = min(len(r) for r in data_rows)
    if ncol < 2:
        raise ValueError("Servono almeno due colonne (X, Y).")
    cols = [np.array([float(r[c]) for r in data_rows]) for c in range(ncol)]

    if raw:
        return "raw", cols[0], cols[1:]
    return "spectrum", cols[0], cols[1]


class PicoscopeManager:
    def __init__(self, root):
        self.root = root
        self.root.title("Picoscope Spectrum Manager")
        self.root.geometry("1400x800")

        self.spectra = {}
        self.v_line = None
        self.v_text = None
        self.current_dir = os.getcwd()
        self._cursor_data = []   # cache (name, x_array, y_array) per il cursore

        if HAS_DND:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind('<<Drop>>', self.handle_drop)

        # --- MENU ---
        self.menu_bar = tk.Menu(root)
        self.file_menu = tk.Menu(self.menu_bar, tearoff=0)
        self.file_menu.add_command(label="Open Files (.csv)", command=self.carica_da_dialog)
        self.file_menu.add_command(label="Export CSV", command=self.esporta_csv)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Exit", command=root.quit)
        self.menu_bar.add_cascade(label="File", menu=self.file_menu)
        self.root.config(menu=self.menu_bar)

        self.paned = tk.PanedWindow(root, orient=tk.HORIZONTAL, sashrelief=tk.RAISED, sashwidth=4)
        self.paned.pack(fill=tk.BOTH, expand=True)

        # 1. SINISTRA: DATA TABLE
        self.f_data = tk.Frame(self.paned, bg='#ffffff')
        tk.Label(self.f_data, text="DATA TABLE", font=('Arial', 10, 'bold'), bg='#ffffff').pack(pady=5)
        self.data_box = scrolledtext.ScrolledText(self.f_data, width=40, font=('Consolas', 10), bd=0)
        self.data_box.pack(padx=2, pady=2, fill=tk.BOTH, expand=True)
        self.paned.add(self.f_data, width=360)

        # 2. CENTRO: Grafico
        self.f_plot = tk.Frame(self.paned)
        self.fig, self.ax = plt.subplots(figsize=(6, 6))
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.f_plot)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)
        self.toolbar = NavigationToolbar2Tk(self.canvas, self.f_plot)
        self.canvas.mpl_connect('motion_notify_event', self.on_mouse_move)
        self.canvas.mpl_connect('axes_leave_event', self.on_mouse_leave)
        self.paned.add(self.f_plot, width=680)

        # 3. DESTRA: parametri di conversione + gestione file + metadata
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
                 text="Parametri dal nome file (_WL_speed) se presenti;\n"
                      "Final WL mostra la fine effettiva dell'ultimo spettro",
                 font=('Consolas', 7), bg='#f0f4f7', fg='#777777', justify='left').pack()

        tk.Label(self.f_right, text="LOADED SPECTRA", font=('Arial', 10, 'bold'), bg='#f0f4f7').pack(pady=(12, 5))
        self.file_listbox = Listbox(self.f_right, selectmode=tk.MULTIPLE, height=8, font=('Arial', 9))
        self.file_listbox.pack(padx=10, pady=5, fill=tk.X)
        self.file_listbox.bind('<Button-3>', self._listbox_tasto_destro)

        btn_frame = tk.Frame(self.f_right, bg='#f0f4f7')
        btn_frame.pack(fill=tk.X, padx=10)
        tk.Button(btn_frame, text="Remove Selected", command=self.remove_selected).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(btn_frame, text="Clear All", command=self.clear_all).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)

        # --- PANNELLO METADATA ---
        tk.Label(self.f_right, text="METADATA", font=('Arial', 10, 'bold'), bg='#f0f4f7').pack(pady=(15, 5))
        self.param_box = scrolledtext.ScrolledText(self.f_right, width=45, font=('Consolas', 9), bg='#f0f4f7', bd=0)
        self.param_box.pack(padx=10, pady=5, fill=tk.BOTH, expand=True)

        self.paned.add(self.f_right, width=360)

        if len(sys.argv) > 1:
            for i in range(1, len(sys.argv)):
                self.processa_file(sys.argv[i])
            self.aggiorna_vista()

    # --- CURSORE ---
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
            self.processa_file(f.strip('{}'))
        self.current_dir = os.path.dirname(files[-1].strip('{}'))
        self.aggiorna_vista()

    def carica_da_dialog(self):
        paths = filedialog.askopenfilenames(initialdir=self.current_dir,
                                            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")])
        if paths:
            for p in paths: self.processa_file(p)
            self.current_dir = os.path.dirname(paths[-1])
            self.aggiorna_vista()

    def _parametri_conversione(self, filename_base):
        """Parametri di conversione: dal nome file se codificati, altrimenti dai campi.

        Convenzione nome file: _<WL iniziale>_<scanspeed> in coda.
        Il taglio finale e' sempre FINAL_WL_ASSUNTO; il campo Final WL e'
        informativo e viene attualizzato dopo il caricamento.
        """
        m = RE_PARAMS_NEL_NOME.search(filename_base)
        if m:
            return float(m.group(1)), float(m.group(2)), 'filename'
        try:
            return float(self.var_start.get()), float(self.var_speed.get()), 'fields'
        except ValueError:
            raise ValueError("Initial WL e Scanspeed devono essere numeri.")

    def processa_file(self, path):
        try:
            kind, x, y = leggi_csv_picoscope(path)
            filename_base = os.path.splitext(os.path.basename(path))[0]

            if kind == "raw":
                start_wl, scanspeed, origine = self._parametri_conversione(filename_base)
                end_wl = FINAL_WL_ASSUNTO
                if origine == 'filename':
                    # riallinea i campi ai parametri effettivamente usati
                    self.var_start.set(f"{start_wl:g}")
                    self.var_speed.set(f"{scanspeed:g}")
                chan = 0 if self.var_channel.get() == "A" else 1
                if chan >= len(y):
                    raise ValueError(f"Il file non ha il canale {self.var_channel.get()}.")
                wl, intensity = converti_tempo_wl(x, y[chan], start_wl, scanspeed, end_wl)
                # intervallo realmente coperto dalla registrazione (t_max)
                wl_max_rec = start_wl + x.max() * scanspeed / 60.0
                # attualizza Final WL alla fine effettiva dello spettro caricato
                self.var_end.set(f"{wl[-1]:.1f}")
                label = filename_base if chan == 0 else f"{filename_base}_chB"
                info = {
                    'Sample':        label,
                    'File':          os.path.basename(path),
                    'Type':          'Picoscope raw',
                    'Channel':       self.var_channel.get(),
                    'Initial WL':    f"{start_wl:g} nm ({origine})",
                    'Scanspeed':     f"{scanspeed:g} nm/min ({origine})",
                    'Scanned range': f"{start_wl:g} - {wl_max_rec:.1f} nm (from recording)",
                    'Final WL':      f"{wl[-1]:.1f} nm (cut assumed at {end_wl:g})",
                    'Smoothing':     f"moving average {SMOOTH_WINDOW} pts",
                    'Grid':          f"{WL_STEP:g} nm (interpolated)",
                }
            else:
                # spettro gia' convertito: solo ricampionamento su WL intere
                wl, intensity = ricampiona_wl_intere(x, y)
                label = filename_base
                info = {
                    'Sample': label,
                    'File':   os.path.basename(path),
                    'Type':   'Converted spectrum',
                    'Grid':   f"{WL_STEP:g} nm (interpolated)",
                }

            info['Area'] = f"{float(np.trapezoid(intensity, wl)):.4f}"

            # Evita collisioni con spettri gia' caricati
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

    # --- VISTA ---
    def aggiorna_vista(self):
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

        # Cache array numpy per il cursore (evita overhead pandas a ogni mouse move)
        self._cursor_data = [
            (name, d['df'].index.to_numpy(dtype=float), d['df'][name].to_numpy(dtype=float))
            for name, d in self.spectra.items()
        ]

        master_df = pd.concat([s['df'] for s in self.spectra.values()], axis=1).sort_index()

        # Tabella tabulata — i dati sono gia' sulla griglia a 1 nm
        table_df = master_df.copy()
        table_df.index.name = 'Wavelength (nm)'
        self.data_box.insert(tk.END, table_df.round(4).to_csv(sep='\t', lineterminator='\n'))

        for col in master_df.columns:
            valid = master_df[col].dropna()
            self.ax.plot(valid.index, valid.values, label=col, lw=1.5)

        self.ax.set_xlim(master_df.index.min(), master_df.index.max())
        self.ax.set_xlabel("Wavelength (nm)")
        self.ax.set_ylabel("Intensity (V)")
        self.ax.legend(fontsize='8', loc='best')
        self.ax.grid(True, linestyle=':', alpha=0.6)
        self.canvas.draw()

        # Metadata panel — display generico di tutte le chiavi info
        for name, data in self.spectra.items():
            self.param_box.insert(tk.END, f"{'='*38}\n")
            for k, v in data['info'].items():
                self.param_box.insert(tk.END, f"{k:<12}: {v}\n")
            self.param_box.insert(tk.END, "\n")

    # --- CONTEXT MENU LISTBOX ---
    def _listbox_tasto_destro(self, event):
        idx = self.file_listbox.nearest(event.y)
        if idx < 0 or idx >= self.file_listbox.size():
            return
        name = self.file_listbox.get(idx)
        # Se l'elemento cliccato fa parte di una selezione multipla, opera su tutta
        sel = self.file_listbox.curselection()
        names = [self.file_listbox.get(i) for i in sel] if idx in sel else [name]

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Copy X,Y values to clipboard",
                         command=lambda: self._copia_xy_clipboard(name))
        etichetta = "Copy for Origin (Long Name/Units/Comment)" if len(names) == 1 \
            else f"Copy {len(names)} spectra for Origin"
        menu.add_command(label=etichetta,
                         command=lambda: self._copia_origin(names))
        menu.tk_popup(event.x_root, event.y_root)

    def _copia_xy_clipboard(self, name):
        df = self.spectra[name]['df']
        testo = "\n".join(f"{x}\t{y}" for x, y in zip(df.index, df[name]))
        self.root.clipboard_clear()
        self.root.clipboard_append(testo)

    def _copia_origin(self, names):
        if isinstance(names, str):
            names = [names]

        # Allinea per indice: se la X è identica risulta una sola colonna condivisa,
        # altrimenti l'unione degli indici con celle vuote dove un dato manca
        merged = pd.concat([self.spectra[n]['df'] for n in names], axis=1).sort_index()

        long_row = ['Wavelength']
        unit_row = ['nm']
        comm_row = ['']
        for n in names:
            info = self.spectra[n]['info']
            long_row.append('Intensity')
            unit_row.append('V')
            comm_row.append(info.get('File') or n)

        righe = ['\t'.join(long_row), '\t'.join(unit_row), '\t'.join(comm_row)]
        for idx, row in zip(merged.index, merged[names].to_numpy()):
            celle = [f"{v}" if pd.notna(v) else '' for v in row]
            righe.append(f"{idx}\t" + '\t'.join(celle))

        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(righe))

    # --- GESTIONE LISTA ---
    def remove_selected(self):
        selected = self.file_listbox.curselection()
        for i in reversed(selected):
            name = self.file_listbox.get(i)
            if name in self.spectra: del self.spectra[name]
        self.aggiorna_vista()

    def clear_all(self):
        self.spectra = {}
        self.aggiorna_vista()

    def esporta_csv(self):
        if not self.spectra: return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialdir=self.current_dir)
        if path:
            dfs = [s['df'] for s in self.spectra.values()]
            pd.concat(dfs, axis=1).round(4).to_csv(path)


if __name__ == "__main__":
    root = TkinterDnD.Tk() if HAS_DND else tk.Tk()
    app = PicoscopeManager(root)
    root.mainloop()
