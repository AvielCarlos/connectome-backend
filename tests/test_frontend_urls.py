from core.config import Settings


def test_frontend_url_joins_paths_without_double_slashes():
    settings = Settings(FRONTEND_BASE_URL="https://example.com/connectome/")

    assert settings.frontend_url() == "https://example.com/connectome"
    assert settings.frontend_url("/auth/callback") == "https://example.com/connectome/auth/callback"
    assert settings.frontend_url("upgrade/success") == "https://example.com/connectome/upgrade/success"


def test_oauth_frontend_callbacks_default_from_base_url():
    settings = Settings(FRONTEND_BASE_URL="https://app.example")

    assert settings.google_frontend_callback_url == "https://app.example/auth/callback"
    assert settings.github_frontend_callback_url == "https://app.example/auth/github-callback"


def test_oauth_frontend_callbacks_can_be_overridden_independently():
    settings = Settings(
        FRONTEND_BASE_URL="https://app.example",
        GOOGLE_FRONTEND_CALLBACK_URL="https://auth.example/google-done",
        GITHUB_FRONTEND_CALLBACK_URL="https://auth.example/github-done",
    )

    assert settings.google_frontend_callback_url == "https://auth.example/google-done"
    assert settings.github_frontend_callback_url == "https://auth.example/github-done"
