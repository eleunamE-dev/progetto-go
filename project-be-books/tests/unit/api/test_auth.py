import hashlib

from bookreviews.api.auth import ApiKeys, key_digest, new_api_key


def test_the_digest_is_the_sha256_of_the_key() -> None:
    assert key_digest("test-key") == hashlib.sha256(b"test-key").hexdigest()


def test_new_keys_are_random_and_recognisable() -> None:
    keys = {new_api_key() for _ in range(100)}

    assert len(keys) == 100
    assert all(key.startswith("bkr_") and len(key) == 47 for key in keys)


def test_keys_identify_their_client() -> None:
    keys = ApiKeys({"web-app": [key_digest("web-key")], "batch": [key_digest("batch-key")]})

    assert keys.client_for("web-key") == "web-app"
    assert keys.client_for("batch-key") == "batch"
    assert keys.client_for("unknown-key") is None
    assert keys.client_for("") is None
    assert keys.client_for("web-key ") is None


def test_a_client_may_have_several_keys() -> None:
    keys = ApiKeys({"web-app": [key_digest("old-key"), key_digest("new-key")]})

    assert keys.client_for("old-key") == keys.client_for("new-key") == "web-app"


def test_without_keys_nobody_is_recognised() -> None:
    assert ApiKeys({}).client_for("anything") is None
