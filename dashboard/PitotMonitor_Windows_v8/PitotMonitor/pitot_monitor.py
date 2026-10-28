import csv
import math
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    import serial
    from serial import SerialException
    from serial.tools import list_ports
except ImportError:
    serial = None
    SerialException = Exception
    list_ports = None

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator


# -----------------------------
# Arduino data parsing
# -----------------------------

DATA_RE_LEGACY = re.compile(
    r"T1=([-+]?\d+(?:\.\d+)?)\s+"
    r"T2=([-+]?\d+(?:\.\d+)?)\s+"
    r"Ps=([-+]?\d+(?:\.\d+)?)\s+"
    r"Pt=([-+]?\d+(?:\.\d+)?)\s+"
    r"dP=([-+]?\d+(?:\.\d+)?)\s+"
    r"V=([-+]?\d+(?:\.\d+)?)\s+"
    r"rho=([-+]?\d+(?:\.\d+)?)\s+"
    r"ERR=(\d+)"
)
ERROR_RE = re.compile(r"ERROR=(\d+)\s*(.*)", re.IGNORECASE)
ZERO_OFFSET_RE = re.compile(r"ZERO_OFFSET_Pa,\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE)
CSV_NUMBER_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?$")

ERROR_NAMES = {
    0: "OK",
    1: "BMP180 №2 — статичний датчик",
    2: "BMP180 №1 — датчик Піто",
    3: "Калібрування нуля",
    4: "Помилка вимірювання",
    5: "Помилка тиску",
    6: "Помилка температури",
    7: "Помилка густини повітря",
    900: "Помилка COM-з'єднання",
}


@dataclass
class Sample:
    t: float
    t1: float
    t2: float
    ps: float
    pt: float
    dp_raw: float
    dp: float
    v: float
    v_kmh: float
    rho: float
    err: int


def parse_float(text):
    return float(text.strip().replace(",", "."))


def density_from_pressure_temperature(pressure_pa, temperature_c):
    if pressure_pa <= 0:
        return float("nan")
    temp_k = temperature_c + 273.15
    if temp_k <= 0:
        return float("nan")
    return pressure_pa / (287.05 * temp_k)


def parse_line(line: str, sample_time=None):
    line = line.strip()
    if not line:
        return ("ignore",)

    m = DATA_RE_LEGACY.search(line)
    if m:
        values = m.groups()
        return Sample(
            t=sample_time,
            t1=parse_float(values[0]),
            t2=parse_float(values[1]),
            ps=parse_float(values[2]),
            pt=parse_float(values[3]),
            dp_raw=parse_float(values[4]),
            dp=parse_float(values[4]),
            v=parse_float(values[5]),
            v_kmh=parse_float(values[5]) * 3.6,
            rho=parse_float(values[6]),
            err=int(values[7]),
        )

    # New diagnostic CSV from the latest Arduino sketch:
    # P_TOTAL_Pa,P_STATIC_Pa,DELTA_RAW_Pa,DELTA_Pa,SPEED_ms,SPEED_kmh,TEMP_C
    parts = [p.strip() for p in line.split(",")]
    if len(parts) == 7 and all(CSV_NUMBER_RE.match(p) for p in parts):
        pt = parse_float(parts[0])
        ps = parse_float(parts[1])
        dp_raw = parse_float(parts[2])
        dp = parse_float(parts[3])
        v = parse_float(parts[4])
        v_kmh = parse_float(parts[5])
        temp = parse_float(parts[6])
        rho = density_from_pressure_temperature(ps, temp)
        return Sample(
            t=sample_time,
            t1=temp,
            t2=temp,
            ps=ps,
            pt=pt,
            dp_raw=dp_raw,
            dp=dp,
            v=v,
            v_kmh=v_kmh,
            rho=rho,
            err=0,
        )

    e = ERROR_RE.search(line)
    if e:
        code = int(e.group(1))
        text = e.group(2).strip() or ERROR_NAMES.get(code, "Невідома помилка")
        return ("error", code, text)

    z = ZERO_OFFSET_RE.search(line)
    if z:
        return ("zero", parse_float(z.group(1)))

    upper = line.upper()
    if upper.startswith("TOTAL_SENSOR="):
        return ("sensor", "total", upper.split("=", 1)[1].strip())
    if upper.startswith("STATIC_SENSOR="):
        return ("sensor", "static", upper.split("=", 1)[1].strip())
    if upper == "ZERO CALIBRATION START":
        return ("info", line)
    if upper == "ZERO CALIBRATION DONE":
        return ("info", line)
    if upper.startswith("P_TOTAL_PA,"):
        return ("ignore",)
    if upper.startswith("ERROR,INVALID_SENSOR_DATA"):
        return ("error", 4, "Некоректні дані датчика")

    return ("text", line)


# -----------------------------
# Main application
# -----------------------------


class PitotMonitorApp:
    BG = "#080d16"
    PANEL = "#0f1724"
    PANEL_2 = "#131e2d"
    PANEL_3 = "#182538"
    BORDER = "#223247"
    TEXT = "#eef4fb"
    MUTED = "#8191a7"
    CYAN = "#42d6ff"
    GREEN = "#36e39a"
    RED = "#ff5f70"
    AMBER = "#ffcc66"
    GRID = "#7e8da0"

    CHARTS = [
        ("Температура T1", "t1", "°C", "Температура датчика Піто", "#42d6ff"),
        ("Температура T2", "t2", "°C", "Температура статичного датчика", "#8aa8ff"),
        ("Статичний тиск Ps", "ps", "Pa", "Тиск статичного порту", "#36e39a"),
        ("Повний тиск Pt", "pt", "Pa", "Тиск із трубки Піто", "#f9b86b"),
        ("Швидкість", "v", "m/s", "Повітряна швидкість", "#ff6eaa"),
        ("Густина повітря", "rho", "kg/m³", "Розрахункова густина", "#b18cff"),
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Pitot Monitor — Air Data Dashboard v8")
        self.root.geometry("1700x1100")
        self.root.minsize(1250, 850)
        self.root.configure(bg=self.BG)

        self.ser = None
        self.reader_thread = None
        self.reader_stop = threading.Event()
        self.rx_queue = queue.Queue()
        self.connected_port = ""
        self.session_start = time.monotonic()
        self.last_sample = None
        self.last_rx_timestamp = None
        self.plot_time = 0.0
        self.last_data_time = 0.0
        self.last_raw_line = ""
        self.last_error_code = 0
        self.nominal_sample_period = 0.10
        self.error_count = 0
        self.zero_offset = None
        self.autofollow = {key: True for _, key, _, _, _ in self.CHARTS}
        self.min_y_pad = {
            "t1": 0.10, "t2": 0.10, "ps": 5.0, "pt": 5.0,
            "dp": 1.0, "v": 0.25, "rho": 0.002,
        }
        self.drag_state = None
        self.redraw_pending = False

        self.port_var = tk.StringVar()
        self.connection_var = tk.StringVar(value="Непідключено")
        self.port_info_var = tk.StringVar(value="COM: —")
        self.clock_var = tk.StringVar(value="--:--:--")
        self.runtime_var = tk.StringVar(value="00:00:00")
        self.status_var = tk.StringVar(value="Система готова")
        self.zero_offset_var = tk.StringVar(value="Нульовий offset: —")
        self.arduino_state = tk.StringVar(value="Непідключено")
        self.pitot_state = tk.StringVar(value="Немає даних")
        self.static_state = tk.StringVar(value="Немає даних")
        self.error_count_var = tk.StringVar(value="0")

        self.value_vars = {
            "t1": tk.StringVar(value="—"),
            "t2": tk.StringVar(value="—"),
            "ps": tk.StringVar(value="—"),
            "pt": tk.StringVar(value="—"),
            "dp": tk.StringVar(value="—"),
            "v": tk.StringVar(value="—"),
            "v_kmh": tk.StringVar(value="—"),
            "rho": tk.StringVar(value="—"),
        }

        self.max_points = 6000
        self.history = {
            key: deque(maxlen=self.max_points)
            for key in ["time", "t1", "t2", "ps", "pt", "dp", "v", "rho"]
        }
        self.export_rows = deque(maxlen=30000)

        self.figures = {}
        self.axes = {}
        self.lines = {}
        self.canvases = {}
        self.auto_buttons = {}
        self.chart_value_labels = {}

        self._build_styles()
        self._build_ui()
        self.refresh_ports()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._tick()

    # -----------------------------
    # Styling
    # -----------------------------

    def _build_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "App.TButton",
            font=("Segoe UI", 9, "bold"),
            padding=(10, 7),
            foreground=self.TEXT,
            background=self.PANEL_3,
            borderwidth=0,
            relief="flat",
        )
        style.map("App.TButton", background=[("active", "#23354d"), ("pressed", "#0f1b29")])
        style.configure(
            "Accent.TButton",
            font=("Segoe UI", 9, "bold"),
            padding=(12, 8),
            foreground="#041019",
            background=self.CYAN,
            borderwidth=0,
            relief="flat",
        )
        style.map("Accent.TButton", background=[("active", "#76e6ff"), ("pressed", "#23bde6")])
        style.configure(
            "App.TCombobox",
            padding=7,
            fieldbackground="#0c1420",
            background="#0c1420",
            foreground=self.TEXT,
            arrowcolor=self.MUTED,
            borderwidth=0,
        )
        style.map("App.TCombobox", fieldbackground=[("readonly", "#0c1420")], foreground=[("readonly", self.TEXT)])
        style.configure(
            "Log.Treeview",
            rowheight=23,
            font=("Segoe UI", 9),
            background="#0b1320",
            fieldbackground="#0b1320",
            foreground="#dce7f3",
            borderwidth=0,
        )
        style.configure(
            "Log.Treeview.Heading",
            font=("Segoe UI", 8, "bold"),
            background=self.PANEL_3,
            foreground="#aebed0",
            relief="flat",
        )
        style.map("Log.Treeview", background=[("selected", "#173852")], foreground=[("selected", self.TEXT)])

    # -----------------------------
    # Small UI helpers
    # -----------------------------

    def _label(self, parent, text, size=9, weight="normal", color=None, bg=None, **pack):
        lbl = tk.Label(
            parent,
            text=text,
            font=("Segoe UI", size, weight),
            fg=color or self.TEXT,
            bg=bg or parent.cget("bg"),
            bd=0,
            highlightthickness=0,
        )
        if pack:
            lbl.pack(**pack)
        return lbl

    def _rounded_panel(self, parent, bg=None, border=None):
        frame = tk.Frame(parent, bg=bg or self.PANEL, highlightbackground=border or self.BORDER, highlightthickness=1, bd=0)
        return frame

    def _state_pill(self, parent, name, variable):
        outer = tk.Frame(parent, bg=self.PANEL, bd=0)
        outer.pack(fill="x", pady=4)
        dot = tk.Label(outer, text="●", fg=self.MUTED, bg=self.PANEL, font=("Segoe UI", 10, "bold"))
        dot.pack(side="left", padx=(10, 6), pady=7)
        body = tk.Frame(outer, bg=self.PANEL)
        body.pack(side="left", fill="x", expand=True, pady=6)
        tk.Label(body, text=name, font=("Segoe UI", 8, "bold"), fg=self.MUTED, bg=self.PANEL, anchor="w").pack(fill="x")
        tk.Label(body, textvariable=variable, font=("Segoe UI", 9, "bold"), fg=self.TEXT, bg=self.PANEL, anchor="w").pack(fill="x", pady=(1, 0))
        return dot

    def _metric_card(self, parent, title, unit, key, accent):
        card = tk.Frame(parent, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1, bd=0)
        tk.Frame(card, bg=accent, width=4).pack(side="left", fill="y")
        body = tk.Frame(card, bg=self.PANEL)
        body.pack(side="left", fill="both", expand=True, padx=12, pady=9)
        tk.Label(body, text=title, font=("Segoe UI", 8, "bold"), fg=self.MUTED, bg=self.PANEL, anchor="w").pack(fill="x")
        row = tk.Frame(body, bg=self.PANEL)
        row.pack(fill="x", pady=(4, 0))
        tk.Label(row, textvariable=self.value_vars[key], font=("Segoe UI", 19, "bold"), fg=self.TEXT, bg=self.PANEL).pack(side="left")
        tk.Label(row, text=unit, font=("Segoe UI", 8, "bold"), fg=self.MUTED, bg=self.PANEL).pack(side="left", padx=(7, 0), pady=(8, 0))
        return card

    def _create_chart_card(self, parent, title, key, unit, subtitle, accent, row, col):
        card = tk.Frame(parent, bg=self.PANEL, highlightbackground=self.BORDER, highlightthickness=1, bd=0)
        card.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)

        header = tk.Frame(card, bg=self.PANEL)
        header.pack(fill="x", padx=12, pady=(9, 5))
        left = tk.Frame(header, bg=self.PANEL)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text=title, font=("Segoe UI", 10, "bold"), fg=self.TEXT, bg=self.PANEL, anchor="w").pack(fill="x")
        tk.Label(left, text=subtitle, font=("Segoe UI", 8), fg=self.MUTED, bg=self.PANEL, anchor="w").pack(fill="x", pady=(1, 0))

        right = tk.Frame(header, bg=self.PANEL)
        right.pack(side="right")
        value = tk.Label(right, text="—", font=("Consolas", 9, "bold"), fg=accent, bg=self.PANEL)
        value.pack(side="left", padx=(0, 7))
        self.chart_value_labels[key] = value

        auto_btn = tk.Button(
            right, text="AUTO ON", command=lambda k=key: self.toggle_auto(k),
            bg="#163326", fg=self.GREEN, activebackground="#204934", activeforeground="#a4ffd9",
            relief="flat", bd=0, padx=9, pady=3, font=("Segoe UI", 8, "bold"), cursor="hand2"
        )
        auto_btn.pack(side="left", padx=(0, 4))
        self.auto_buttons[key] = auto_btn

        reset_btn = tk.Button(
            right, text="RESET", command=lambda k=key: self.reset_chart(k),
            bg=self.PANEL_3, fg="#c8d3df", activebackground="#253a52", activeforeground=self.TEXT,
            relief="flat", bd=0, padx=8, pady=3, font=("Segoe UI", 8, "bold"), cursor="hand2"
        )
        reset_btn.pack(side="left")

        fig = Figure(figsize=(7.6, 2.35), dpi=100, facecolor=self.PANEL)
        ax = fig.add_subplot(111)
        ax.set_facecolor("#0b1320")
        ax.set_xlabel("Час, с", color=self.MUTED, fontsize=8)
        ax.set_ylabel(unit, color=self.MUTED, fontsize=8)
        ax.tick_params(colors="#a8b4c2", labelsize=8, length=3, width=0.8)
        ax.xaxis.set_major_locator(MaxNLocator(nbins=6, integer=False, min_n_ticks=3))
        ax.yaxis.set_major_locator(MaxNLocator(nbins=5, integer=False, min_n_ticks=3))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda value, pos: f"{value:g}"))
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, pos, k=key: self._format_y_tick(k, value)))
        for spine in ax.spines.values():
            spine.set_color(self.BORDER)
            spine.set_linewidth(0.8)
        ax.grid(True, color=self.GRID, alpha=0.13, linewidth=0.8)
        ax.margins(x=0)

        line, = ax.plot(
            [], [], color=accent, linewidth=2.0,
            solid_capstyle="round", solid_joinstyle="round", antialiased=True
        )
        ax._pitot_key = key
        ax._pitot_unit = unit
        self.figures[key] = fig
        self.axes[key] = ax
        self.lines[key] = line

        fig.subplots_adjust(left=0.15, right=0.985, top=0.97, bottom=0.16)
        canvas = FigureCanvasTkAgg(fig, master=card)
        canvas_widget = canvas.get_tk_widget()
        canvas_widget.configure(bg=self.PANEL, highlightthickness=0, bd=0)
        canvas_widget.pack(fill="both", expand=True, padx=7, pady=(0, 7))
        self.canvases[key] = canvas

        canvas.mpl_connect("scroll_event", self.on_scroll_zoom)
        canvas.mpl_connect("button_press_event", self.on_chart_press)
        canvas.mpl_connect("button_release_event", self.on_chart_release)
        canvas.mpl_connect("motion_notify_event", self.on_chart_motion)
        return card

    def _format_y_tick(self, key, value):
        if key in ("ps", "pt"):
            return f"{value:,.0f}".replace(",", " ")
        if key == "dp":
            return f"{value:.1f}"
        if key in ("t1", "t2"):
            return f"{value:.1f}"
        if key == "v":
            return f"{value:.1f}"
        if key == "rho":
            return f"{value:.3f}"
        return f"{value:g}"

    # -----------------------------
    # Full interface
    # -----------------------------

    def _build_ui(self):
        shell = tk.Frame(self.root, bg=self.BG)
        shell.pack(fill="both", expand=True)

        sidebar = tk.Frame(shell, bg="#0a111c", width=255)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg="#0a111c")
        brand.pack(fill="x", padx=18, pady=(18, 14))
        mark = tk.Frame(brand, bg=self.CYAN, width=36, height=36)
        mark.pack(side="left")
        mark.pack_propagate(False)
        tk.Label(mark, text="P", font=("Segoe UI", 19, "bold"), fg="#061018", bg=self.CYAN).pack(expand=True)
        tk.Label(brand, text="PITOT LAB", font=("Segoe UI", 14, "bold"), fg=self.TEXT, bg="#0a111c").pack(side="left", padx=(10, 0), pady=(0, 1))

        tk.Label(sidebar, text="AIR DATA DASHBOARD", font=("Segoe UI", 8, "bold"), fg="#5e7189", bg="#0a111c").pack(anchor="w", padx=19, pady=(0, 10))

        conn = self._rounded_panel(sidebar, bg=self.PANEL, border=self.BORDER)
        conn.pack(fill="x", padx=13, pady=(0, 10))
        tk.Label(conn, text="ПІДКЛЮЧЕННЯ", font=("Segoe UI", 8, "bold"), fg=self.MUTED, bg=self.PANEL).pack(anchor="w", padx=11, pady=(10, 7))
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var, state="readonly", style="App.TCombobox", width=23)
        self.port_combo.pack(fill="x", padx=10, pady=(0, 7))
        row = tk.Frame(conn, bg=self.PANEL)
        row.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(row, text="Оновити", style="App.TButton", command=self.refresh_ports).pack(side="left", fill="x", expand=True)
        self.connect_btn = ttk.Button(row, text="Підключити", style="Accent.TButton", command=self.toggle_connection)
        self.connect_btn.pack(side="left", fill="x", expand=True, padx=(6, 0))
        tk.Label(conn, textvariable=self.port_info_var, font=("Consolas", 8), fg="#7689a0", bg=self.PANEL).pack(anchor="w", padx=11, pady=(0, 3))
        tk.Label(conn, textvariable=self.connection_var, font=("Segoe UI", 9, "bold"), fg=self.CYAN, bg=self.PANEL).pack(anchor="w", padx=11, pady=(0, 10))

        state_panel = self._rounded_panel(sidebar, bg=self.PANEL, border=self.BORDER)
        state_panel.pack(fill="x", padx=13, pady=(0, 10))
        tk.Label(state_panel, text="СТАН СИСТЕМИ", font=("Segoe UI", 8, "bold"), fg=self.MUTED, bg=self.PANEL).pack(anchor="w", padx=11, pady=(10, 4))
        self.arduino_dot = self._state_pill(state_panel, "Arduino Nano", self.arduino_state)
        self.pitot_dot = self._state_pill(state_panel, "BMP180 №1 • Pt / Піто", self.pitot_state)
        self.static_dot = self._state_pill(state_panel, "BMP180 №2 • Ps / Static", self.static_state)
        tk.Label(state_panel, textvariable=self.zero_offset_var, font=("Consolas", 8), fg="#6e8097", bg=self.PANEL, anchor="w").pack(fill="x", padx=11, pady=(2, 10))

        actions = self._rounded_panel(sidebar, bg=self.PANEL, border=self.BORDER)
        actions.pack(fill="x", padx=13, pady=(0, 10))
        tk.Label(actions, text="ДАНІ", font=("Segoe UI", 8, "bold"), fg=self.MUTED, bg=self.PANEL).pack(anchor="w", padx=11, pady=(10, 6))
        ttk.Button(actions, text="Очистити графіки", style="App.TButton", command=self.clear_history).pack(fill="x", padx=10, pady=(0, 6))
        ttk.Button(actions, text="Зберегти CSV", style="App.TButton", command=self.export_csv).pack(fill="x", padx=10, pady=(0, 10))

        tk.Label(sidebar, text="КЕРУВАННЯ ГРАФІКОМ", font=("Segoe UI", 8, "bold"), fg="#5e7189", bg="#0a111c").pack(anchor="w", padx=19, pady=(7, 6))
        tk.Label(
            sidebar,
            text="Колесо миші — масштаб\nЛКМ + перетягування — панорама\nAUTO — постійний автопідбір Y\nRESET — повернення огляду",
            justify="left",
            font=("Segoe UI", 8),
            fg="#6e8097",
            bg="#0a111c",
            anchor="w",
        ).pack(fill="x", padx=19)

        tk.Label(sidebar, text="" , bg="#0a111c").pack(fill="both", expand=True)
        tk.Label(sidebar, text="115200 baud  •  BMP180  •  Arduino Nano", font=("Consolas", 7), fg="#45566b", bg="#0a111c").pack(anchor="w", padx=19, pady=(0, 13))

        main = tk.Frame(shell, bg=self.BG)
        main.pack(side="left", fill="both", expand=True)

        top = tk.Frame(main, bg=self.BG, height=65)
        top.pack(fill="x", padx=22, pady=(16, 7))
        left_top = tk.Frame(top, bg=self.BG)
        left_top.pack(side="left", fill="x", expand=True)
        tk.Label(left_top, text="Air Data Dashboard", font=("Segoe UI", 21, "bold"), fg=self.TEXT, bg=self.BG).pack(anchor="w")
        tk.Label(left_top, text="Потік телеметрії та живі параметри трубки Піто", font=("Segoe UI", 9), fg=self.MUTED, bg=self.BG).pack(anchor="w", pady=(1, 0))

        right_top = tk.Frame(top, bg=self.BG)
        right_top.pack(side="right", pady=(7, 0))
        live = tk.Frame(right_top, bg="#11261e", highlightbackground="#1f4d3b", highlightthickness=1)
        live.pack(side="left", padx=(0, 14))
        self.live_dot = tk.Label(live, text="●", font=("Segoe UI", 9, "bold"), fg=self.GREEN, bg="#11261e")
        self.live_dot.pack(side="left", padx=(8, 4), pady=6)
        tk.Label(live, text="LIVE", font=("Segoe UI", 8, "bold"), fg="#a4ffd9", bg="#11261e").pack(side="left", padx=(0, 8), pady=6)
        tk.Label(right_top, textvariable=self.clock_var, font=("Consolas", 10), fg="#8090a4", bg=self.BG).pack(side="left", padx=(0, 14))
        tk.Label(right_top, textvariable=self.runtime_var, font=("Consolas", 10, "bold"), fg=self.TEXT, bg=self.BG).pack(side="left")

        metrics_wrap = tk.Frame(main, bg=self.BG)
        metrics_wrap.pack(fill="x", padx=17, pady=(0, 9))
        metric_specs = [
            ("T1 · ПІТО", "°C", "t1", self.CYAN),
            ("T2 · STATIC", "°C", "t2", "#8aa8ff"),
            ("Ps · STATIC", "Pa", "ps", self.GREEN),
            ("Pt · PITOT", "Pa", "pt", "#f9b86b"),
            ("ΔP · DYNAMIC", "Pa", "dp", "#ff7db3"),
            ("ШВИДКІСТЬ", "m/s", "v", "#ff6eaa"),
            ("ШВИДКІСТЬ", "km/h", "v_kmh", "#ff91bd"),
            ("ГУСТИНА", "kg/m³", "rho", "#b18cff"),
        ]
        for i in range(4):
            metrics_wrap.grid_columnconfigure(i, weight=1)
        for idx, spec in enumerate(metric_specs):
            card = self._metric_card(metrics_wrap, *spec)
            card.grid(row=idx // 4, column=idx % 4, sticky="nsew", padx=5, pady=5)

        # Main content uses grid so the six graphs and the error log are visible at the same time.
        content = tk.Frame(main, bg=self.BG)
        content.pack(fill="both", expand=True, padx=17, pady=(0, 13))
        content.pack_propagate(False)
        content.grid_rowconfigure(0, weight=1, minsize=390)
        content.grid_rowconfigure(1, weight=0, minsize=180)
        content.grid_columnconfigure(0, weight=1)

        charts_host = tk.Frame(content, bg=self.BG)
        charts_host.grid(row=0, column=0, sticky="nsew")

        log_host = self._rounded_panel(content, bg=self.PANEL, border=self.BORDER)
        self.log_host = log_host
        log_host.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        # Use three columns by two rows so all six graphs stay large while the log remains visible.
        for i in range(3):
            charts_host.grid_columnconfigure(i, weight=1, uniform="chart_col")
        for i in range(2):
            charts_host.grid_rowconfigure(i, weight=1, uniform="chart_row")
        for idx, chart in enumerate(self.CHARTS):
            _, key, unit, subtitle, accent = chart
            self._create_chart_card(charts_host, *chart, row=idx // 3, col=idx % 3)

        # Give the error log enough vertical room to show several events without hiding the charts.
        log_host.configure(height=180)
        log_host.pack_propagate(False)

        log_header = tk.Frame(log_host, bg=self.PANEL)
        log_header.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(log_header, text="ЖУРНАЛ ПОМИЛОК", font=("Segoe UI", 10, "bold"), fg=self.TEXT, bg=self.PANEL).pack(side="left")
        tk.Label(log_header, textvariable=self.status_var, font=("Segoe UI", 8), fg="#6f8297", bg=self.PANEL).pack(side="left", padx=(12, 0))
        tk.Label(log_header, textvariable=self.error_count_var, font=("Consolas", 10, "bold"), fg=self.AMBER, bg=self.PANEL).pack(side="right", padx=(6, 0))
        tk.Label(log_header, text="подій", font=("Segoe UI", 8), fg=self.MUTED, bg=self.PANEL).pack(side="right")
        tk.Button(
            log_header,
            text="Очистити",
            command=self.clear_log,
            bg=self.PANEL_3,
            fg="#ccd7e3",
            activebackground="#263c55",
            activeforeground=self.TEXT,
            relief="flat",
            bd=0,
            padx=9,
            pady=3,
            font=("Segoe UI", 8, "bold"),
            cursor="hand2",
        ).pack(side="right", padx=(14, 0))

        tree_wrap = tk.Frame(log_host, bg=self.PANEL)
        tree_wrap.pack(fill="both", expand=True, padx=11, pady=(0, 10))
        cols = ("time", "type", "code", "message", "raw")
        self.error_tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", style="Log.Treeview")
        headings = {"time": "Час", "type": "Тип", "code": "Код", "message": "Опис", "raw": "Рядок Arduino"}
        widths = {"time": 75, "type": 78, "code": 50, "message": 260, "raw": 850}
        for col in cols:
            self.error_tree.heading(col, text=headings[col])
            self.error_tree.column(col, width=widths[col], stretch=(col == "raw"), anchor=("center" if col in ("time", "type", "code") else "w"))
        self.error_tree.pack(side="left", fill="both", expand=True)
        sy = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.error_tree.yview)
        sy.pack(side="right", fill="y")
        sx = ttk.Scrollbar(log_host, orient="horizontal", command=self.error_tree.xview)
        sx.pack(fill="x", padx=11, pady=(0, 8))
        self.error_tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.error_tree.bind("<MouseWheel>", lambda e: self.error_tree.yview_scroll(int(-e.delta / 120), "units"))

        # Keep the log visible from the moment the window opens.
        self.root.after(260, self._ensure_log_height)

    def _ensure_log_height(self):
        # Re-apply the log height after geometry management has completed.
        try:
            self.log_host.configure(height=180)
            self.log_host.update_idletasks()
        except tk.TclError:
            pass

    # -----------------------------
    # Status and connection
    # -----------------------------

    def _set_dot(self, dot, state):
        color = {
            "Підключено": self.GREEN,
            "Очікування": self.AMBER,
            "Помилка": self.RED,
            "Немає даних": self.MUTED,
            "Непідключено": self.MUTED,
        }.get(state, self.MUTED)
        dot.configure(fg=color)

    def refresh_ports(self):
        if list_ports is None:
            self.status_var.set("pyserial не встановлено")
            return
        ports = list(list_ports.comports())
        labels = [f"{p.device} — {p.description or 'USB Serial'}" for p in ports]
        self.port_combo["values"] = labels
        if labels:
            current = self.port_var.get()
            if current not in labels:
                self.port_var.set(labels[0])
            self.status_var.set(f"Знайдено COM-портів: {len(labels)}")
        else:
            self.port_var.set("")
            self.status_var.set("COM-порти не знайдені")

    def selected_device(self):
        value = self.port_var.get().strip()
        return value.split(" — ", 1)[0] if value else ""

    def toggle_connection(self):
        if self.ser and self.ser.is_open:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if serial is None:
            messagebox.showerror("Залежності", "Не встановлено pyserial. Запусти run.bat ще раз.")
            return
        port = self.selected_device()
        if not port:
            messagebox.showwarning("COM-порт", "Спочатку обери COM-порт Arduino.")
            return
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.15)
            self.connected_port = port
            self.reader_stop.clear()
            self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self.reader_thread.start()
            self.session_start = time.monotonic()
            self.last_data_time = time.monotonic()
            self.last_rx_timestamp = None
            self.plot_time = 0.0
            self.connection_var.set("Підключено")
            self.port_info_var.set(f"{port}  •  115200 baud")
            self.arduino_state.set("Підключено")
            self.pitot_state.set("Очікування")
            self.static_state.set("Очікування")
            self._set_dot(self.arduino_dot, "Підключено")
            self._set_dot(self.pitot_dot, "Очікування")
            self._set_dot(self.static_dot, "Очікування")
            self.connect_btn.configure(text="Відключити", style="App.TButton")
            self.status_var.set(f"З'єднання з {port} встановлено")
            self.live_dot.configure(fg=self.GREEN)
        except Exception as exc:
            self.ser = None
            self.connection_var.set("Помилка")
            self.arduino_state.set("Помилка")
            self._set_dot(self.arduino_dot, "Помилка")
            self.add_log("ERROR", 900, "Не вдалося відкрити COM-порт", str(exc))
            messagebox.showerror("Помилка підключення", self.pretty_serial_error(exc, port))

    def pretty_serial_error(self, exc, port):
        txt = str(exc)
        low = txt.lower()
        if "access is denied" in low or "permission" in low or "permissionerror" in low:
            return (
                f"Не вдалося відкрити {port}. Порт зайнятий іншою програмою або Windows забороняє доступ.\n\n"
                "Закрий Serial Monitor / Serial Plotter та інші програми, які працюють із COM-портом, "
                "після чого натисни «Оновити» і повтори підключення."
            )
        if "cannot find the file" in low or "does not exist" in low:
            return f"Порт {port} більше не існує. Перепідключи Arduino Nano та онови список COM-портів."
        return txt

    def disconnect(self):
        self.reader_stop.set()
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.connected_port = ""
        self.connection_var.set("Непідключено")
        self.port_info_var.set("COM: —")
        self.arduino_state.set("Непідключено")
        self.pitot_state.set("Немає даних")
        self.static_state.set("Немає даних")
        self._set_dot(self.arduino_dot, "Непідключено")
        self._set_dot(self.pitot_dot, "Немає даних")
        self._set_dot(self.static_dot, "Немає даних")
        self.connect_btn.configure(text="Підключити", style="Accent.TButton")
        self.live_dot.configure(fg=self.MUTED)
        self.status_var.set("Відключено")

    def _reader_loop(self):
        while not self.reader_stop.is_set():
            try:
                if self.ser is None or not self.ser.is_open:
                    break
                raw = self.ser.readline()
                if not raw:
                    continue
                received_at = time.monotonic()
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self.rx_queue.put(("line", line, received_at))
            except (SerialException, OSError) as exc:
                self.rx_queue.put(("connection_error", str(exc)))
                break
            except Exception as exc:
                self.rx_queue.put(("connection_error", str(exc)))
                break

    # -----------------------------
    # Data processing
    # -----------------------------

    def _tick(self):
        now = time.monotonic()
        self.clock_var.set(datetime.now().strftime("%H:%M:%S"))
        elapsed = max(0.0, now - self.session_start)
        self.runtime_var.set(self.format_duration(elapsed))
        self.process_queue()
        self.update_connection_timeout()
        self.redraw_all()
        self.root.after(120, self._tick)

    @staticmethod
    def format_duration(seconds):
        seconds = int(max(0, seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def process_queue(self):
        processed = 0
        while processed < 180:
            try:
                item = self.rx_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1

            if isinstance(item, tuple):
                if item[0] == "connection_error":
                    self.add_log("ERROR", 900, "COM-з'єднання втрачено", item[1])
                    self.disconnect()
                    continue
                if item[0] == "line":
                    line = item[1]
                    received_at = item[2]
                else:
                    continue
            else:
                line = item
                received_at = time.monotonic()

            self.last_raw_line = line
            parsed = parse_line(line, sample_time=self.plot_time)

            if isinstance(parsed, Sample):
                s = parsed
                self.last_data_time = received_at
                if self.last_rx_timestamp is None:
                    self.plot_time = 0.0
                else:
                    rx_dt = received_at - self.last_rx_timestamp
                    if rx_dt > 0.25:
                        self.plot_time += rx_dt
                    else:
                        self.plot_time += self.nominal_sample_period
                self.last_rx_timestamp = received_at
                s.t = self.plot_time
                self.history["time"].append(self.plot_time)
                self.history["t1"].append(s.t1)
                self.history["t2"].append(s.t2)
                self.history["ps"].append(s.ps)
                self.history["pt"].append(s.pt)
                self.history["dp"].append(s.dp)
                self.history["v"].append(s.v)
                self.history["rho"].append(s.rho)
                self.last_sample = s
                self.export_rows.append([
                    datetime.now().isoformat(timespec="milliseconds"),
                    s.t1, s.t2, s.ps, s.pt, s.dp_raw, s.dp, s.v, s.v_kmh, s.rho, s.err,
                ])
                self.update_values(s)
                self.update_chart_values(s)
                self.update_sensor_status(s.err)
                if s.err != 0 and s.err != self.last_error_code:
                    self.add_log("ERROR", s.err, ERROR_NAMES.get(s.err, "Невідома помилка"), line)
                self.last_error_code = s.err
                continue

            if not isinstance(parsed, tuple) or not parsed:
                continue

            kind = parsed[0]
            if kind == "error":
                code, msg = parsed[1], parsed[2]
                self.update_sensor_status(code)
                self.add_log("ERROR", code, msg, line)
                self.last_error_code = code
            elif kind == "sensor":
                sensor, status = parsed[1], parsed[2]
                is_ok = status == "OK"
                state = "Підключено" if is_ok else "Помилка"
                if sensor == "total":
                    self.pitot_state.set(state)
                    self._set_dot(self.pitot_dot, state)
                elif sensor == "static":
                    self.static_state.set(state)
                    self._set_dot(self.static_dot, state)
            elif kind == "zero":
                self.zero_offset = parsed[1]
                self.zero_offset_var.set(f"Нульовий offset: {self.zero_offset:+.2f} Pa")
            elif kind == "info":
                self.status_var.set(parsed[1])
            elif kind == "text":
                self.status_var.set(parsed[1])

    def update_connection_timeout(self):
        if not (self.ser and self.ser.is_open):
            return
        if self.last_data_time and time.monotonic() - self.last_data_time > 3.0:
            self.connection_var.set("Підключено • немає даних")
            self.arduino_state.set("Підключено")
            self.pitot_state.set("Немає даних")
            self.static_state.set("Немає даних")
            self._set_dot(self.arduino_dot, "Підключено")
            self._set_dot(self.pitot_dot, "Немає даних")
            self._set_dot(self.static_dot, "Немає даних")
            self.live_dot.configure(fg=self.AMBER)

    def update_sensor_status(self, err):
        self.arduino_state.set("Підключено")
        self._set_dot(self.arduino_dot, "Підключено")
        if err == 1:
            self.static_state.set("Помилка")
            self._set_dot(self.static_dot, "Помилка")
            self.pitot_state.set("Очікування")
            self._set_dot(self.pitot_dot, "Очікування")
        elif err == 2:
            self.pitot_state.set("Помилка")
            self._set_dot(self.pitot_dot, "Помилка")
            self.static_state.set("Очікування")
            self._set_dot(self.static_dot, "Очікування")
        elif err == 0:
            self.pitot_state.set("Підключено")
            self.static_state.set("Підключено")
            self._set_dot(self.pitot_dot, "Підключено")
            self._set_dot(self.static_dot, "Підключено")
        else:
            self.pitot_state.set("Підключено")
            self.static_state.set("Підключено")
            self._set_dot(self.pitot_dot, "Підключено")
            self._set_dot(self.static_dot, "Підключено")
        self.live_dot.configure(fg=self.GREEN)

    def update_values(self, s: Sample):
        self.value_vars["t1"].set(f"{s.t1:.1f}")
        self.value_vars["t2"].set(f"{s.t2:.1f}")
        self.value_vars["ps"].set(f"{s.ps:,.0f}".replace(",", " "))
        self.value_vars["pt"].set(f"{s.pt:,.0f}".replace(",", " "))
        self.value_vars["dp"].set(f"{s.dp:.2f}")
        self.value_vars["v"].set(f"{s.v:.2f}")
        self.value_vars["v_kmh"].set(f"{s.v_kmh:.2f}")
        self.value_vars["rho"].set(f"{s.rho:.3f}" if math.isfinite(s.rho) else "—")

    def update_chart_values(self, s: Sample):
        self.chart_value_labels["t1"].configure(text=f"{s.t1:.1f} °C")
        self.chart_value_labels["t2"].configure(text=f"{s.t2:.1f} °C")
        self.chart_value_labels["ps"].configure(text=f"{s.ps:.0f} Pa")
        self.chart_value_labels["pt"].configure(text=f"{s.pt:.0f} Pa")
        self.chart_value_labels["v"].configure(text=f"{s.v:.2f} m/s")
        self.chart_value_labels["rho"].configure(text=f"{s.rho:.3f} kg/m³" if math.isfinite(s.rho) else "—")

    # -----------------------------
    # Logging
    # -----------------------------

    def add_log(self, kind, code, message, raw):
        self.error_tree.insert("", 0, values=(datetime.now().strftime("%H:%M:%S"), kind, code, message, raw))
        children = self.error_tree.get_children()
        if len(children) > 250:
            self.error_tree.delete(children[-1])
        self.error_count = len(self.error_tree.get_children())
        self.error_count_var.set(str(self.error_count))
        self.status_var.set(f"Подія {code}: {message}")

    def clear_log(self):
        for item in self.error_tree.get_children():
            self.error_tree.delete(item)
        self.error_count = 0
        self.error_count_var.set("0")
        self.status_var.set("Журнал очищено")

    # -----------------------------
    # Graph interaction
    # -----------------------------

    def current_time_max(self):
        return float(self.history["time"][-1]) if self.history["time"] else 1.0

    def _time_view_end(self):
        max_t = max(1.0, self.current_time_max())
        return max_t if max_t <= 2.0 else max_t * 1.025

    def _clamp_x_limits(self, ax, x0, x1):
        end = self._time_view_end()
        if x0 > x1:
            x0, x1 = x1, x0
        width = max(0.05, x1 - x0)
        if width >= end:
            return 0.0, end
        if x0 < 0.0:
            x1 -= x0
            x0 = 0.0
        if x1 > end:
            shift = x1 - end
            x0 -= shift
            x1 = end
        x0 = max(0.0, x0)
        x1 = max(x0 + 0.05, x1)
        return x0, min(end, x1)

    def _nice_padding(self, values, key):
        finite = [v for v in values if math.isfinite(v)]
        if not finite:
            return -1.0, 1.0
        lo = min(finite)
        hi = max(finite)
        span = hi - lo
        min_pad = self.min_y_pad.get(key, 1.0)
        if span <= 1e-12:
            pad = min_pad * 2.5
        else:
            pad = max(span * 0.10, min_pad)
        y0 = lo - pad
        y1 = hi + pad
        if key in ("v", "rho"):
            y0 = max(0.0, y0)
        if y1 <= y0:
            y1 = y0 + min_pad * 2.0
        return y0, y1

    def redraw_all(self):
        if not self.history["time"]:
            return
        x = list(self.history["time"])
        for _, key, _, _, _ in self.CHARTS:
            y = list(self.history[key])
            line = self.lines[key]
            line.set_data(x, y)
            ax = self.axes[key]
            if len(x) == 1:
                end = max(1.0, x[-1] + self.nominal_sample_period)
            else:
                end = max(1.0, x[-1] + self.nominal_sample_period)
            if self.autofollow[key]:
                ax.set_xlim(0.0, end)
                current_y0, current_y1 = ax.get_ylim()
                finite = [v for v in y if math.isfinite(v)]
                if finite:
                    lo, hi = min(finite), max(finite)
                    span = max(hi - lo, self.min_y_pad.get(key, 1.0))
                    pad = max(span * 0.12, self.min_y_pad.get(key, 1.0))
                    if len(finite) == 1:
                        target0, target1 = lo - pad * 2.0, hi + pad * 2.0
                    else:
                        target0, target1 = lo - pad, hi + pad
                    if key in ("v", "rho"):
                        target0 = max(0.0, target0)
                    margin = max((current_y1 - current_y0) * 0.04, self.min_y_pad.get(key, 1.0) * 0.5)
                    outside = (lo < current_y0 + margin) or (hi > current_y1 - margin)
                    if not math.isfinite(current_y0) or not math.isfinite(current_y1) or current_y1 <= current_y0 or outside:
                        ax.set_ylim(target0, target1)
            self.canvases[key].draw_idle()

    def toggle_auto(self, key):
        self.autofollow[key] = not self.autofollow[key]
        self.update_auto_button(key)
        if self.autofollow[key]:
            self.reset_chart(key, keep_auto=True)
            self.status_var.set("Автовідстеження увімкнено")
        else:
            self.status_var.set("Ручний режим графіка")

    def update_auto_button(self, key):
        button = self.auto_buttons[key]
        if self.autofollow[key]:
            button.configure(text="AUTO ON", bg="#163326", fg=self.GREEN, activebackground="#204934", activeforeground="#a4ffd9")
        else:
            button.configure(text="AUTO OFF", bg="#2a2024", fg="#d58f9d", activebackground="#432b31", activeforeground="#ffd0d7")

    def reset_chart(self, key, keep_auto=False):
        if not keep_auto:
            self.autofollow[key] = True
            self.update_auto_button(key)
        values = list(self.history[key])
        ax = self.axes[key]
        ax.set_xlim(0.0, self._time_view_end())
        y0, y1 = self._nice_padding(values, key)
        ax.set_ylim(y0, y1)
        self.canvases[key].draw_idle()

    def on_scroll_zoom(self, event):
        ax = event.inaxes
        if ax is None or event.xdata is None or event.ydata is None:
            return
        key = getattr(ax, "_pitot_key", None)
        if key is None:
            return

        self.autofollow[key] = False
        self.update_auto_button(key)

        x0, x1 = ax.get_xlim()
        y0, y1 = ax.get_ylim()
        cx, cy = event.xdata, event.ydata
        factor = 0.82 if event.button == "up" else 1.22

        key_mod = getattr(event, "key", None)
        if key_mod == "ctrl":
            nx0, nx1 = x0, x1
        else:
            nx0 = cx - (cx - x0) * factor
            nx1 = cx + (x1 - cx) * factor
            nx0, nx1 = self._clamp_x_limits(ax, nx0, nx1)

        if key_mod == "shift":
            ny0, ny1 = y0, y1
        else:
            ny0 = cy - (cy - y0) * factor
            ny1 = cy + (y1 - cy) * factor
            if ny1 - ny0 < 1e-9:
                return

        ax.set_xlim(nx0, nx1)
        ax.set_ylim(ny0, ny1)
        self.canvases[key].draw_idle()

    def on_chart_press(self, event):
        if event.inaxes is None or event.button != 1 or event.xdata is None or event.ydata is None:
            return
        key = getattr(event.inaxes, "_pitot_key", None)
        if key is None:
            return
        self.drag_state = {
            "ax": event.inaxes,
            "key": key,
            "x": event.xdata,
            "y": event.ydata,
            "xlim": event.inaxes.get_xlim(),
            "ylim": event.inaxes.get_ylim(),
            "moved": False,
        }

    def on_chart_release(self, event):
        if event.button == 1:
            self.drag_state = None

    def on_chart_motion(self, event):
        state = self.drag_state
        if state is None or event.inaxes is not state["ax"] or event.xdata is None or event.ydata is None:
            return
        ax = state["ax"]
        key = state["key"]
        dx = event.xdata - state["x"]
        dy = event.ydata - state["y"]
        if not state["moved"] and abs(dx) < 1e-6 and abs(dy) < 1e-6:
            return
        if not state["moved"]:
            state["moved"] = True
            self.autofollow[key] = False
            self.update_auto_button(key)
        x0, x1 = state["xlim"]
        y0, y1 = state["ylim"]
        nx0, nx1 = self._clamp_x_limits(ax, x0 - dx, x1 - dx)
        ax.set_xlim(nx0, nx1)
        ax.set_ylim(y0 - dy, y1 - dy)
        self.canvases[key].draw_idle()

    # -----------------------------
    # Data operations
    # -----------------------------

    def clear_history(self):
        for values in self.history.values():
            values.clear()
        self.session_start = time.monotonic()
        self.last_sample = None
        self.last_rx_timestamp = None
        self.plot_time = 0.0
        for key in self.autofollow:
            self.autofollow[key] = True
            self.update_auto_button(key)
            self.reset_chart(key, keep_auto=True)
        for key, var in self.value_vars.items():
            var.set("—")
        for key in self.chart_value_labels:
            self.chart_value_labels[key].configure(text="—")
        self.status_var.set("Графіки очищено")

    def export_csv(self):
        if not self.export_rows:
            messagebox.showinfo("CSV", "Поки що немає даних для збереження.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV файл", "*.csv"), ("Усі файли", "*.*")],
            title="Зберегти вимірювання",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle, delimiter=";")
                writer.writerow([
                    "Timestamp", "T1_C", "T2_C", "Ps_Pa", "Pt_Pa", "dP_raw_Pa",
                    "dP_Pa", "V_mps", "V_kmh", "rho_kg_m3", "ERR",
                ])
                writer.writerows(self.export_rows)
            self.status_var.set(f"CSV збережено: {path}")
        except Exception as exc:
            messagebox.showerror("CSV", f"Не вдалося зберегти файл:\n{exc}")

    def on_close(self):
        self.disconnect()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = PitotMonitorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
