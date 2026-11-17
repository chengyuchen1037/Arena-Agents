from app.storage.errors import StorageError
from app.storage.paths import get_data_dir


def clear_store_caches() -> None:
    """Clear data-dir singletons without changing crypto test key providers."""
    from app.providers.pricing import reset_price_store
    from app.providers.store import reset_provider_store
    from app.storage.sessions import reset_session_store
    from app.storage.settings import reset_settings_store

    reset_provider_store()
    reset_price_store()
    reset_session_store()
    reset_settings_store()


def reset_stores_for_tests() -> None:
    """Reset all process-local state after a test data directory changes."""
    from app.storage.crypto import reset_crypto_for_tests

    clear_store_caches()
    try:
        from app.storage.workspace import reset_workspace_for_tests
    except ModuleNotFoundError as exc:
        # Optional-workspace feature tests deliberately mask this exact module.
        if exc.name != "app.storage.workspace":
            raise
    else:
        reset_workspace_for_tests()
    reset_crypto_for_tests()


__all__ = [
    "StorageError",
    "clear_store_caches",
    "get_data_dir",
    "reset_stores_for_tests",
]
