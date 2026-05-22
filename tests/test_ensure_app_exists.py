"""Tests for ensure_app_exists() in fly_ops.py.

Covers the unconditional cleanup + retry-create loop introduced to fix two
failure modes:

  Case A — app visible via `fly status`: destroy runs but Fly's GraphQL hasn't
            propagated the deletion yet → "Name has already been taken".

  Case B — app not in the dashboard (no machines, invisible to `fly status`):
            the old `if app_exists():` guard skipped destroy entirely, so the
            name stayed reserved and every create attempt failed immediately.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from flyexit.fly_ops import AppStatus, ensure_app_exists


def _make_run(returncode: int, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# Happy path: app does not exist (Case B baseline)
# ---------------------------------------------------------------------------


def test_creates_when_app_absent():
    """No existing app → kill+destroy (no-ops), create succeeds on first try."""
    with (
        patch("flyexit.fly_ops.app_exists", return_value=True),
        patch("flyexit.fly_ops.kill_all_machines") as mock_kill,
        patch("flyexit.fly_ops.destroy_app", return_value=False) as mock_destroy,
        patch("flyexit.fly_ops.subprocess.run", return_value=_make_run(0)) as mock_run,
        patch("flyexit.fly_ops.time.sleep") as mock_sleep,
    ):
        status, err = ensure_app_exists("fly-vpn-node", "personal")

    assert status is AppStatus.CREATED
    assert err == ""
    mock_kill.assert_called_once_with("fly-vpn-node")
    mock_destroy.assert_called_once_with("fly-vpn-node")
    mock_run.assert_called_once()
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Stale app: exists → destroy → create succeeds immediately
# ---------------------------------------------------------------------------


def test_destroys_stale_app_then_creates():
    """Existing stale app is destroyed; create succeeds on first attempt."""
    with (
        patch("flyexit.fly_ops.app_exists", return_value=True),
        patch("flyexit.fly_ops.kill_all_machines") as mock_kill,
        patch("flyexit.fly_ops.destroy_app", return_value=True) as mock_destroy,
        patch("flyexit.fly_ops.subprocess.run", return_value=_make_run(0)),
        patch("flyexit.fly_ops.time.sleep"),
    ):
        status, err = ensure_app_exists("fly-vpn-node", "personal")

    assert status is AppStatus.CREATED
    assert err == ""
    mock_kill.assert_called_once_with("fly-vpn-node")
    mock_destroy.assert_called_once_with("fly-vpn-node")


# ---------------------------------------------------------------------------
# Post-destroy lag: name still "taken" on first 2 create attempts
# ---------------------------------------------------------------------------


def test_waits_for_name_release_before_creating():
    """Name still taken on first 2 create attempts, released on 3rd — still creates.

    Ordering proof: sleep is only called inside the retry loop, after a
    "already been taken" failure.  2 sleeps means 2 failed attempts before
    the 3rd succeeds — so the create only happened after the name was free.
    """
    create_seq = [
        _make_run(1, stderr="Name has already been taken"),
        _make_run(1, stderr="Name has already been taken"),
        _make_run(0),
    ]
    with (
        patch("flyexit.fly_ops.app_exists", return_value=True),
        patch("flyexit.fly_ops.kill_all_machines"),
        patch("flyexit.fly_ops.destroy_app", return_value=True),
        patch("flyexit.fly_ops.subprocess.run", side_effect=create_seq) as mock_run,
        patch("flyexit.fly_ops.time.sleep") as mock_sleep,
    ):
        status, err = ensure_app_exists("fly-vpn-node", "personal")

    assert status is AppStatus.CREATED
    assert err == ""
    assert mock_sleep.call_count == 2
    assert mock_run.call_count == 3


# ---------------------------------------------------------------------------
# Max retries exhausted
# ---------------------------------------------------------------------------


def test_fails_after_max_retries():
    """After 10 failed 'already taken' attempts the function returns FAILED."""
    create_seq = [
        _make_run(1, stderr="Name has already been taken")
    ] * 10
    with (
        patch("flyexit.fly_ops.kill_all_machines"),
        patch("flyexit.fly_ops.destroy_app", return_value=True),
        patch("flyexit.fly_ops.subprocess.run", side_effect=create_seq),
        patch("flyexit.fly_ops.time.sleep"),
    ):
        status, err = ensure_app_exists("fly-vpn-node", "personal")

    assert status is AppStatus.FAILED
    assert "already been taken" in err


# ---------------------------------------------------------------------------
# Non-transient create error
# ---------------------------------------------------------------------------


def test_returns_failed_on_non_transient_create_error():
    """A non-'already taken' error (e.g. billing) fails immediately, no retry."""
    with (
        patch("flyexit.fly_ops.kill_all_machines"),
        patch("flyexit.fly_ops.destroy_app", return_value=False),
        patch(
            "flyexit.fly_ops.subprocess.run",
            return_value=_make_run(1, stderr="requires a credit card"),
        ) as mock_run,
        patch("flyexit.fly_ops.time.sleep") as mock_sleep,
    ):
        status, err = ensure_app_exists("fly-vpn-node", "personal")

    assert status is AppStatus.FAILED
    assert "credit card" in err
    mock_run.assert_called_once()
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# diagnosis.py hint wiring
# ---------------------------------------------------------------------------


def test_diagnose_returns_hint_for_name_taken():
    """diagnose_fly_error matches 'already been taken' and formats app_name."""
    from flyexit.diagnosis import diagnose_fly_error

    hint = diagnose_fly_error(
        "Validation failed: Name has already been taken",
        "",
        app_name="fly-vpn-node",
    )

    assert hint is not None
    assert "fly-vpn-node" in hint
    assert "watchdog" in hint
