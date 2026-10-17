"""
Razer Battery Tray — Razer ワイヤレスマウスのバッテリー残量をタスクトレイに表示する常駐アプリ（Windows用）
対象: Razer DeathAdder V3 Pro
      （既知のIDで見つからない場合は接続中の全Razer機器を自動で試すので、他の無線マウスでも動くことがあります）

使い方:
  python  razer_battery_tray.py --diag   … 動作確認。接続中のRazer機器と読み取り結果をコンソールに表示
  pythonw razer_battery_tray.py          … タスクトレイに常駐（コンソール窓なし）

機能:
  - マウス型アイコンの水位で残量を表示（20%以下は赤、充電中は稲妻）
  - 残量低下（20% / 10%）と充電完了の通知
  - 残量の減り方・増え方から「残り使用時間」「満充電までの時間」を予測
  - 右クリックメニューから Windows 起動時の自動起動をオン/オフ
  - 二重起動防止、エラーログ
  設定・履歴・ログは %APPDATA%\\RazerBatteryTray に保存されます。

仕組み:
  Razer機器は 90 バイトの HID フィーチャーレポートで命令を受け付けます。
  コマンドクラス 0x07 / ID 0x80 がバッテリー残量（0-255）、0x84 が充電状態です。
  Synapse の API は使わず、USB HID で直接問い合わせます（ドライバの入れ替え不要）。
"""
__version__ = "0.2.0"

import argparse
import json
import logging
import os
import platform
import sys
import threading
import time
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import hid  # pip install hidapi
from PIL import Image, ImageDraw

# ---------------------------------------------------------------- 設定
POLL_SECONDS = 60          # 何秒ごとに残量を読むか
LOW_THRESHOLD = 20         # この%以下で赤
MID_THRESHOLD = 50         # この%以下で黄
NOTIFY_LEVELS = (20, 10)   # この%以下になったら通知
NOTIFY_RESET_MARGIN = 5    # 通知済みのしきい値を、何%戻ったら再び通知対象にするか

APP_NAME = "RazerBatteryTray"
DATA_DIR = (Path(os.environ["APPDATA"]) / APP_NAME if os.environ.get("APPDATA")
            else Path.home() / f".{APP_NAME.lower()}")
LOG_FILE = DATA_DIR / "log.txt"
HISTORY_FILE = DATA_DIR / "history.csv"
SETTINGS_FILE = DATA_DIR / "settings.json"

log = logging.getLogger(APP_NAME)

RAZER_VID = 0x1532
# DeathAdder V3 Pro の製品ID（優先して試す）
KNOWN_PIDS = {
    0x00B6: "DeathAdder V3 Pro (有線)",
    0x00B7: "DeathAdder V3 Pro (無線)",
    0x00C2: "DeathAdder V3 Pro (有線)",
    0x00C3: "DeathAdder V3 Pro (無線/HyperPolling)",
}
# トランザクションID。新しめの機種は 0x1F。念のため順に試す
TRANSACTION_IDS = (0x1F, 0x3F, 0xFF)

# ---------------------------------------------------------------- Razer プロトコル
REPORT_LEN = 90
CMD_CLASS_POWER = 0x07
CMD_BATTERY = 0x80
CMD_CHARGING = 0x84
STATUS_NAMES = {0x00: "new", 0x01: "busy", 0x02: "ok", 0x03: "failure",
                0x04: "timeout", 0x05: "not supported"}
STATUS_BUSY, STATUS_OK = 0x01, 0x02


def build_report(tid: int, cmd_class: int, cmd_id: int, data_size: int = 0x02) -> bytes:
    """90バイトの Razer レポートを組み立てる（末尾から2番目は XOR チェックサム）"""
    r = bytearray(REPORT_LEN)
    r[1] = tid
    r[5] = data_size
    r[6] = cmd_class
    r[7] = cmd_id
    crc = 0
    for b in r[2:88]:
        crc ^= b
    r[88] = crc
    return bytes(r)


def query(dev, tid: int, cmd_class: int, cmd_id: int):
    """命令を送って応答を返す。戻り値 (status, response or None)"""
    dev.send_feature_report(b"\x00" + build_report(tid, cmd_class, cmd_id))  # 先頭はレポートID 0
    status = None
    for _ in range(10):
        time.sleep(0.06)
        resp = bytes(dev.get_feature_report(0x00, REPORT_LEN + 1))
        if len(resp) == REPORT_LEN + 1:  # Windows ではレポートIDが先頭に付く
            resp = resp[1:]
        if len(resp) < REPORT_LEN:
            return None, None
        status = resp[0]
        if status == STATUS_BUSY:
            continue
        if status == STATUS_OK and resp[1] == tid and resp[6] == cmd_class and resp[7] == cmd_id:
            return status, resp
        return status, None
    return status, None


class RazerMouse:
    """マウスを探して開き、残量を読む。失敗したら次回また探し直す"""

    def __init__(self):
        self.dev = None
        self.tid = None
        self.name = "Razer マウス"

    def close(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None

    @staticmethod
    def _candidates():
        devs = hid.enumerate(RAZER_VID, 0)
        known = [d for d in devs if d["product_id"] in KNOWN_PIDS]
        others = [d for d in devs if d["product_id"] not in KNOWN_PIDS]
        return known + others

    def connect(self) -> bool:
        self.close()
        for info in self._candidates():
            dev = hid.device()
            try:
                dev.open_path(info["path"])
            except Exception:
                continue
            for tid in TRANSACTION_IDS:
                try:
                    _, resp = query(dev, tid, CMD_CLASS_POWER, CMD_BATTERY)
                except Exception:
                    break
                if resp is not None:
                    self.dev, self.tid = dev, tid
                    pid = info["product_id"]
                    self.name = KNOWN_PIDS.get(pid) or info.get("product_string") or f"Razer 0x{pid:04X}"
                    log.info("接続: %s PID=0x%04X if=%s usage=0x%04X/0x%04X tid=0x%02X",
                             self.name, pid, info.get("interface_number"),
                             info.get("usage_page", 0), info.get("usage", 0), tid)
                    return True
            try:
                dev.close()
            except Exception:
                pass
        return False

    def read(self):
        """(残量% 整数, 充電中か, 残量% 小数) を返す。読めなければ None"""
        if self.dev is None and not self.connect():
            return None
        try:
            _, resp = query(self.dev, self.tid, CMD_CLASS_POWER, CMD_BATTERY)
            if resp is None:
                raise IOError("no battery response")
            raw = resp[9]
            _, cresp = query(self.dev, self.tid, CMD_CLASS_POWER, CMD_CHARGING)
            charging = bool(cresp is not None and cresp[9] == 1)
        except Exception as e:
            log.debug("読み取り失敗: %s", e)
            self.close()  # 抜き差し・スリープ復帰に備えて次回探し直す
            return None
        if raw == 0 and not charging:
            return None  # マウスの電源オフ/スリープ中はドングルが 0 を返すことがある
        level = raw / 255 * 100
        return round(level), charging, level


# ---------------------------------------------------------------- 履歴と残り時間の予測
class BatteryHistory:
    """残量の記録（history.csv）と、減り方・増え方からの時間予測。

    PC の電源オフやマウスのスリープで記録が途切れた区間は計算に入れないので、
    予測は「アプリが動いていて、マウスが起きている時間」ベースの使用時間になります。
    """
    MAX_AGE = 7 * 24 * 3600        # 7日より古い記録は捨てる
    GAP_MAX = POLL_SECONDS * 5     # これ以上記録があいたら別区間として扱う
    # 予測に必要な合計時間（秒）と合計変化量（%）。放電はゆっくりなので長めに見る
    MIN_SPAN = {False: 60 * 60, True: 15 * 60}
    MIN_DELTA = {False: 1.5, True: 3.0}
    FULL_LEVEL = 99.5              # 充電中、これ以上は満充電後の維持充電とみなして除外

    def __init__(self, path: Path | None = None):
        self.path = path
        self.samples = deque()     # (時刻, 残量%, 充電中)
        self._appends = 0
        self._load()

    def _load(self):
        if not self.path:
            return
        lines = 0
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    lines += 1
                    parts = line.strip().split(",")
                    if len(parts) != 3:
                        continue
                    try:
                        self.samples.append((float(parts[0]), float(parts[1]), parts[2] == "1"))
                    except ValueError:
                        continue
        except FileNotFoundError:
            return
        except Exception:
            log.warning("履歴を読めませんでした", exc_info=True)
            return
        self._trim(time.time())
        if lines - len(self.samples) > 1000:  # 古い行がたまっていたら書き直して小さくする
            self._rewrite()

    def _trim(self, now: float):
        while self.samples and now - self.samples[0][0] > self.MAX_AGE:
            self.samples.popleft()

    def _rewrite(self):
        self._appends = 0
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for ts, level, ch in self.samples:
                    f.write(f"{ts:.0f},{level:.2f},{int(ch)}\n")
            os.replace(tmp, self.path)
        except Exception:
            log.warning("履歴を整理できませんでした", exc_info=True)

    def add(self, level: float, charging: bool, ts: float | None = None):
        ts = time.time() if ts is None else ts
        self.samples.append((ts, level, charging))
        self._trim(ts)
        if not self.path:
            return
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"{ts:.0f},{level:.2f},{int(charging)}\n")
            self._appends += 1
            if self._appends >= 1000:
                self._rewrite()
        except Exception:
            log.warning("履歴を保存できませんでした", exc_info=True)

    def rate(self, charging: bool):
        """変化の速さ（%/時、正の値）。データ不足なら None"""
        total_dt = total_delta = 0.0
        start = prev = None

        def close_segment():
            nonlocal total_dt, total_delta
            if start is not None and prev is not None and prev[0] > start[0]:
                total_dt += prev[0] - start[0]
                total_delta += (prev[1] - start[1]) if charging else (start[1] - prev[1])

        for s in self.samples:
            excluded = s[2] != charging or (charging and s[1] >= self.FULL_LEVEL)
            gap = prev is not None and s[0] - prev[0] > self.GAP_MAX
            if excluded or gap:
                close_segment()
                start = prev = None
                if excluded:
                    continue
            if start is None:
                start = s
            prev = s
        close_segment()

        if total_dt < self.MIN_SPAN[charging] or total_delta < self.MIN_DELTA[charging]:
            return None
        return total_delta / (total_dt / 3600)

    def estimate_hours(self, level: float, charging: bool):
        """放電中は空まで、充電中は満充電までの時間（時間）。予測できなければ None"""
        r = self.rate(charging)
        if not r:
            return None
        return ((100 - level) if charging else level) / r


def format_hours(h: float) -> str:
    if h < 1:
        return f"約{max(1, round(h * 60))}分"
    if h < 10:
        return f"約{h:.1f}時間"
    if h >= 200:
        return "200時間以上"
    return f"約{round(h)}時間"


def describe_time(history: BatteryHistory, pct: int, charging: bool, level: float) -> str:
    if charging and pct >= 100:
        return "充電完了"
    label = "満充電まで" if charging else "残り使用時間"
    h = history.estimate_hours(level, charging)
    return f"{label}: {format_hours(h)}" if h is not None else f"{label}: 計測中"


# ---------------------------------------------------------------- 通知の判定
class Notifier:
    """残量低下と充電完了の通知を、同じ内容で何度も出さないように管理する"""

    def __init__(self):
        self.notified = set()       # 通知済みのしきい値
        self.full_notified = False
        self.first = True

    def check(self, pct: int, charging: bool):
        """出すべき通知 (タイトル, 本文) のリストを返す"""
        first, self.first = self.first, False
        out = []
        if charging:
            self.notified.clear()
            if pct >= 100 and not self.full_notified:
                self.full_notified = True
                if not first:  # 起動した時点ですでに満充電なら通知しない
                    out.append(("充電完了", "マウスの充電が完了しました。ケーブルを外して大丈夫です。"))
            return out

        self.full_notified = False
        # 充電せずに少し戻った程度では再通知しない（しきい値付近でのチラつき防止）
        self.notified = {t for t in self.notified if pct <= t + NOTIFY_RESET_MARGIN}
        crossed = [t for t in NOTIFY_LEVELS if pct <= t and t not in self.notified]
        if crossed:
            t = min(crossed)
            self.notified.update(x for x in NOTIFY_LEVELS if pct <= x)
            body = ("まもなくバッテリーが切れます。充電してください。" if t <= min(NOTIFY_LEVELS)
                    else f"残量が{t}%以下になりました。そろそろ充電してください。")
            out.append((f"マウスの残量 {pct}%", body))
        return out


# ---------------------------------------------------------------- 設定・ログ
def load_settings() -> dict:
    settings = {"notify": True}
    try:
        settings.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception:
        log.warning("settings.json を読めませんでした。既定値で起動します", exc_info=True)
    return settings


def save_settings(settings: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        log.warning("settings.json を保存できませんでした", exc_info=True)


def setup_logging():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    # pythonw ではエラーが画面に出ないので、拾えなかった例外もログに残す
    sys.excepthook = lambda t, v, tb: log.critical("未処理の例外", exc_info=(t, v, tb))
    threading.excepthook = lambda a: log.critical(
        "スレッドで未処理の例外", exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


# ---------------------------------------------------------------- 自動起動（レジストリの Run キー）
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command() -> str:
    """自動起動用のコマンド。pyenv の中継 .bat ではなく本物の pythonw.exe を指す"""
    if getattr(sys, "frozen", False):  # PyInstaller で exe 化した場合
        return f'"{sys.executable}" --autostart'
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").exists():
        exe = exe.with_name("pythonw.exe")
    return f'"{exe}" "{Path(__file__).resolve()}" --autostart'


def get_autostart():
    """登録されているコマンド。未登録や Windows 以外なら None"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, APP_NAME)[0]
    except (ImportError, OSError):
        return None


def set_autostart(enable: bool):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- アイコン描画
GREEN, AMBER, RED, GRAY = (74, 222, 128), (251, 191, 36), (248, 113, 113), (140, 140, 140)


def _taskbar_is_light() -> bool:
    """Windows のタスクバーがライトテーマなら True（輪郭色の切り替えに使う）"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        return winreg.QueryValueEx(key, "SystemUsesLightTheme")[0] == 1
    except Exception:
        return False


def make_icon(pct, charging) -> Image.Image:
    """マウス型アイコン。中の水位が残量。256px で描いて 64px に縮小し、輪郭をなめらかにする"""
    big = 256
    fg = (40, 40, 40) if _taskbar_is_light() else (255, 255, 255)
    if pct is None:
        fill, outline = None, GRAY
    elif pct <= LOW_THRESHOLD:
        fill, outline = RED, RED  # 少ないときは輪郭も赤くして気づきやすく
    elif pct <= MID_THRESHOLD:
        fill, outline = AMBER, fg
    else:
        fill, outline = GREEN, fg

    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    body = (34, 2, 222, 254)
    radius = 88
    if fill is not None:
        mask = Image.new("L", (big, big), 0)
        ImageDraw.Draw(mask).rounded_rectangle(body, radius, fill=255)
        level = Image.new("RGBA", (big, big), (0, 0, 0, 0))
        top = body[3] - (body[3] - body[1]) * pct / 100
        ImageDraw.Draw(level).rectangle((0, top, big, big), fill=fill)
        img.paste(level, (0, 0), mask)

    d = ImageDraw.Draw(img)
    d.rounded_rectangle(body, radius, outline=outline, width=18)
    d.line((128, 10, 128, 96), fill=outline, width=12)            # 左右ボタンの境目
    d.rounded_rectangle((113, 42, 143, 98), 15, fill=outline)     # ホイール
    if charging:  # 充電中は稲妻マーク
        x, y, s = 90, 128, 80
        d.polygon([(x + 0.55 * s, y), (x + 0.1 * s, y + 0.55 * s), (x + 0.45 * s, y + 0.55 * s),
                   (x + 0.35 * s, y + s), (x + 0.9 * s, y + 0.4 * s), (x + 0.55 * s, y + 0.4 * s)],
                  fill=(30, 30, 30))
    return img.resize((64, 64), Image.LANCZOS)


# ---------------------------------------------------------------- 二重起動防止
MUTEX_NAME = "Local\\RazerBatteryTray"
ERROR_ALREADY_EXISTS = 183
_mutex_handle = None  # プロセスが終わるまで保持する（解放されるとロックが外れる）


def acquire_single_instance() -> bool:
    """Windows の名前付きミューテックスで二重起動を防ぐ。すでに起動中なら False"""
    global _mutex_handle
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except (ImportError, AttributeError, OSError):
        return True  # Windows 以外ではチェックしない
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def notify_already_running():
    """pythonw ではコンソールがないので、メッセージボックスで知らせる"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, "Razer バッテリー表示はすでに起動しています。\nタスクトレイを確認してください。",
            "Razer Battery Tray", 0x40)  # 0x40 = 情報アイコン
    except Exception:
        print("すでに起動しています。")


# ---------------------------------------------------------------- トレイ常駐
def run_tray(autostart: bool = False):
    setup_logging()
    if not acquire_single_instance():
        log.info("すでに起動中のため終了")
        if not autostart:  # PC 起動時の自動起動ならメッセージは出さない
            notify_already_running()
        return
    log.info("起動 v%s (Python %s, %s)", __version__, platform.python_version(), sys.executable)
    import pystray  # 診断モードでは不要なのでここで読み込む

    settings = load_settings()
    history = BatteryHistory(HISTORY_FILE)
    notifier = Notifier()
    mouse = RazerMouse()
    wake, stop = threading.Event(), threading.Event()
    state = {"status": "取得中…", "ok": None}

    # フォルダを移動していたら、自動起動の登録パスを今の場所に合わせる
    registered = get_autostart()
    if registered and registered != startup_command():
        try:
            set_autostart(True)
            log.info("自動起動の登録を更新: %s", startup_command())
        except Exception:
            log.warning("自動起動の登録を更新できませんでした", exc_info=True)

    icon = pystray.Icon(APP_NAME, make_icon(None, False), "Razer バッテリー: 取得中…")

    def update():
        reading = mouse.read()
        now = datetime.now().strftime("%H:%M")
        if reading is None:
            if state["ok"] is not False:
                log.info("マウスが応答しません（未接続またはスリープ中）")
            state["ok"] = False
            state["status"] = "マウス未検出 / スリープ中"
            icon.icon = make_icon(None, False)
            icon.title = f"Razer マウスが見つからないか、スリープ中です（{now}）"
        else:
            pct, charging, level = reading
            if state["ok"] is not True:
                log.info("読み取り: %s %d%%%s", mouse.name, pct, "（充電中）" if charging else "")
            state["ok"] = True
            history.add(level, charging)
            time_text = describe_time(history, pct, charging, level)
            state["status"] = f"{pct}%{'（充電中）' if charging else ''} ・ {time_text}"
            icon.icon = make_icon(pct, charging)
            icon.title = (f"{mouse.name}: {pct}%{'（充電中）' if charging else ''}\n"
                          f"{time_text}\n更新 {now}")[:127]
            for title, body in notifier.check(pct, charging):  # 通知オフでも判定は進める
                if settings.get("notify", True):
                    log.info("通知: %s / %s", title, body)
                    try:
                        icon.notify(body, title)
                    except Exception:
                        log.warning("通知を出せませんでした", exc_info=True)
        icon.update_menu()

    def loop():
        while not stop.is_set():
            try:
                update()
            except Exception as e:  # 常駐が止まらないように握りつぶしてログに残す
                log.exception("更新中のエラー")
                icon.title = f"エラー: {e}"[:120]
            wake.wait(POLL_SECONDS)
            wake.clear()

    def on_refresh(_icon, _item):
        wake.set()

    def on_toggle_notify(_icon, _item):
        settings["notify"] = not settings.get("notify", True)
        save_settings(settings)
        log.info("通知: %s", "オン" if settings["notify"] else "オフ")

    def on_toggle_autostart(_icon, _item):
        enable = get_autostart() is None
        try:
            set_autostart(enable)
            log.info("自動起動: %s (%s)", "オン" if enable else "オフ", startup_command())
        except Exception:
            log.exception("自動起動の設定を変更できませんでした")

    def on_open_folder(_icon, _item):
        os.startfile(DATA_DIR)

    def on_quit(_icon, _item):
        log.info("終了")
        stop.set()
        wake.set()
        mouse.close()
        icon.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem(lambda _item: state["status"], lambda _icon, _item: None, enabled=False),
        pystray.MenuItem("今すぐ更新", on_refresh, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("通知を表示", on_toggle_notify, checked=lambda _item: settings.get("notify", True)),
        pystray.MenuItem("Windows 起動時に自動で起動", on_toggle_autostart,
                         checked=lambda _item: get_autostart() is not None),
        pystray.MenuItem("ログ・履歴のフォルダを開く", on_open_folder),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("終了", on_quit),
    )

    def setup(ic):
        ic.visible = True
        threading.Thread(target=loop, daemon=True).start()

    icon.run(setup=setup)


# ---------------------------------------------------------------- 診断
def run_diag():
    print(f"Razer Battery Tray v{__version__}  /  データの保存先: {DATA_DIR}")
    print(f"自動起動: {get_autostart() or '未登録'}\n")
    devs = hid.enumerate(RAZER_VID, 0)
    if not devs:
        print("Razer 機器が見つかりません。ドングル（またはケーブル）が刺さっているか確認してください。")
        return
    print(f"Razer の HID インターフェースが {len(devs)} 個見つかりました:\n")
    for info in devs:
        pid = info["product_id"]
        label = KNOWN_PIDS.get(pid, info.get("product_string") or "?")
        print(f"- PID=0x{pid:04X} if={info.get('interface_number')} "
              f"usage_page=0x{info.get('usage_page', 0):04X} usage=0x{info.get('usage', 0):04X}  {label}")
        dev = hid.device()
        try:
            dev.open_path(info["path"])
        except Exception as e:
            print(f"    開けません: {e}")
            continue
        for tid in TRANSACTION_IDS:
            try:
                status, resp = query(dev, tid, CMD_CLASS_POWER, CMD_BATTERY)
            except Exception as e:
                print(f"    tid=0x{tid:02X}: 送受信エラー {e}")
                break
            s = STATUS_NAMES.get(status, status)
            if resp is not None:
                print(f"    tid=0x{tid:02X}: status={s}  残量 raw={resp[9]} → {round(resp[9] / 255 * 100)}%")
            else:
                print(f"    tid=0x{tid:02X}: status={s}")
        dev.close()

    reading = RazerMouse().read()
    print("\n本番と同じ読み取り:", reading[:2] if reading else "読めませんでした")
    if reading:
        history = BatteryHistory(HISTORY_FILE)
        print(f"履歴: {len(history.samples)} 件  /  {describe_time(history, reading[0], reading[1], reading[2])}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Razer マウスのバッテリー残量をタスクトレイに表示")
    p.add_argument("--diag", action="store_true", help="接続中の機器と読み取り結果を表示して終了")
    p.add_argument("--autostart", action="store_true", help=argparse.SUPPRESS)  # 自動起動時に付く
    args = p.parse_args()
    run_diag() if args.diag else run_tray(autostart=args.autostart)
