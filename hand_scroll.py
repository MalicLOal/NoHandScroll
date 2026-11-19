"""
HandScroll - 웹캠 손 제스처로 문서 스크롤 + 커서 이동

  ☝  검지만 펴서 위를 가리킴            → 아래로 스크롤
  🖐  손바닥을 활짝 펼침                 → 위로 스크롤
  ✌  검지 + 중지만 펴고 약지·새끼 접음  → 중지 끝 위치로 마우스 커서 이동

트레이 아이콘으로 백그라운드 실행된다. 우클릭 메뉴의 '설정'에서
속도·인식·커서 옵션을 슬라이더로 바꾸면 즉시 적용되고 settings.ini에 저장된다.
"""

import configparser
import ctypes
import logging
import os
import sys
import threading
import time
from collections import deque

import cv2
import mediapipe as mp
import pystray
from PIL import Image, ImageDraw

IS_WINDOWS = sys.platform.startswith("win")
IS_FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable if IS_FROZEN else os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.ini")

FRAME_W, FRAME_H = 640, 480
TARGET_FPS = 30

# ───────────────────────── 로그 ─────────────────────────
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "HandScroll")
os.makedirs(APP_DIR, exist_ok=True)
logging.basicConfig(
    handlers=[logging.FileHandler(os.path.join(APP_DIR, "handscroll.log"), encoding="utf-8")],
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("handscroll")

if IS_WINDOWS:
    # 고해상도 배율(125%, 150% 등)에서도 커서 좌표가 화면 픽셀과 일치하도록
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# ───────────────────────── 설정 ─────────────────────────
# key: (section, type, default, min, max)
SETTING_SPECS = {
    "down_amount":    ("scroll", int, 60, 10, 600),
    "up_amount":      ("scroll", int, 60, 10, 600),
    "interval":       ("scroll", float, 0.04, 0.01, 0.30),
    "stable_frames":  ("recognition", int, 4, 1, 15),
    "strict_thumb":   ("recognition", bool, False, None, None),
    "cursor_enabled": ("cursor", bool, True, None, None),
    "cursor_area":    ("cursor", float, 0.6, 0.2, 1.0),
    "cursor_smooth":  ("cursor", float, 0.6, 0.0, 0.95),
    "camera_index":   ("camera", int, 0, 0, 5),
}


class Settings:
    """settings.ini ↔ 메모리. 값은 단순 속성이라 스레드 간에 그대로 읽어도 안전."""

    def __init__(self):
        for k, (_, _, default, _, _) in SETTING_SPECS.items():
            setattr(self, k, default)
        self._lock = threading.Lock()
        self.load()

    def load(self):
        cp = configparser.ConfigParser()
        if os.path.exists(SETTINGS_PATH):
            try:
                cp.read(SETTINGS_PATH, encoding="utf-8-sig")
            except (configparser.Error, OSError) as e:
                log.error("settings.ini 읽기 실패, 기본값 사용: %s", e)
        for k, (sec, typ, default, lo, hi) in SETTING_SPECS.items():
            try:
                if not cp.has_option(sec, k):
                    raise KeyError
                if typ is bool:
                    v = cp.getboolean(sec, k)
                else:
                    v = typ(cp.get(sec, k))
            except (KeyError, ValueError, configparser.Error):
                v = default
            self.set(k, v, save=False)
        self.save()
        log.info("settings: %s", self.as_dict())

    def set(self, key, value, save=True):
        sec, typ, default, lo, hi = SETTING_SPECS[key]
        if typ is not bool:
            value = typ(value)
            value = max(lo, min(hi, value))
        setattr(self, key, value)
        if save:
            self.save()

    def as_dict(self):
        return {k: getattr(self, k) for k in SETTING_SPECS}

    def save(self):
        cp = configparser.ConfigParser()
        for k, (sec, typ, *_ ) in SETTING_SPECS.items():
            if not cp.has_section(sec):
                cp.add_section(sec)
            v = getattr(self, k)
            cp.set(sec, k, ("true" if v else "false") if typ is bool else str(round(v, 3) if typ is float else v))
        with self._lock:
            try:
                with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                    f.write("; HandScroll 설정 — 트레이 메뉴 '설정' 창에서 바꾸는 것을 권장\n")
                    cp.write(f)
            except OSError as e:
                log.error("settings.ini 저장 실패: %s", e)


# ───────────────────────── 입력 출력 ─────────────────────────
def scroll(amount: int) -> None:
    """amount > 0: 위로, amount < 0: 아래로"""
    if IS_WINDOWS:
        MOUSEEVENTF_WHEEL = 0x0800
        ctypes.windll.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, ctypes.c_uint32(amount & 0xFFFFFFFF).value, 0)


def screen_rect():
    """(left, top, width, height) — 여러 모니터를 합친 가상 화면"""
    if IS_WINDOWS:
        u = ctypes.windll.user32
        return u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79)
    return 0, 0, 1920, 1080


def move_cursor(x: int, y: int) -> None:
    if IS_WINDOWS:
        ctypes.windll.user32.SetCursorPos(int(x), int(y))


# ───────────────────────── 제스처 판별 ─────────────────────────
WRIST = 0
FINGERS = {                    # (MCP, PIP, DIP, TIP)
    "index": (5, 6, 7, 8),
    "middle": (9, 10, 11, 12),
    "ring": (13, 14, 15, 16),
    "pinky": (17, 18, 19, 20),
}


def _d(a, b):
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def finger_state(lm, name):
    """'up'(펴짐) / 'down'(접힘) / 'mid'(애매)"""
    _, pip, _, tip = FINGERS[name]
    w = lm[WRIST]
    ratio = _d(lm[tip], w) / max(_d(lm[pip], w), 1e-6)
    if ratio > 1.25:
        return "up"
    if ratio < 1.0:
        return "down"
    return "mid"


def thumb_extended(lm):
    # 엄지 끝이 새끼 MCP·검지 MCP에서 충분히 멀면 펴진 것으로 판단 (손 크기로 정규화)
    palm = max(_d(lm[WRIST], lm[9]), 1e-6)
    return _d(lm[4], lm[17]) / palm > 1.0 and _d(lm[4], lm[5]) / palm > 0.45


def classify(lm, strict_thumb=False, cursor_enabled=True):
    """'point_up' / 'open' / 'cursor' / None"""
    s = {n: finger_state(lm, n) for n in FINGERS}
    thumb_up = thumb_extended(lm)

    # 손바닥 펼침: 네 손가락 + 엄지 모두 펴짐
    if all(v == "up" for v in s.values()) and thumb_up:
        return "open"

    # 검지 + 중지만 폄 (엄지는 상관없음) → 커서 이동
    if (cursor_enabled and s["index"] == "up" and s["middle"] == "up"
            and all(s[n] == "down" for n in ("ring", "pinky"))):
        return "cursor"

    # 검지만 펴서 위를 가리킴
    if s["index"] == "up" and all(s[n] == "down" for n in ("middle", "ring", "pinky")):
        if strict_thumb and thumb_up:
            return None
        tip, mcp = lm[8], lm[5]
        dy = mcp.y - tip.y          # 화면 좌표는 아래로 갈수록 y 증가
        dx = abs(tip.x - mcp.x)
        if dy > 0 and dy > dx * 1.2:  # 위쪽 방향 + 대체로 수직
            return "point_up"
    return None


def map_to_screen(nx, ny, area, rect):
    """카메라 화면의 중앙 area 비율 영역 → 모니터 전체로 매핑"""
    left, top, w, h = rect
    lo = (1.0 - area) / 2
    fx = min(1.0, max(0.0, (nx - lo) / area))
    fy = min(1.0, max(0.0, (ny - lo) / area))
    return left + fx * (w - 1), top + fy * (h - 1)


# ───────────────────────── 메인 워커 ─────────────────────────
class HandScroller:
    def __init__(self):
        self.running = True
        self.paused = False
        self.show_preview = False
        self.current = None           # 확정된 제스처
        self.on_state_change = None   # 트레이 아이콘 갱신 콜백
        self.settings = Settings()
        self._cam_in_use = None
        self._history = deque(maxlen=self.settings.stable_frames)
        self._last_scroll = 0.0
        self._cursor_pos = None       # 부드럽게 따라가는 현재 커서 위치

    def _open_camera(self):
        idx = self.settings.camera_index
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW) if IS_WINDOWS else cv2.VideoCapture(idx)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_W)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_H)
        self._cam_in_use = idx
        return cap

    def _set_gesture(self, g):
        if g != self.current:
            self.current = g
            if g != "cursor":
                self._cursor_pos = None
            log.info("gesture: %s", g)
            if self.on_state_change:
                self.on_state_change()

    def _update_cursor(self, lm):
        st = self.settings
        rect = screen_rect()
        tx, ty = map_to_screen(lm[12].x, lm[12].y, st.cursor_area, rect)
        if self._cursor_pos is None:
            self._cursor_pos = (tx, ty)
        else:
            a = st.cursor_smooth
            px, py = self._cursor_pos
            self._cursor_pos = (px * a + tx * (1 - a), py * a + ty * (1 - a))
        move_cursor(*self._cursor_pos)

    def run(self):
        hands = mp.solutions.hands.Hands(
            max_num_hands=1,
            model_complexity=0,
            min_detection_confidence=0.7,
            min_tracking_confidence=0.6,
        )
        drawer = mp.solutions.drawing_utils
        cap = None
        preview_open = False
        frame_dt = 1.0 / TARGET_FPS

        try:
            while self.running:
                st = self.settings

                # 일시정지 중엔 카메라를 놓아준다 (웹캠 LED 꺼짐)
                if self.paused:
                    if cap is not None:
                        cap.release()
                        cap = None
                    if preview_open:
                        cv2.destroyAllWindows()
                        preview_open = False
                    self._history.clear()
                    self._set_gesture(None)
                    time.sleep(0.2)
                    continue

                # 설정 창에서 카메라 번호가 바뀌었으면 다시 연다
                if cap is not None and self._cam_in_use != st.camera_index:
                    cap.release()
                    cap = None

                if cap is None:
                    cap = self._open_camera()
                    if not cap.isOpened():
                        log.error("카메라를 열 수 없음 (index=%d)", st.camera_index)
                        cap.release()
                        cap = None
                        time.sleep(2)
                        continue

                if self._history.maxlen != st.stable_frames:
                    self._history = deque(self._history, maxlen=st.stable_frames)

                t0 = time.time()
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.05)
                    continue

                frame = cv2.flip(frame, 1)
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                res = hands.process(rgb)

                raw = None
                lm = None
                if res.multi_hand_landmarks:
                    lm = res.multi_hand_landmarks[0].landmark
                    raw = classify(lm, st.strict_thumb, st.cursor_enabled)
                    if self.show_preview:
                        drawer.draw_landmarks(frame, res.multi_hand_landmarks[0],
                                              mp.solutions.hands.HAND_CONNECTIONS)

                # 연속 N프레임 같은 결과일 때만 상태 변경
                hist = self._history
                hist.append(raw)
                if len(hist) == hist.maxlen and len(set(hist)) == 1:
                    self._set_gesture(raw)

                now = time.time()
                if self.current == "cursor":
                    # 이번 프레임도 커서 제스처일 때만 따라감 (흔들릴 때 튀는 것 방지)
                    if raw == "cursor" and lm is not None:
                        self._update_cursor(lm)
                elif self.current and now - self._last_scroll >= st.interval:
                    scroll(-st.down_amount if self.current == "point_up" else st.up_amount)
                    self._last_scroll = now

                # 미리보기 창
                if self.show_preview:
                    h, w = frame.shape[:2]
                    lo = (1.0 - st.cursor_area) / 2
                    cv2.rectangle(frame, (int(w * lo), int(h * lo)),
                                  (int(w * (1 - lo)), int(h * (1 - lo))), (255, 160, 0), 1)
                    label = {"point_up": "DOWN  (index up)", "open": "UP  (open palm)",
                             "cursor": "CURSOR  (index + middle)"}.get(self.current, "-")
                    cv2.putText(frame, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
                    cv2.imshow("HandScroll Preview", frame)
                    preview_open = True
                    if cv2.waitKey(1) & 0xFF == 27:  # ESC로 미리보기 닫기
                        self.show_preview = False
                elif preview_open:
                    cv2.destroyAllWindows()
                    preview_open = False

                elapsed = time.time() - t0
                if elapsed < frame_dt:
                    time.sleep(frame_dt - elapsed)
        except Exception:
            log.exception("워커 오류")
        finally:
            if cap is not None:
                cap.release()
            cv2.destroyAllWindows()
            hands.close()


# ───────────────────────── 설정 GUI ─────────────────────────
class SettingsWindow:
    """tkinter 설정 창. 별도 스레드에서 실행되며, 값을 움직이면 즉시 적용·저장된다."""

    _thread = None
    _root = None

    def __init__(self, scroller):
        self.scroller = scroller

    def show(self):
        cls = SettingsWindow
        if cls._thread and cls._thread.is_alive():
            root = cls._root
            if root is not None:
                try:
                    root.after(0, lambda: (root.deiconify(), root.lift(), root.focus_force()))
                except Exception:
                    pass
            return
        cls._thread = threading.Thread(target=self._run, daemon=True)
        cls._thread.start()

    def _run(self):
        import tkinter as tk
        from tkinter import ttk

        st = self.scroller.settings
        root = tk.Tk()
        SettingsWindow._root = root
        root.title("HandScroll 설정")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        pad = {"padx": 12, "pady": 4}
        body = ttk.Frame(root, padding=10)
        body.grid(sticky="nsew")
        row = [0]
        refreshers = []   # 기본값 복원 시 위젯 값을 다시 맞추는 함수들

        def section(title):
            ttk.Label(body, text=title, font=("Malgun Gothic", 10, "bold")).grid(
                row=row[0], column=0, columnspan=3, sticky="w", pady=(10, 2), padx=4)
            row[0] += 1

        def slider(key, label, fmt, step, hint=""):
            _, typ, _, lo, hi = SETTING_SPECS[key]
            var = tk.DoubleVar(value=getattr(st, key))
            val_lbl = ttk.Label(body, width=8, anchor="e")

            def on_move(_=None):
                v = round(var.get() / step) * step
                if typ is int:
                    v = int(round(v))
                st.set(key, v, save=False)
                val_lbl.config(text=fmt.format(getattr(st, key)))

            ttk.Label(body, text=label).grid(row=row[0], column=0, sticky="w", **pad)
            sc = ttk.Scale(body, from_=lo, to=hi, variable=var, length=240, command=on_move)
            sc.grid(row=row[0], column=1, **pad)
            sc.bind("<ButtonRelease-1>", lambda e: st.save())
            val_lbl.grid(row=row[0], column=2, sticky="e", **pad)
            on_move()
            refreshers.append(lambda: (var.set(getattr(st, key)), on_move()))
            row[0] += 1
            if hint:
                ttk.Label(body, text=hint, foreground="#777").grid(
                    row=row[0], column=0, columnspan=3, sticky="w", padx=14)
                row[0] += 1

        def check(key, label):
            var = tk.BooleanVar(value=getattr(st, key))
            ttk.Checkbutton(body, text=label, variable=var,
                            command=lambda: st.set(key, bool(var.get()))).grid(
                row=row[0], column=0, columnspan=3, sticky="w", **pad)
            refreshers.append(lambda: var.set(getattr(st, key)))
            row[0] += 1

        section("스크롤")
        slider("down_amount", "아래로 속도", "{:d}", 5, "한 번에 움직이는 양 (120 = 휠 한 칸)")
        slider("up_amount", "위로 속도", "{:d}", 5)
        slider("interval", "반복 간격", "{:.2f}초", 0.01, "작을수록 더 자주 스크롤")

        section("인식")
        slider("stable_frames", "반응 지연", "{:d}프레임", 1, "오작동이 많으면 올리고, 반응이 느리면 내리기")
        check("strict_thumb", "검지 제스처에서 엄지도 반드시 접기")

        section("커서 (검지 + 중지)")
        check("cursor_enabled", "커서 이동 제스처 사용")
        slider("cursor_area", "조작 영역", "{:.0%}", 0.05, "작을수록 손을 조금만 움직여도 화면 끝까지 이동")
        slider("cursor_smooth", "떨림 보정", "{:.2f}", 0.05, "높을수록 부드럽지만 반응이 늦어짐")

        section("카메라")
        slider("camera_index", "웹캠 번호", "{:d}", 1)
        prev_var = tk.BooleanVar(value=self.scroller.show_preview)

        def toggle_prev():
            self.scroller.show_preview = bool(prev_var.get())

        ttk.Checkbutton(body, text="카메라 미리보기 (인식 영역 표시)", variable=prev_var,
                        command=toggle_prev).grid(row=row[0], column=0, columnspan=3, sticky="w", **pad)
        row[0] += 1

        btns = ttk.Frame(body)
        btns.grid(row=row[0], column=0, columnspan=3, sticky="e", pady=(12, 0))

        def reset():
            for k, (_, _, default, _, _) in SETTING_SPECS.items():
                st.set(k, default, save=False)
            st.save()
            for f in refreshers:
                f()

        def close():
            st.save()
            SettingsWindow._root = None
            root.destroy()

        ttk.Button(btns, text="기본값", command=reset).pack(side="left", padx=4)
        ttk.Button(btns, text="닫기", command=close).pack(side="left", padx=4)
        ttk.Label(body, text="변경 사항은 바로 적용되고 자동 저장됩니다.", foreground="#777").grid(
            row=row[0] + 1, column=0, columnspan=3, sticky="w", padx=4, pady=(6, 0))

        root.protocol("WM_DELETE_WINDOW", close)
        root.mainloop()


# ───────────────────────── 트레이 아이콘 ─────────────────────────
def make_icon(color):
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=color)
    d.polygon([(32, 8), (20, 24), (44, 24)], fill="white")
    d.polygon([(32, 56), (20, 40), (44, 40)], fill="white")
    d.rectangle((29, 22, 35, 42), fill="white")
    return img


ICONS = {
    "idle": make_icon((40, 120, 220)),
    "active": make_icon((30, 170, 80)),
    "cursor": make_icon((230, 140, 20)),
    "paused": make_icon((130, 130, 130)),
}


def single_instance():
    """이미 실행 중이면 False"""
    if not IS_WINDOWS:
        return True
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\HandScroll_SingleInstance")
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main():
    if not single_instance():
        if IS_WINDOWS:
            ctypes.windll.user32.MessageBoxW(0, "HandScroll이 이미 실행 중입니다.", "HandScroll", 0x40)
        return

    scroller = HandScroller()
    settings_win = SettingsWindow(scroller)
    worker = threading.Thread(target=scroller.run, daemon=True)

    def refresh(icon=None):
        if scroller.paused:
            icon_obj.icon, icon_obj.title = ICONS["paused"], "HandScroll - 일시정지"
        elif scroller.current == "cursor":
            icon_obj.icon, icon_obj.title = ICONS["cursor"], "HandScroll - 커서 이동"
        elif scroller.current:
            icon_obj.icon = ICONS["active"]
            icon_obj.title = "HandScroll - " + ("아래로 스크롤" if scroller.current == "point_up" else "위로 스크롤")
        else:
            icon_obj.icon, icon_obj.title = ICONS["idle"], "HandScroll - 대기 중"

    def toggle_pause(icon, item):
        scroller.paused = not scroller.paused
        refresh()

    def toggle_preview(icon, item):
        scroller.show_preview = not scroller.show_preview

    def open_settings(icon, item):
        settings_win.show()

    def quit_app(icon, item):
        scroller.running = False
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("설정", open_settings, default=True),
        pystray.MenuItem("일시정지", toggle_pause, checked=lambda i: scroller.paused),
        pystray.MenuItem("카메라 미리보기", toggle_preview, checked=lambda i: scroller.show_preview),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("종료", quit_app),
    )
    icon_obj = pystray.Icon("HandScroll", ICONS["idle"], "HandScroll - 대기 중", menu)
    scroller.on_state_change = refresh

    worker.start()
    log.info("HandScroll 시작")
    icon_obj.run()          # 메인 스레드에서 트레이 루프
    scroller.running = False
    worker.join(timeout=3)
    log.info("HandScroll 종료")


if __name__ == "__main__":
    main()
