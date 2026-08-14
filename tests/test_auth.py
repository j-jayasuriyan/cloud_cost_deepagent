import time

import pytest

import auth


@pytest.fixture(autouse=True)
def clean_sessions():
    auth._sessions.clear()
    yield
    auth._sessions.clear()


def should_accept_when_credentials_match():
    assert auth.verify_credentials("Admin", "Admin@123")


@pytest.mark.parametrize(
    "username,password",
    [
        ("admin", "Admin@123"),       # username is case-sensitive
        ("Admin", "admin@123"),       # password is case-sensitive
        ("Admin", "Admin@1234"),
        ("Admin", ""),
        ("", "Admin@123"),
        ("Admin ", "Admin@123"),      # no whitespace tolerance
    ],
)
def should_reject_when_credentials_do_not_match(username, password):
    assert not auth.verify_credentials(username, password)


def should_read_credentials_from_environment_when_set(monkeypatch):
    monkeypatch.setattr(auth, "_USERNAME", "deployer")
    monkeypatch.setattr(auth, "_PASSWORD", "s3cret-from-env")

    assert auth.verify_credentials("deployer", "s3cret-from-env")
    assert not auth.verify_credentials("Admin", "Admin@123")


def should_issue_unique_tokens_when_sessions_are_created():
    assert auth.create_session() != auth.create_session()


def should_accept_session_when_token_is_current():
    assert auth.is_valid_session(auth.create_session())


@pytest.mark.parametrize("token", [None, "", "not-a-real-token"])
def should_reject_session_when_token_is_unknown(token):
    assert not auth.is_valid_session(token)


def should_reject_session_when_token_has_expired(monkeypatch):
    token = auth.create_session()
    past_expiry = time.time() + auth.session_max_age() + 1
    monkeypatch.setattr(time, "time", lambda: past_expiry)

    assert not auth.is_valid_session(token)


def should_reject_session_when_destroyed():
    token = auth.create_session()
    auth.destroy_session(token)

    assert not auth.is_valid_session(token)


def should_not_raise_when_destroying_an_unknown_session():
    auth.destroy_session("never-existed")
    auth.destroy_session(None)


def should_drop_expired_tokens_when_new_session_is_created(monkeypatch):
    auth._sessions["stale"] = time.time() - 1

    auth.create_session()

    assert "stale" not in auth._sessions
