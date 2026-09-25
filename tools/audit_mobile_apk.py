"""Inspect distributed engines, certificates, models and ARM64 ELF alignment."""
from __future__ import annotations

import hashlib
import io
import json
import struct
import sys
import zipfile
from pathlib import Path


def elf_load_alignments(data: bytes) -> list[int]:
    if data[:4] != b"\x7fELF":
        return []
    if data[4] != 2 or data[5] != 1:
        return []  # 16 KB page-size requirement is for 64-bit devices.
    offset = struct.unpack_from("<Q", data, 32)[0]
    size, count = struct.unpack_from("<HH", data, 54)
    return [struct.unpack_from("<Q", data, offset + i * size + 48)[0]
            for i in range(count) if struct.unpack_from("<I", data, offset + i * size)[0] == 1]


def audit(path: Path):
    checks, alignment = [], []
    with zipfile.ZipFile(path) as apk:
        names = apk.namelist()
        abis = sorted({n.split('/')[1] for n in names if n.startswith('lib/')})
        assert abis and set(abis) <= {"arm64-v8a", "armeabi-v7a"}
        for abi in abis:
            for lib in ["libpython.so", "libffmpeg.so", "libqjs.so", "libpython.zip.so", "libffmpeg.zip.so", "libslucss_upscale.so"]:
                name = f"lib/{abi}/{lib}"
                assert name in names, name
                data = apk.read(name)
                checks.append(name)
                if lib.endswith('.zip.so'):
                    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
                        if lib == "libpython.zip.so":
                            assert bundle.read("usr/etc/tls/cert.pem").startswith(b"#") or b"BEGIN CERTIFICATE" in bundle.read("usr/etc/tls/cert.pem")
                        if abi == "arm64-v8a":
                            for nested in bundle.namelist():
                                if '.so' in nested:
                                    aligns = elf_load_alignments(bundle.read(nested))
                                    if aligns and min(aligns) < 16384:
                                        alignment.append(f"{name}:{nested}")
                elif abi == "arm64-v8a":
                    assert struct.unpack_from('<H', data, 18)[0] == 183, name
                    aligns = elf_load_alignments(data)
                    if aligns and min(aligns) < 16384:
                        alignment.append(name)
        expected = "1fa6733c37ea6fb51c99ad8fe785e7b7e5f3246c9b980230329d4fb72ed8d4d6"
        bundled = [n for n in names if apk.getinfo(n).file_size == 3072469 and hashlib.sha256(apk.read(n)).hexdigest() == expected]
        assert bundled, "Verified yt-dlp resource missing"
        with zipfile.ZipFile(io.BytesIO(apk.read(bundled[0]))) as ytdlp:
            assert "yt_dlp_ejs/yt/solver/core.min.js" in ytdlp.namelist()
        for name in ["assets/upscale/animevideov3/realesr-animevideov3-x2.bin", "assets/upscale/realcugan-se/up4x-conservative.bin"]:
            assert apk.getinfo(name).file_size > 0, name
    return {"apk": path.name, "abis": abis, "packaged_engines": checks,
            "bundled_ytdlp_sha256": expected, "arm64_4k_libraries": alignment,
            "native_execution_on_physical_device": "NOT TESTED"}


if __name__ == '__main__':
    print(json.dumps(audit(Path(sys.argv[1])), indent=2))
