"""Package public Android metadata + PRIVATE Square Cloud server, without editing .env."""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

from package_release import ROOT, _atomic_zip, _verify_env_is_shell_safe, _verify_netscape_cookies


def main():
    gradle = (ROOT / "android/app/build.gradle.kts").read_text(encoding="utf-8")
    version = re.search(r'versionName = "([^"]+)"', gradle).group(1)
    code = int(re.search(r'versionCode = (\d+)', gradle).group(1))
    dist = ROOT / "android/dist"
    env = ROOT / "backend/.env"
    env_digest = hashlib.sha256(env.read_bytes()).hexdigest()
    changes = [
        "Downloads com retomada, cancelamento e mensagens de erro por etapa",
        "Extrator e dependências embarcados com integridade verificada",
        "Recentes na tela inicial, compartilhamento e nova tentativa",
        "Atualizações versionadas com avisos persistentes sem repetição",
    ]
    metadata = {"android_latest_version": version, "android_latest_version_code": code,
                "android_release_id": f"android-{version}-{code}", "android_update_policy": "recommended",
                "android_changelog_json": json.dumps(changes, ensure_ascii=False)}
    assets = []
    for suffix, name in [("", "Slucss-System.apk"), ("_arm64", "Slucss-System-arm64-v8a.apk"),
                         ("_arm32", "Slucss-System-armeabi-v7a.apk")]:
        path = dist / name
        with path.open("rb") as f:
            digest = hashlib.file_digest(f, "sha256").hexdigest()
        metadata[f"android_download_url{suffix}"] = (
            f"https://github.com/XardAly/slucssTube-releases-Tube/releases/download/android-v{version}/{name}"
        )
        metadata[f"android_download_sha256{suffix}"] = digest
        assets.append({"name": name, "sha256": digest, "bytes": path.stat().st_size})
    (dist / f"android-release-{version}.json").write_text(
        json.dumps({"version": version, "version_code": code, "assets": assets, "changelog": changes},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (dist / "SHA256SUMS.txt").write_text("".join(f"{a['sha256']}  {a['name']}\n" for a in assets), encoding="ascii")

    def write(zf):
        count = 0
        for path in sorted((ROOT / "backend").rglob("*")):
            rel = path.relative_to(ROOT / "backend")
            if (not path.is_file() or path.is_symlink() or
                    any(part.startswith(".") or part in {"tests", "__pycache__", "nginx", "releases"} for part in rel.parts) or
                    path.name.startswith("_dev_") or
                    (path.suffix not in {".py", ".sql"} and rel.as_posix() != "requirements.txt")):
                continue
            zf.write(path, rel.as_posix()); count += 1
        for path, name in [(env, ".env"), (ROOT / "cookies.txt", "cookies.txt"),
                           (ROOT / "deploy/backend/squarecloud.app", "squarecloud.app")]:
            zf.write(path, name); count += 1
        zf.writestr("android-release.json", json.dumps(metadata, ensure_ascii=False, indent=2))
        return count + 1

    output = ROOT / f"square-backend-mobile-v1-{version}.PRIVATE.zip"
    count, size = _atomic_zip(output, write)
    _verify_env_is_shell_safe(output)
    _verify_netscape_cookies(output)
    with zipfile.ZipFile(output) as zf:
        assert hashlib.sha256(zf.read(".env")).hexdigest() == env_digest
        assert len(zf.namelist()) == len(set(zf.namelist()))
        assert {"api/main.py", "main.py", "requirements.txt", "notifications/releases.py", "android-release.json"} <= set(zf.namelist())
    assert hashlib.sha256(env.read_bytes()).hexdigest() == env_digest
    print(f"PRIVATE bundle: {output.name}; {count} files; {size} bytes; original .env preserved byte for byte.")


if __name__ == "__main__":
    main()
