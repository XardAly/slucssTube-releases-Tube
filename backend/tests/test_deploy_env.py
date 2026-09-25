import os

from backend import main


def test_packaged_env_unquotes_values_preserves_secrets_and_respects_panel(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "ANDROID_LATEST_VERSION_CODE='22'\n"
        "ANDROID_DOWNLOAD_URL='https://example.com/app.apk'\n"
        "ANDROID_CHANGELOG_JSON='[\"Nome e animações\"]'\n"
        "TEST_DEPLOY_LITERAL='${KEEP_LITERAL}'\n"
        "TEST_DEPLOY_PANEL='file'\n",
        encoding="utf-8",
    )
    for key in ("ANDROID_LATEST_VERSION_CODE", "ANDROID_DOWNLOAD_URL", "ANDROID_CHANGELOG_JSON", "TEST_DEPLOY_LITERAL"):
        # Register even absent keys so load_dotenv's writes are undone afterwards.
        monkeypatch.setenv(key, "test-placeholder")
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TEST_DEPLOY_PANEL", "panel")
    main._load_env()
    assert os.environ["ANDROID_LATEST_VERSION_CODE"] == "22"
    assert os.environ["ANDROID_DOWNLOAD_URL"] == "https://example.com/app.apk"
    assert os.environ["ANDROID_CHANGELOG_JSON"] == '["Nome e animações"]'
    assert os.environ["TEST_DEPLOY_LITERAL"] == "${KEEP_LITERAL}"
    assert os.environ["TEST_DEPLOY_PANEL"] == "panel"
