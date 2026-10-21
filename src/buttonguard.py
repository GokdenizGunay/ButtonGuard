#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gökdeniz G.
"""ButtonGuard: güç düğmesinin sistemi kapatmasını engelleyen tek anahtarlı uygulama.

İki katmanlı çalışır, ikisi de root gerektirmez:
  1. systemd-logind "handle-power-key" engelleme kilidi (Inhibit, mode=block).
     Etkin oturumdaki kullanıcıya polkit varsayılan olarak izin verir.
  2. Masaüstü ortamının kendi güç düğmesi ayarı (GNOME, Cinnamon, MATE, XFCE).
     Bu ortamlar düğmeyi logind yerine kendileri işlediği için ayar "hiçbir şey" yapılır,
     önceki değer saklanır ve koruma kapanınca geri yüklenir.
"""

import json
import os
import shutil
import signal
import subprocess
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

Indicator = None
for _name in ("AyatanaAppIndicator3", "AppIndicator3"):
    try:
        gi.require_version(_name, "0.1")
        Indicator = __import__("gi.repository." + _name, fromlist=[_name])
        break
    except (ValueError, ImportError):
        continue

VERSION = "1.0.0"
APP_ID = "io.github.buttonguard"
ICON_ON = APP_ID
ICON_OFF = APP_ID + "-off"

CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "buttonguard")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# (şema, anahtar, "hiçbir şey" değeri)
GSETTINGS_KEYS = [
    ("org.gnome.settings-daemon.plugins.power", "power-button-action", "nothing"),
    ("org.cinnamon.settings-daemon.plugins.power", "button-power", "nothing"),
    ("org.mate.power-manager", "button-power", "nothing"),
]
XFCONF_CHANNEL = "xfce4-power-manager"
XFCONF_PROP = "/xfce4-power-manager/power-button-action"


# --------------------------------------------------------------------- ayarlar

def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {"enabled": bool(data.get("enabled", False)),
                    "saved": dict(data.get("saved") or {}),
                    "shortcut": bool(data.get("shortcut", False))}
    except (OSError, ValueError):
        pass
    return {"enabled": False, "saved": {}, "shortcut": False}


def ensure_desktop_shortcut(cfg):
    """Masaüstü kısayolunu kullanıcı başına bir kez ekler (sonradan açılan hesaplar için).

    Kullanıcı kısayolu silerse yeniden eklenmez. Sonuç değiştiyse True döner.
    """
    if cfg["shortcut"]:
        return False
    desk = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DESKTOP)
    src = "/usr/share/applications/%s.desktop" % APP_ID
    if not desk or desk == GLib.get_home_dir() or not os.path.isdir(desk) \
            or not os.path.exists(src):
        return False
    dst = os.path.join(desk, APP_ID + ".desktop")
    try:
        if not os.path.exists(dst):
            shutil.copyfile(src, dst)
        os.chmod(dst, 0o755)
        # GNOME masaüstü simgeleri (DING) başlatıcıyı ancak güvenilir işaretliyse çalıştırır.
        Gio.File.new_for_path(dst).set_attribute_string(
            "metadata::trusted", "true", Gio.FileQueryInfoFlags.NONE, None)
    except (OSError, GLib.Error):
        pass
    cfg["shortcut"] = True
    return True


def save_config(cfg):
    os.makedirs(CONFIG_DIR, mode=0o700, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


# --------------------------------------------------------------------- logind

class PowerKeyInhibitor:
    """logind handle-power-key kilidi; dosya tanıtıcısı açık kaldıkça geçerlidir."""

    def __init__(self):
        self.fd = None

    @property
    def active(self):
        return self.fd is not None

    def acquire(self):
        if self.fd is not None:
            return
        bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        result, fds = bus.call_with_unix_fd_list_sync(
            "org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager",
            "Inhibit",
            GLib.Variant("(ssss)", ("handle-power-key", "ButtonGuard",
                                    "Güç düğmesi koruması açık", "block")),
            GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE, -1, None, None)
        self.fd = fds.get(result.unpack()[0])

    def release(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


# --------------------------------------------------------------------- masaüstü

class DesktopPowerSetting:
    """Masaüstü ortamlarının güç düğmesi eylemini 'hiçbir şey' yapar ve geri yükler.

    cfg["saved"] içinde özgün değerler tutulur: anahtar -> kullanıcı değeri
    (None ise varsayılan kullanılıyordu, geri yüklerken sıfırlanır).
    """

    def __init__(self, cfg, on_saved_changed):
        self.cfg = cfg
        self.on_saved_changed = on_saved_changed
        self.enforcing = False
        self.settings = []  # (Gio.Settings, anahtar, değer, sinyal kimliği)
        source = Gio.SettingsSchemaSource.get_default()
        for schema_id, key, value in GSETTINGS_KEYS:
            schema = source.lookup(schema_id, True) if source else None
            if schema is None or not schema.has_key(key):
                continue
            s = Gio.Settings.new(schema_id)
            hid = s.connect("changed::" + key, self._on_changed)
            self.settings.append((s, key, value, hid))
        self.xfconf = shutil.which("xfconf-query")

    @property
    def available(self):
        return bool(self.settings) or self._xfce_running()

    def _xfce_running(self):
        return bool(self.xfconf) and "XFCE" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper()

    def apply(self):
        saved = self.cfg["saved"]
        for s, key, value, _ in self.settings:
            sid = "gsettings:%s:%s" % (s.props.schema_id, key)
            if sid not in saved:
                user = s.get_user_value(key)
                saved[sid] = s.get_string(key) if user is not None else None
                self.on_saved_changed()
            if s.get_string(key) != value:
                s.set_string(key, value)
        if self._xfce_running():
            sid = "xfconf:%s:%s" % (XFCONF_CHANNEL, XFCONF_PROP)
            if sid not in saved:
                saved[sid] = self._xfconf_get()
                self.on_saved_changed()
            self._xfconf_set("0")
        Gio.Settings.sync()
        self.enforcing = True

    def restore(self):
        self.enforcing = False
        saved = self.cfg["saved"]
        for s, key, _, _ in self.settings:
            sid = "gsettings:%s:%s" % (s.props.schema_id, key)
            if sid not in saved:
                continue
            old = saved.pop(sid)
            if old is None:
                s.reset(key)
            else:
                s.set_string(key, old)
        sid = "xfconf:%s:%s" % (XFCONF_CHANNEL, XFCONF_PROP)
        if sid in saved:
            old = saved.pop(sid)
            if self.xfconf:
                if old is None:
                    self._xfconf_run("-r")
                else:
                    self._xfconf_set(old)
        # Bu oturumda bulunmayan ortamlara ait kayıtlar ileride geri yüklenmek üzere kalır.
        Gio.Settings.sync()
        self.on_saved_changed()

    def _on_changed(self, settings, key):
        # Koruma açıkken ayar başka yerden değiştirilirse yeniden uygula.
        if not self.enforcing:
            return
        for s, k, value, _ in self.settings:
            if s is settings and k == key and s.get_string(key) != value:
                GLib.idle_add(self._reenforce, s, key, value)

    @staticmethod
    def _reenforce(s, key, value):
        s.set_string(key, value)
        return False

    def _xfconf_run(self, *args):
        try:
            return subprocess.run(
                [self.xfconf, "-c", XFCONF_CHANNEL, "-p", XFCONF_PROP, *args],
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return None

    def _xfconf_get(self):
        r = self._xfconf_run()
        return r.stdout.strip() if r is not None and r.returncode == 0 else None

    def _xfconf_set(self, value):
        self._xfconf_run("-n", "-t", "uint", "-s", str(value))


# --------------------------------------------------------------------- uygulama

class ButtonGuardApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.cfg = None
        self.window = None
        self.indicator = None
        self.status_icon = None
        self.tray_toggle = None
        self.syncing = False
        self.inhibitor = PowerKeyInhibitor()
        self.desktop = None
        self.error = None

    # -- yaşam döngüsü

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self.hold()  # pencere kapalıyken de çalışmaya devam et
        self.cfg = load_config()
        if ensure_desktop_shortcut(self.cfg):
            self._save()
        self.desktop = DesktopPowerSetting(self.cfg, self._save)
        self._build_window()
        self._build_tray()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._on_signal)
        if self.cfg["enabled"]:
            self._set_guard(True, persist=False)
        elif self.cfg["saved"]:
            # Önceki çalışma beklenmedik biçimde sonlandıysa ayarları geri getir.
            self.desktop.restore()
        self._refresh()

    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if "--quit" in args:
            self.quit_app()
        elif "--hidden" not in args or self.tray_missing():
            self.show_window()
        return 0

    def do_shutdown(self):
        # Çıkışta korumayı bırak; ayarlarda "açık" kalır, sonraki açılışta yeniden uygulanır.
        self.inhibitor.release()
        if self.desktop is not None and self.desktop.enforcing:
            self.desktop.restore()
        Gtk.Application.do_shutdown(self)

    def _on_signal(self):
        self.quit_app()
        return GLib.SOURCE_REMOVE

    def quit_app(self, *_):
        self.quit()

    def _save(self):
        try:
            save_config(self.cfg)
        except OSError as e:
            print("ButtonGuard: ayarlar kaydedilemedi:", e, file=sys.stderr)

    # -- koruma

    def _set_guard(self, on, persist=True):
        error = None
        if on:
            try:
                self.inhibitor.acquire()
            except GLib.Error as e:
                error = e.message
            if self.desktop.available:
                self.desktop.apply()
            if not self.inhibitor.active and not self.desktop.enforcing:
                on = False
        else:
            self.inhibitor.release()
            if self.desktop.enforcing or self.cfg["saved"]:
                self.desktop.restore()
        if persist:
            self.cfg["enabled"] = on
            self._save()
        self.error = error
        self._refresh()

    @property
    def guard_on(self):
        return self.inhibitor.active or self.desktop.enforcing

    # -- pencere

    def _build_window(self):
        win = Gtk.ApplicationWindow(application=self, title="ButtonGuard")
        win.set_icon_name(ICON_ON)
        win.set_default_size(380, -1)
        win.set_resizable(False)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.connect("delete-event", self._on_delete)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_border_width(24)
        win.add(box)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.image = Gtk.Image()
        self.image.set_pixel_size(56)
        row.pack_start(self.image, False, False, 0)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label(xalign=0)
        title.set_markup("<b><big>Güç Düğmesi Koruması</big></b>")
        texts.pack_start(title, False, False, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_line_wrap(True)
        self.status.set_max_width_chars(30)
        texts.pack_start(self.status, False, False, 0)
        row.pack_start(texts, True, True, 0)

        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.switch.connect("notify::active", self._on_switch)
        row.pack_end(self.switch, False, False, 0)
        box.pack_start(row, False, False, 0)

        self.note = Gtk.Label(xalign=0)
        self.note.set_line_wrap(True)
        self.note.set_max_width_chars(44)
        self.note.get_style_context().add_class("dim-label")
        box.pack_start(self.note, False, False, 0)

        box.show_all()
        self.window = win

    def _on_delete(self, *_):
        if self.tray_missing():
            self.window.iconify()
        else:
            self.window.hide()
        return True

    def show_window(self, *_):
        self.window.show()
        self.window.present()

    def _on_switch(self, switch, _):
        if not self.syncing:
            self._set_guard(switch.get_active())

    # -- sistem tepsisi

    def _build_tray(self):
        menu = Gtk.Menu()
        show = Gtk.MenuItem(label="Pencereyi Aç")
        show.connect("activate", self.show_window)
        menu.append(show)
        self.tray_toggle = Gtk.CheckMenuItem(label="Koruma")
        self.tray_toggle.connect("toggled", self._on_tray_toggle)
        menu.append(self.tray_toggle)
        menu.append(Gtk.SeparatorMenuItem())
        quit_item = Gtk.MenuItem(label="Çık")
        quit_item.connect("activate", self.quit_app)
        menu.append(quit_item)
        menu.show_all()
        self.menu = menu

        if Indicator is not None:
            ind = Indicator.Indicator.new(
                "buttonguard", ICON_OFF, Indicator.IndicatorCategory.APPLICATION_STATUS)
            ind.set_status(Indicator.IndicatorStatus.ACTIVE)
            ind.set_title("ButtonGuard")
            ind.set_menu(menu)
            ind.set_secondary_activate_target(show)
            self.indicator = ind
        else:
            icon = Gtk.StatusIcon.new_from_icon_name(ICON_OFF)
            icon.set_title("ButtonGuard")
            icon.connect("activate", self.show_window)
            icon.connect("popup-menu", lambda i, b, t: menu.popup(
                None, None, Gtk.StatusIcon.position_menu, i, b, t))
            self.status_icon = icon

    def tray_missing(self):
        return self.indicator is None and (
            self.status_icon is None or not self.status_icon.is_embedded())

    def _on_tray_toggle(self, item):
        if not self.syncing:
            self._set_guard(item.get_active())

    # -- görünüm

    def _refresh(self):
        on = self.guard_on
        self.syncing = True
        self.switch.set_active(on)
        self.tray_toggle.set_active(on)
        self.syncing = False

        icon = ICON_ON if on else ICON_OFF
        self.image.set_from_icon_name(icon, Gtk.IconSize.DIALOG)
        self.window.set_icon_name(icon)
        desc = "Koruma açık" if on else "Koruma kapalı"
        if self.indicator is not None:
            self.indicator.set_icon_full(icon, desc)
        elif self.status_icon is not None:
            self.status_icon.set_from_icon_name(icon)
            self.status_icon.set_tooltip_text("ButtonGuard: " + desc.lower())

        if on:
            self.status.set_text("Açık: güç düğmesi sistemi kapatmaz.")
        else:
            self.status.set_text("Kapalı: güç düğmesi normal çalışır.")

        if self.error and on:
            note = "Uyarı: sistem kilidi alınamadı, yalnızca masaüstü ayarı değiştirildi."
        elif self.error:
            note = "Koruma açılamadı: " + self.error
        else:
            note = ("Pencereyi kapatınca uygulama sistem tepsisinde çalışmaya devam eder. "
                    "Tamamen kapatmak için tepsideki menüden Çık'ı seçin; "
                    "bu durumda koruma da kalkar.")
        self.note.set_text(note)


def main():
    GLib.set_prgname("buttonguard")
    app = ButtonGuardApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
