"""Tests for the Picoscope Spectrum Manager: the conversion functions and CSV reader on synthetic data,
and the application itself (hidden Tk window, dialogs replaced) on a synthetic raw file and, if present,
on the real recordings in 'example data/' (gitignored).

    py -m unittest discover -s tests -v
"""
import importlib.util
import os
import pickle
import tempfile
import tkinter as tk
import unittest
from unittest import mock

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("pico", os.path.join(HERE, "..", "picoscope_gui.pyw"))
pico = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pico)

REAL = os.path.join(HERE, "..", "example data", "pyranine_ex_300_240.csv")


def write_raw(path, start_time=-5.0, t_max=30.0, dt=0.003, two_channels=True):
    """Raw Picoscope export: 2 header lines, a blank line, then Time, Channel A[, Channel B]."""
    t = np.arange(start_time, t_max, dt)
    a = np.sin(t / 3.0)
    b = np.cos(t / 3.0)
    with open(path, "w", newline="") as f:
        f.write("Time,Channel A,Channel B\n" if two_channels else "Time,Channel A\n")
        f.write("(s),(V),(V)\n" if two_channels else "(s),(V)\n")
        f.write("\n")
        for i in range(len(t)):
            f.write(f"{t[i]:.8f},{a[i]:.8f},{b[i]:.8f}\n" if two_channels else f"{t[i]:.8f},{a[i]:.8f}\n")
    return t


class TestFunctions(unittest.TestCase):
    def test_moving_average_constant_and_edges(self):
        np.testing.assert_allclose(pico.moving_average(np.full(50, 2.0), 11), 2.0)
        out = pico.moving_average(np.arange(10.0), 5)
        self.assertAlmostEqual(out[0], np.mean([0, 1, 2]))          # the window shrinks at the edge
        self.assertAlmostEqual(out[5], np.mean([3, 4, 5, 6, 7]))

    def test_moving_average_trivial_window(self):
        x = np.array([1.0, 5.0, 2.0])
        np.testing.assert_array_equal(pico.moving_average(x, 1), x)

    def test_resample_integer_grid(self):
        x = np.linspace(300.2, 310.8, 200)
        wl, y = pico.resample_integer_wl(x, 2 * x)
        self.assertEqual(wl[0], 300)
        self.assertEqual(wl[-1], 310)
        self.assertTrue(np.all(np.diff(wl) == 1))
        np.testing.assert_allclose(y[1:], 2.0 * wl[1:], atol=1e-5)   # linear signal is reproduced

    def test_resample_too_short(self):
        with self.assertRaises(ValueError):
            pico.resample_integer_wl(np.array([300.0, 300.2]), np.array([1.0, 2.0]))

    def test_convert_time_to_wl(self):
        t = np.arange(-1.0, 60.01, 0.01)
        wl, y = pico.convert_time_to_wl(t, np.ones_like(t), 300.0, 240.0, 1000.0)
        self.assertEqual(wl[0], 300)                    # t > 0 starts at the initial wavelength
        self.assertEqual(wl[-1], 540)                   # 60 s x 240 nm/min = 240 nm
        np.testing.assert_allclose(y, 1.0)

    def test_convert_respects_the_final_wavelength(self):
        t = np.arange(0.5, 60.0, 0.01)
        wl, _ = pico.convert_time_to_wl(t, np.ones_like(t), 300.0, 240.0, 400.0)
        self.assertEqual(wl[-1], 400)

    def test_convert_without_positive_time(self):
        with self.assertRaises(ValueError):
            pico.convert_time_to_wl(np.array([-3.0, -2.0, -1.0]), np.ones(3), 300.0, 240.0, 1000.0)

    def test_name_parameters_regex(self):
        m = pico.RE_PARAMS_IN_NAME.search("pyranine_ex_300_240")
        self.assertEqual((m.group(1), m.group(2)), ("300", "240"))
        self.assertIsNone(pico.RE_PARAMS_IN_NAME.search("sample_without_params"))


class TestReader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_raw_file(self):
        t = write_raw(self.path("raw.csv"))
        kind, x, ys = pico.read_picoscope_csv(self.path("raw.csv"))
        self.assertEqual(kind, "raw")
        self.assertEqual(len(x), len(t))
        self.assertEqual(len(ys), 2)

    def test_raw_file_with_one_channel(self):
        write_raw(self.path("one.csv"), two_channels=False)
        kind, x, ys = pico.read_picoscope_csv(self.path("one.csv"))
        self.assertEqual((kind, len(ys)), ("raw", 1))

    def test_converted_spectrum(self):
        with open(self.path("spec.csv"), "w") as f:
            f.write("500.0,1.5\n500.5,1.6\n501.0,1.7\n")
        kind, x, y = pico.read_picoscope_csv(self.path("spec.csv"))
        self.assertEqual(kind, "spectrum")
        np.testing.assert_allclose(y, [1.5, 1.6, 1.7])

    def test_empty_file(self):
        open(self.path("empty.csv"), "w").close()
        with self.assertRaises(ValueError):
            pico.read_picoscope_csv(self.path("empty.csv"))

    def test_header_only(self):
        with open(self.path("h.csv"), "w") as f:
            f.write("Time,Channel A\n(s),(V)\n")
        with self.assertRaises(ValueError):
            pico.read_picoscope_csv(self.path("h.csv"))


class TestApplication(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = pico.TkinterDnD.Tk() if pico.HAS_DND else tk.Tk()
        except tk.TclError as e:
            raise unittest.SkipTest("Tk not available: %s" % e)
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.app = pico.PicoscopeManager(self.root)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def tearDown(self):
        for w in self.root.winfo_children():
            w.destroy()

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_parameters_come_from_the_file_name(self):
        write_raw(self.path("sample_ex_300_240.csv"), t_max=60.01)
        self.app.process_file(self.path("sample_ex_300_240.csv"))
        self.app.refresh_view()
        info = self.app.spectra["sample_ex_300_240"]["info"]
        self.assertIn("(filename)", info["Initial WL"])
        self.assertEqual(self.app.var_start.get(), "300")
        self.assertEqual(self.app.var_speed.get(), "240")
        self.assertEqual(self.app.var_end.get(), "540.0")        # Final WL follows the recording

    def test_parameters_come_from_the_fields(self):
        write_raw(self.path("noparams.csv"), t_max=30.0)
        self.app.var_start.set("500")
        self.app.var_speed.set("120")
        self.app.process_file(self.path("noparams.csv"))
        info = self.app.spectra["noparams"]["info"]
        self.assertIn("(fields)", info["Scanspeed"])
        self.assertEqual(int(self.app.spectra["noparams"]["df"].index[0]), 500)

    def test_channel_b_and_missing_channel(self):
        write_raw(self.path("a_300_240.csv"), t_max=30.0, two_channels=False)
        self.app.var_channel.set("B")
        with mock.patch.object(pico.messagebox, "showerror") as err:
            self.app.process_file(self.path("a_300_240.csv"))
        self.assertTrue(err.called)                              # no channel B in the file
        self.assertEqual(self.app.spectra, {})
        write_raw(self.path("b_300_240.csv"), t_max=30.0)
        self.app.process_file(self.path("b_300_240.csv"))
        self.assertIn("b_300_240_chB", self.app.spectra)

    def test_same_file_twice_gets_a_new_name(self):
        write_raw(self.path("x_300_240.csv"), t_max=30.0)
        self.app.process_file(self.path("x_300_240.csv"))
        self.app.process_file(self.path("x_300_240.csv"))
        self.assertEqual(list(self.app.spectra), ["x_300_240", "x_300_240_2"])

    def test_converted_spectrum_and_view(self):
        with open(self.path("conv.csv"), "w") as f:
            f.write("500.0,1.0\n501.0,2.0\n502.0,3.0\n")
        self.app.process_file(self.path("conv.csv"))
        self.app.refresh_view()
        self.assertEqual(self.app.file_listbox.get(0), "conv")
        self.assertIn("Wavelength (nm)", self.app.data_box.get("1.0", tk.END))
        self.assertEqual(self.app.spectra["conv"]["info"]["Area"], "4.0000")

    def test_export_and_remove(self):
        for n in ("p_300_240", "q_300_240"):
            write_raw(self.path(n + ".csv"), t_max=30.0)
            self.app.process_file(self.path(n + ".csv"))
        self.app.refresh_view()
        out = self.path("merged.csv")
        with mock.patch.object(pico.filedialog, "asksaveasfilename", return_value=out):
            self.app.export_csv()
        with open(out) as f:
            header = f.readline().strip()
        self.assertEqual(header, ",p_300_240,q_300_240")
        self.app.file_listbox.selection_set(0)
        self.app.remove_selected()
        self.assertEqual(list(self.app.spectra), ["q_300_240"])

    def test_copy_for_origin(self):
        write_raw(self.path("o_300_240.csv"), t_max=30.0)
        self.app.process_file(self.path("o_300_240.csv"))
        self.app._copy_for_origin(["o_300_240"])
        lines = self.root.clipboard_get().split("\n")
        self.assertEqual(lines[0], "Wavelength\tIntensity")
        self.assertEqual(lines[1], "nm\tV")
        self.assertEqual(lines[2], "\to_300_240.csv")

    def test_figure_copy_is_styled_and_has_no_cursor(self):
        write_raw(self.path("f_300_240.csv"), t_max=30.0)
        self.app.process_file(self.path("f_300_240.csv"))
        self.app.refresh_view()
        self.app.v_line = self.app.ax.axvline(400)               # a cursor line on the live plot
        fig = self.app._copy_figure()
        self.assertEqual(len(fig.axes[0].lines), 1)              # only the spectrum, no cursor
        if pico.origin_style is not None:
            w, h = fig.get_size_inches()
            self.assertAlmostEqual(w / h, 4 / 3, places=2)       # Origin 'single' preset
        out = self.path("fig.fig.pickle")
        with mock.patch.object(pico.filedialog, "asksaveasfilename", return_value=out), \
                mock.patch.object(pico.messagebox, "showinfo"), \
                mock.patch.object(pico.messagebox, "showerror") as err:
            self.app.save_figure_pickle()
        self.assertFalse(err.called)
        with open(out, "rb") as f:
            self.assertEqual(len(pickle.load(f).axes), 1)

    def test_figure_actions_without_spectra_warn(self):
        with mock.patch.object(pico.messagebox, "showwarning") as warn:
            self.app.save_figure_image()
            self.app.save_figure_pickle()
            self.app.open_figure_editor()
        self.assertEqual(warn.call_count, 3)

    @unittest.skipUnless(os.path.isfile(REAL), "real recording not present")
    def test_real_recording(self):
        self.app.process_file(REAL)
        self.app.refresh_view()
        df = self.app.spectra["pyranine_ex_300_240"]["df"]
        self.assertEqual(int(df.index[0]), 300)
        self.assertTrue(np.all(np.diff(df.index) == 1))
        self.assertTrue(np.isfinite(df.iloc[:, 0]).all())


if __name__ == "__main__":
    unittest.main()
