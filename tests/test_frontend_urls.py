from core.config import Settings


def test_frontend_url_joins_paths_without_double_slashes():
    settings = Settings(FRONTEND_BASE_URL="https://example.com/connectome/")

    assert settings.frontend_url() == "https://example.com/connectome"
    assert settings.frontend_url("/auth/callback") == "https://example.com/connectome/auth/callback"
    assert settings.frontend_url("upgrade/success") == "https://example.com/connectome/upgrade/success"


def test_api_url_joins_paths_without_double_slashes():
    settings = Settings(API_BASE_URL="https://api.example.com/")

    assert settings.api_url() == "https://api.example.com"
    assert settings.api_url("/health") == "https://api.example.com/health"
    assert settings.api_url("api/screens/next") == "https://api.example.com/api/screens/next"


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


def test_backend_oauth_callbacks_default_from_api_base_url():
    settings = Settings(API_BASE_URL="https://api.example")

    assert settings.google_redirect_uri == "https://api.example/api/auth/google/callback"
    assert settings.github_redirect_uri == "https://api.example/api/auth/github/callback"


def test_backend_oauth_callbacks_can_be_overridden_independently():
    settings = Settings(
        API_BASE_URL="https://api.example",
        GOOGLE_REDIRECT_URI="https://auth.example/google/callback",
        GITHUB_REDIRECT_URI="https://auth.example/github/callback",
    )

    assert settings.google_redirect_uri == "https://auth.example/google/callback"
    assert settings.github_redirect_uri == "https://auth.example/github/callback"
