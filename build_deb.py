#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Gökdeniz G.
"""ButtonGuard için .deb paketi üretir (dpkg-deb gerektirmez, Windows'ta da çalışır).

Kullanım:  python build_deb.py   ->  dist/buttonguard_<sürüm>_all.deb
"""

import gzip
import hashlib
import io
import os
import re
import tarfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = "buttonguard"
APP_ID = "io.github.buttonguard"
MAINTAINER = "Gökdeniz G."
DEPENDS = "python3 (>= 3.9), python3-gi, gir1.2-gtk-3.0, systemd"
RECOMMENDS = "gir1.2-ayatanaappindicator3-0.1 | gir1.2-appindicator3-0.1"

LAUNCHER = b"#!/bin/sh\nexec /usr/bin/python3 /usr/share/buttonguard/buttonguard.py \"$@\"\n"


def read(rel):
    with open(os.path.join(ROOT, rel), "rb") as f:
        return f.read().replace(b"\r\n", b"\n")


def version():
    m = re.search(rb'^VERSION = "([^"]+)"', read("src/buttonguard.py"), re.M)
    return m.group(1).decode()


# (paketteki yol, içerik, mod)
def payload():
    return [
        ("usr/bin/buttonguard", LAUNCHER, 0o755),
        ("usr/share/buttonguard/buttonguard.py", read("src/buttonguard.py"), 0o644),
        (f"usr/share/applications/{APP_ID}.desktop", read(f"data/{APP_ID}.desktop"), 0o644),
        (f"usr/share/icons/hicolor/scalable/apps/{APP_ID}.svg", read(f"data/{APP_ID}.svg"), 0o644),
        (f"usr/share/icons/hicolor/scalable/apps/{APP_ID}-off.svg", read(f"data/{APP_ID}-off.svg"), 0o644),
        (f"etc/xdg/autostart/{APP_ID}.desktop", read(f"data/{APP_ID}-autostart.desktop"), 0o644),
        ("usr/share/doc/buttonguard/copyright", read("data/copyright"), 0o644),
    ]


def make_tar(entries, mtime):
    """entries: [(yol, içerik|None (dizin), mod)] -> tar.gz bayt dizisi"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for path, data, mode in entries:
            info = tarfile.TarInfo("./" + path if path else ".")
            info.mtime = mtime
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            info.mode = mode
            if data is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue(), 9, mtime=mtime)


def ar_member(name, data, mtime):
    header = (
        f"{name:<16}{mtime:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
    ).encode()
    assert len(header) == 60
    return header + data + (b"\n" if len(data) % 2 else b"")


def main():
    ver = version()
    mtime = int(time.time())
    files = payload()

    dirs = set()
    for path, _, _ in files:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    data_entries = [("", None, 0o755)] + [(d, None, 0o755) for d in sorted(dirs)]
    data_entries += files

    installed_kb = sum(len(d) for _, d, _ in files) // 1024 + 1
    control = (
        f"Package: {NAME}\n"
        f"Version: {ver}\n"
        "Architecture: all\n"
        f"Maintainer: {MAINTAINER}\n"
        f"Installed-Size: {installed_kb}\n"
        f"Depends: {DEPENDS}\n"
        f"Recommends: {RECOMMENDS}\n"
        "Section: utils\n"
        "Priority: optional\n"
        "Description: Güç düğmesinin sistemi kapatmasını engeller\n"
        " Tek anahtarla açılıp kapanan koruma: açıkken bilgisayarın güç düğmesine\n"
        " basmak sistemi kapatmaz. Oturum açılışında otomatik başlar ve sistem\n"
        " tepsisinde çalışır. Internet bağlantısı gerektirmez.\n"
    ).encode()
    md5sums = "".join(
        f"{hashlib.md5(d).hexdigest()}  {p}\n" for p, d, _ in files
    ).encode()

    control_tar = make_tar(
        [
            ("", None, 0o755),
            ("control", control, 0o644),
            ("md5sums", md5sums, 0o644),
            ("postinst", read("data/postinst"), 0o755),
            ("conffiles", f"/etc/xdg/autostart/{APP_ID}.desktop\n".encode(), 0o644),
            ("prerm", read("data/prerm"), 0o755),
            ("postrm", read("data/postrm"), 0o755),
        ],
        mtime,
    )
    data_tar = make_tar(data_entries, mtime)

    deb = b"!<arch>\n"
    deb += ar_member("debian-binary", b"2.0\n", mtime)
    deb += ar_member("control.tar.gz", control_tar, mtime)
    deb += ar_member("data.tar.gz", data_tar, mtime)

    out_dir = os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{NAME}_{ver}_all.deb")
    with open(out, "wb") as f:
        f.write(deb)
    print(f"{out}  ({len(deb) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
