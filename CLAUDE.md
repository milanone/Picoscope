# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
General rules for all of Francesco's projects (stack, code conventions, repository rules, git/GitHub, the
Drive mirror, how he works) are in `..\CLAUDE.md`; this file has the project-specific details.

## Purpose

Tkinter viewer that turns raw Picoscope recordings of a wavelength-scanning measurement (an oscilloscope
trace of a detector while a monochromator scans) into spectra: time -> wavelength, smoothing, resampling on a 1 nm
grid, several spectra at once, merged CSV export. Same look and workflow as LabSpectrumManager.

## Running / tests

```bash
pythonw picoscope_gui.pyw [file.csv ...]
py -m unittest discover -s tests -v
```

Sample data: `example data/*.csv` (gitignored: real recordings, never commit). Tk tests need a screen; the
real-recording test is skipped if the sample is missing. `screenshot.png` is the real program with two recordings
(`ImageGrab` on the window, window topmost) and was checked before publishing.

## Architecture

Single file `picoscope_gui.pyw`, class `PicoscopeManager` (Tkinter + matplotlib, 3-pane `tk.PanedWindow`: data table /
plot with cursor / conversion parameters + list + metadata). `self.spectra = {name: {'df': DataFrame indexed by
integer wavelength, 'info': {...}}}`. Module-level functions hold the logic and are tested without Tk:

- `read_picoscope_csv(path)` -> `("raw", time, [channels])` (2 text header lines, then numbers) or `("spectrum", wl,
  intensity)` (two numeric columns, no header, already converted).
- `convert_time_to_wl()`: keeps `t > 0`, `wavelength = initial WL + t * scanspeed / 60` (scanspeed in nm/min), after
  a centred moving average of `SMOOTH_WINDOW` = 100 points (`moving_average()`, same as MATLAB `smooth(x,100)`, the
  window shrinks at the edges); the end is cut at `ASSUMED_FINAL_WL` = 1000 nm.
- `resample_integer_wl()` interpolates on integer wavelengths at `WL_STEP` = 1 nm.
- Parameters come from the end of the file name (`RE_PARAMS_IN_NAME`: `_<initial WL>_<scanspeed>`), otherwise from
  the right-panel fields; the Final WL field is then updated to the actual end of the loaded spectrum.

The app takes the file list as `PicoscopeManager(root, files=...)` (the entry point passes `sys.argv[1:]`).

## PlotStyleKit

`_load_origin_style()` loads `origin_style.py` by path (local copy, then `../PlotStyleKit`) and calls
`applica_rcparams()` at import; without it the plot uses plain matplotlib with a grid. `refresh_view()` calls
`applica_stile_origin(ax, fig, set_size=False)` and enlarges the fonts for the on-screen pane. The legend is fixed at
`upper right` (not `best`) so it does not jump under the cursor line. File menu: `Save Figure Image`, `Save Figure
(pickle)`, `Edit Figure...` work on `_copy_figure()` (a pickle copy without the cursor, restyled `single` 4:3) and
open `plot_editor.pyw` from PlotStyleKit. Style changes go in PlotStyleKit, never here.

## Verified / not verified

- Tested on synthetic recordings (conversion maths, name parameters, channel B, duplicates, export, Origin clipboard
  copy, figure copy) and loaded on four real recordings.
- Not verified against the MATLAB original beyond the smoothing definition; drag and drop is untested (needs
  `tkinterdnd2`, which is installed but not exercised by the tests).
- The y axis label is fixed to "Intensity (V)"; the unit of channel B in some files is mV (it is not converted).

## Editing conventions (project)
- Follow `..\CLAUDE.md`: everything in English, surgical edits. The project was translated from Italian in one pass
  (identifiers, comments, docstrings, messages), keeping the behaviour; the entry point now takes `files=`.
- Never commit anything from `example data/`.
