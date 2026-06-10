"""Unit tests for the rules engine — including the time-dependent rules that
the API surface can't exercise without time travel (30-day cache expiry,
14-day username window)."""
from datetime import timedelta

import pytest

from app import rules
from app.errors import ApiError
from app.models import ApiKey, UserState


def make_key(config: dict | None = None, **user_kw) -> ApiKey:
    key = ApiKey(id="key_000001", config=config or {})
    defaults = dict(api_key_id="key_000001", phone="5511888880001",
                    display_name="U", country="BR", bsuid="BR.1234567890123456789",
                    parent_bsuid="BR.ENT.123456789012345", contact_book=False,
                    contact_book_phone_known=False, cache_at=None,
                    cache_phone_known=False, window_opened_at=None,
                    had_phone_traffic=False)
    defaults.update(user_kw)
    key.user = UserState(**defaults)
    return key


# ------------------------------------------------------------- visibility

def test_pre_ga_always_visible():
    key = make_key({"ga_mode": False, "user": {"has_username": True}})
    assert rules.phone_visible(key)


def test_no_username_always_visible():
    key = make_key({"user": {"has_username": False}})
    assert rules.phone_visible(key)


def test_ga_username_no_history_hidden():
    key = make_key({"user": {"has_username": True}})
    assert not rules.phone_visible(key)


def test_forced_overrides():
    key = make_key({"user": {"has_username": True, "phone_visibility": "always"}})
    assert rules.phone_visible(key)
    key = make_key({"ga_mode": False, "user": {"phone_visibility": "never"}})
    assert not rules.phone_visible(key)
    key = make_key({"user": {"has_username": True, "in_contact_book": True}})
    assert rules.phone_visible(key)


def test_contact_book_grants_visibility_only_when_phone_known():
    key = make_key({"user": {"has_username": True}},
                   contact_book=True, contact_book_phone_known=True)
    assert rules.phone_visible(key)
    key = make_key({"user": {"has_username": True}},
                   contact_book=True, contact_book_phone_known=False)
    assert not rules.phone_visible(key)


def test_forced_in_contact_book_false_skips_real_entry():
    key = make_key({"user": {"has_username": True, "in_contact_book": False}},
                   contact_book=True, contact_book_phone_known=True)
    assert not rules.phone_visible(key)


def test_cache_30_days_per_number():
    fresh = rules.now() - timedelta(days=29)
    stale = rules.now() - timedelta(days=31)
    key = make_key({"user": {"has_username": True}},
                   cache_at=fresh, cache_phone_known=True)
    assert rules.phone_visible(key)
    key = make_key({"user": {"has_username": True}},
                   cache_at=stale, cache_phone_known=True)
    assert not rules.phone_visible(key), "cache must expire after 30 days"
    key = make_key({"user": {"has_username": True}},
                   cache_at=fresh, cache_phone_known=False)
    assert not rules.phone_visible(key), "BSUID-only cache entries reveal nothing"


def test_touch_contact_phone_known_upgrades_never_downgrades():
    key = make_key()
    user = key.user
    rules.touch_contact(user, rules.now(), phone_known=False)
    assert user.contact_book and not user.contact_book_phone_known
    rules.touch_contact(user, rules.now(), phone_known=True)
    assert user.contact_book_phone_known and user.cache_phone_known
    rules.touch_contact(user, rules.now(), phone_known=False)
    assert user.contact_book_phone_known, "later BSUID traffic must not downgrade"


def test_expired_cache_refreshed_by_bsuid_traffic_stays_demoted():
    key = make_key()
    user = key.user
    user.cache_at = rules.now() - timedelta(days=31)
    user.cache_phone_known = True
    user.contact_book = True
    rules.touch_contact(user, rules.now(), phone_known=False)
    assert not user.cache_phone_known, \
        "a BSUID send must not resurrect an expired phone cache"


# ------------------------------------------------------------ window

def test_window_states():
    key = make_key({"user": {"service_window": "open"}})
    assert rules.window_open(key)
    key = make_key({"user": {"service_window": "closed"}},
                   window_opened_at=rules.now())
    assert not rules.window_open(key)
    key = make_key(window_opened_at=rules.now() - timedelta(hours=23))
    assert rules.window_open(key)
    key = make_key(window_opened_at=rules.now() - timedelta(hours=25))
    assert not rules.window_open(key)
    key = make_key()
    assert not rules.window_open(key)


# ------------------------------------------------------------ config

def test_deep_merge():
    merged = rules.deep_merge({"a": {"x": 1, "y": 2}, "b": 1},
                              {"a": {"y": 3}, "c": 4})
    assert merged == {"a": {"x": 1, "y": 3}, "b": 1, "c": 4}


def test_effective_config_defaults_and_merge():
    key = make_key({"user": {"phone_visibility": "never"}})
    cfg = rules.effective_config(key)
    assert cfg["user"]["phone_visibility"] == "never"
    assert cfg["user"]["has_username"] is True
    assert cfg["ga_mode"] is True
    assert cfg["consumer_actions"]["reply_to_messages"] is False


def test_user_username_resolution():
    key = make_key({"user": {"has_username": False}})
    assert rules.user_username(key) is None
    key = make_key({"user": {"has_username": True, "username": "maria.s"}})
    assert rules.user_username(key) == "maria.s"
    key = make_key({"user": {"has_username": True}})
    derived = rules.user_username(key)
    assert derived and derived.startswith("user.")
    assert rules.user_username(key) == derived  # deterministic


def test_validate_config_patch_rejects_unknowns():
    with pytest.raises(ApiError):
        rules.validate_config_patch({"bogus": 1})
    with pytest.raises(ApiError):
        rules.validate_config_patch({"user": {"bogus": 1}})
    rules.validate_config_patch({"user": {"in_contact_book": False}})
    rules.validate_config_patch({"inject_error": None})


# ------------------------------------------------------------ username format

@pytest.mark.parametrize("name", ["abc", "a.b_c", "user.name35", "A.Valid.Name"])
def test_valid_usernames(name):
    rules.validate_username_format(name)


@pytest.mark.parametrize("name", [
    "ab", "a" * 36, "1234", ".abc", "abc.", "a..b", "wwwshop", "shop.com",
    "shop.org", "shop.html", "has space", "emoji😀", None, 42])
def test_invalid_usernames(name):
    with pytest.raises(ApiError) as e:
        rules.validate_username_format(name)
    assert e.value.body["error"]["code"] == 100
