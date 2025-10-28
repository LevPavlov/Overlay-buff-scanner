"""
OverlayBuffScanner - минимальная сборка:
- dxcam захват
- matchTemplate для нескольких шаблонов из templates/
- region picker (Shift+F9) с визуальным прямоугольником (как Snipping Tool)
- hotkeys: F8 - toggle, F10 - quit, Shift+F9 - select region
- overlay Tkinter (click-through)
"""

import os
import time
import json
import threading
import ctypes
from ctypes import wintypes
from pathlib import Path

import dxcam
import cv2
import numpy as np
from PIL import Image, ImageTk
import tkinter as tk
import keyboard  # глобальные hotkeys

APP_DIR = Path(__file__).parent
TEMPLATES_DIR = APP_DIR / "templates"
CONFIG_PATH = APP_DIR / "config.json"

# --- Default config ---
DEFAULT_CONFIG = {
    "search_region": [300, 1000, 1100, 1050],   # (left, top, right, bottom)
    "overlay_pos": [100, 100],
    "threshold": 0.82,
    "hotkeys": {"toggle": "F8", "select_region": "shift+f9", "quit": "F10"},
    # buffs: list of {"name": "...", "file": "templates/icon.png", "refreshable": true, "duration": null}
    "buffs": [
        # Пример: {"name": "Shield", "file": "templates/icon_shield.png", "refreshable": True, "duration": None}
    ]
}

# --- Helpers: load/save config ---
def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        c = DEFAULT_CONFIG.copy()
        # если есть образцы в templates - добавить автоматом
        buffs = []
        if TEMPLATES_DIR.exists():
            for p in sorted(TEMPLATES_DIR.glob("*.png")):
                buffs.append({"name": p.stem, "file": str(p.relative_to(APP_DIR)), "refreshable": True, "duration": None})
        c["buffs"] = buffs
        save_config(c)
        return c

def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

# --- Click-through helper for overlay window ---
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOPMOST = 0x0008

def make_window_clickthrough(root):
    hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    exStyle = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    newEx = exStyle | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
    ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, newEx)

# --- Region picker: fullscreen transient window capturing mouse drag ---
def pick_region_via_drag():
    """
    Показывает fullscreen прозрачное окно, дает пользователю перетащить прямоугольник.
    Возвращает (left, top, right, bottom) в координатах экрана или None.
    """
    result = {"rect": None}

    sel_root = tk.Tk()
    sel_root.attributes("-fullscreen", True)
    sel_root.attributes("-topmost", True)
    sel_root.overrideredirect(True)

    # полупрозрачный затемнённый фон
    canvas = tk.Canvas(sel_root, bg="black")
    canvas.pack(fill="both", expand=True)
    canvas.configure(cursor="cross")

    start = [0, 0]
    rect_id = None

    def on_button_press(event):
        start[0], start[1] = event.x_root, event.y_root

    def on_move(event):
        nonlocal rect_id
        x0, y0 = start
        x1, y1 = event.x_root, event.y_root
        # очистить прошлый
        if rect_id:
            canvas.delete(rect_id)
        # рисуем полупрозрачный прямоугольник (через outline + stipple)
        rect_id = canvas.create_rectangle(x0, y0, x1, y1, outline="cyan", width=2)

    def on_button_release(event):
        x0, y0 = start
        x1, y1 = event.x_root, event.y_root
        left, right = sorted([x0, x1])
        top, bottom = sorted([y0, y1])
        result["rect"] = [int(left), int(top), int(right), int(bottom)]
        sel_root.destroy()

    # Bind mouse
    canvas.bind("<ButtonPress-1>", on_button_press)
    canvas.bind("<B1-Motion>", on_move)
    canvas.bind("<ButtonRelease-1>", on_button_release)

    # Instruction label
    instr = tk.Label(sel_root, text="Перетащи область для сканирования. ESC - отмена", bg="black", fg="white")
    instr.place(x=10, y=10)

    def on_esc(e=None):
        sel_root.destroy()

    sel_root.bind("<Escape>", on_esc)
    sel_root.mainloop()
    return result["rect"]

# --- Main application class ---
class BuffMonitorApp:
    def __init__(self):
        self.cfg = load_config()
        self.running = False
        self.stop_event = threading.Event()
        self.camera = dxcam.create(output_idx=0)
        self.load_templates()
        self.overlay_root = None
        self.overlay_labels = {}
        self.monitor_thread = None

        # hotkeys
        keyboard.add_hotkey(self.cfg["hotkeys"].get("select_region", "shift+f9"), self.on_select_region)
        keyboard.add_hotkey(self.cfg["hotkeys"].get("toggle", "F8"), self.toggle_running)
        keyboard.add_hotkey(self.cfg["hotkeys"].get("quit", "F10"), self.quit)

        print("Hotkeys: select_region={}, toggle={}, quit={}".format(
            self.cfg["hotkeys"].get("select_region"), self.cfg["hotkeys"].get("toggle"), self.cfg["hotkeys"].get("quit")
        ))

        # start overlay window (click-through)
        self.start_overlay()

    def load_templates(self):
        # загружаем все шаблоны из cfg
        for b in self.cfg.get("buffs", []):
            fpath = APP_DIR / b["file"]
            if not fpath.exists():
                print(f"[WARN] template not found: {fpath}")
                b["template"] = None
                continue
            img = cv2.imread(str(fpath), cv2.IMREAD_COLOR)
            if img is None:
                print(f"[WARN] cannot load template: {fpath}")
                b["template"] = None
                continue
            b["template"] = img
            b["t_h"], b["t_w"] = img.shape[:2]
            b["active"] = False
            b["icon_data"] = None
            b["last_seen"] = 0

    def start_overlay(self):
        # создаём окно overlay и делаем click-through
        self.overlay_root = tk.Tk()
        self.overlay_root.overrideredirect(True)
        self.overlay_root.attributes("-topmost", True)
        self.overlay_root.configure(bg="black")
        # позиция по конфигу
        x, y = self.cfg.get("overlay_pos", [100, 100])
        self.overlay_root.geometry(f"+{x}+{y}")
        # контейнер для иконок (будем добавлять Label'ы)
        # делаем click-through:
        make_window_clickthrough(self.overlay_root)
        # главное окно в отдельном потоке обновлений
        threading.Thread(target=self.overlay_loop, daemon=True).start()

    def overlay_loop(self):
        # независимо обновляем окно раз в 50ms
        while not self.stop_event.is_set():
            # обновляем видимые иконки на overlay
            # active buffs:
            active = [b for b in self.cfg.get("buffs", []) if b.get("active") and b.get("icon_data") is not None]
            # синхронизируем label'ы
            # удаляем неактивные
            keys = list(self.overlay_labels.keys())
            for k in keys:
                if not any(b["name"] == k for b in active):
                    try:
                        self.overlay_labels[k].place_forget()
                        del self.overlay_labels[k]
                    except Exception:
                        pass
            # добавляем/обновляем активные
            x0, y0 = self.cfg.get("overlay_pos", [100, 100])
            spacing = 70
            for i, b in enumerate(self.cfg.get("buffs", [])):
                if not b.get("active") or b.get("icon_data") is None:
                    continue
                name = b["name"]
                pil = Image.fromarray(cv2.cvtColor(b["icon_data"], cv2.COLOR_BGR2RGB))
                tk_img = ImageTk.PhotoImage(pil)
                if name not in self.overlay_labels:
                    lbl = tk.Label(self.overlay_root, image=tk_img, bg="black")
                    lbl.image = tk_img
                    lbl.place(x=x0 + i * spacing, y=y0)
                    self.overlay_labels[name] = lbl
                else:
                    lbl = self.overlay_labels[name]
                    lbl.config(image=tk_img)
                    lbl.image = tk_img
                    lbl.place(x=x0 + i * spacing, y=y0)
            try:
                self.overlay_root.update_idletasks()
                self.overlay_root.update()
            except tk.TclError:
                # окно могло быть уничтожено
                break
            time.sleep(0.05)

    def on_select_region(self):
        print("Region selection started (Shift+F9). Выдели область мышью.")
        # Для выбора региона создаём модальное окно, которое перехватывает мышь
        rect = pick_region_via_drag()
        if rect:
            print("Selected region:", rect)
            # сохранить в конфиг
            self.cfg["search_region"] = rect
            save_config(self.cfg)
        else:
            print("Region selection cancelled.")

    def toggle_running(self):
        if not self.running:
            print("Start monitoring")
            self.running = True
            self.stop_event.clear()
            self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
            self.monitor_thread.start()
        else:
            print("Pause monitoring")
            self.running = False
            self.stop_event.set()
            # clear active states
            for b in self.cfg.get("buffs", []):
                b["active"] = False
                b["icon_data"] = None

    def quit(self):
        print("Quitting...")
        self.running = False
        self.stop_event.set()
        try:
            if self.overlay_root:
                self.overlay_root.destroy()
        except Exception:
            pass
        os._exit(0)

    def monitor_loop(self):
        """
        Захват кадра через dxcam из self.cfg["search_region"]
        и поиск всех шаблонов.
        """
        region = self.cfg.get("search_region", [300, 1000, 1100, 1050])
        left, top, right, bottom = region
        w = right - left
        h = bottom - top
        threshold = float(self.cfg.get("threshold", 0.82))

        print(f"Monitoring region {region}, size {w}x{h}, threshold {threshold}")

        while self.running and not self.stop_event.is_set():
            frame = self.camera.grab(region=(left, top, right, bottom))
            if frame is None:
                time.sleep(0.05)
                continue
            now = time.time()
            # искать все шаблоны
            for b in self.cfg.get("buffs", []):
                tmpl = b.get("template")
                if tmpl is None:
                    b["active"] = False
                    b["icon_data"] = None
                    continue
                res = cv2.matchTemplate(frame, tmpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= threshold:
                    x, y = max_loc
                    t_h, t_w = b["t_h"], b["t_w"]
                    # сохранить вырез (в координатах фрейма)
                    b["icon_data"] = frame[y:y + t_h, x:x + t_w]
                    b["active"] = True
                    b["last_seen"] = now
                else:
                    b["active"] = False
                    b["icon_data"] = None
            time.sleep(0.05)


if __name__ == "__main__":
    # ensure templates dir exists
    TEMPLATES_DIR.mkdir(exist_ok=True)
    cfg = load_config()
    print("Config loaded. Put your PNG templates into templates/ and add them to config.json (or let auto add).")
    print("Hotkeys: select region -> Shift+F9, toggle monitor -> F8, quit -> F10")
    app = BuffMonitorApp()
    # keep main thread alive (keyboard waits on hotkeys)
    keyboard.wait()
