import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.adapters import keycloak_identity
from app.adapters.keycloak_identity import KeycloakIdentity
from app.ports.identity import InvalidToken

ISSUER = "https://identity.example.invalid"
AUDIENCE = "synthetic-api"


@pytest.fixture(scope="module")
def signing_keys():
    return {kid: rsa.generate_private_key(public_exponent=65537, key_size=2048) for kid in ("old", "new")}


def _jwks(signing_keys, *kids):
    return {"keys": [{**jwt.algorithms.RSAAlgorithm.to_jwk(signing_keys[kid].public_key(), as_dict=True),
                      "kid": kid} for kid in kids]}


def _token(signing_keys, kid, *, signed_by="old"):
    now = int(time.time())
    return jwt.encode({"sub": "synthetic", "iss": ISSUER, "aud": AUDIENCE, "iat": now, "exp": now + 300},
                      signing_keys[signed_by], algorithm="RS256", headers={"kid": kid})


@pytest.fixture()
def ticks(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(keycloak_identity, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


def test_unknown_key_refresh_cooldown_bounds_distinct_kids(signing_keys, ticks):
    replies = [_jwks(signing_keys, "old"), _jwks(signing_keys, "old"), _jwks(signing_keys, "new")]
    calls = []

    def fetch():
        calls.append(None)
        return replies[min(len(calls) - 1, 2)]

    identity = KeycloakIdentity(ISSUER, AUDIENCE, jwks_provider=fetch)
    assert identity.verify_access_token(_token(signing_keys, "old")).subject == "synthetic"
    for i in range(10):
        with pytest.raises(InvalidToken, match="unknown_kid"):
            identity.verify_access_token(_token(signing_keys, f"unknown-{i}"))
    assert len(calls) == 2
    with pytest.raises(InvalidToken, match="unknown_kid"):
        identity.verify_access_token(_token(signing_keys, "new", signed_by="new"))
    ticks.now += 60
    assert identity.verify_access_token(_token(signing_keys, "new", signed_by="new")).subject == "synthetic"
    assert len(calls) == 3


def test_first_unknown_key_can_refresh_for_rotation(signing_keys, ticks):
    calls = []

    def fetch():
        calls.append(None)
        return _jwks(signing_keys, "old" if len(calls) == 1 else "new")

    identity = KeycloakIdentity(ISSUER, AUDIENCE, jwks_provider=fetch)
    assert identity.verify_access_token(_token(signing_keys, "old")).subject == "synthetic"
    assert identity.verify_access_token(_token(signing_keys, "new", signed_by="new")).subject == "synthetic"
    with pytest.raises(InvalidToken, match="unknown_kid"):
        identity.verify_access_token(_token(signing_keys, "old"))
    assert len(calls) == 2


def test_warm_known_key_does_not_wait_for_unknown_key_refresh(signing_keys, ticks):
    started, release = Event(), Event()
    jwks = _jwks(signing_keys, "old")
    calls = []

    def fetch():
        calls.append(None)
        if len(calls) > 1:
            started.set()
            assert release.wait(10)
        return jwks

    identity = KeycloakIdentity(ISSUER, AUDIENCE, jwks_provider=fetch)
    known_token = _token(signing_keys, "old")
    identity.verify_access_token(known_token)
    with ThreadPoolExecutor(max_workers=3) as pool:
        pending = pool.submit(identity.verify_access_token, _token(signing_keys, "unknown"))
        try:
            assert started.wait(5)
            known = pool.submit(identity.verify_access_token, known_token)
            assert known.result(timeout=2).subject == "synthetic"
            follower = pool.submit(identity.verify_access_token, _token(signing_keys, "other-unknown"))
            with pytest.raises(InvalidToken, match="unknown_kid"):
                follower.result(timeout=2)
        finally:
            release.set()
        with pytest.raises(InvalidToken, match="unknown_kid"):
            pending.result(timeout=5)
    assert len(calls) == 2


def test_failed_expired_refresh_is_throttled_without_extending_key_trust(signing_keys, ticks):
    jwks = _jwks(signing_keys, "old")
    calls = []

    def fetch():
        calls.append(None)
        if len(calls) == 2:
            raise httpx.ConnectError("synthetic identity connection failure")
        return jwks

    identity = KeycloakIdentity(ISSUER, AUDIENCE, jwks_provider=fetch, cache_ttl_s=10)
    token = _token(signing_keys, "old")
    identity.verify_access_token(token)
    ticks.now += 11
    with pytest.raises(InvalidToken, match="ConnectError"):
        identity.verify_access_token(token)
    with pytest.raises(InvalidToken):
        identity.verify_access_token(token)
    assert len(calls) == 2
    ticks.now += 60
    assert identity.verify_access_token(token).subject == "synthetic"
    assert len(calls) == 3
