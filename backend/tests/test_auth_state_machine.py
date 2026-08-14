import asyncio

import pytest

from app.crawlers.runtime import (
    AuthState,
    AuthStateMachine,
    CancellationToken,
    CrawlerErrorCode,
    CrawlerFailure,
    SessionProbe,
    SessionStatus,
)


async def wait_for_state(machine: AuthStateMachine, state: AuthState) -> None:
    for _ in range(100):
        if machine.state is state:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"state did not reach {state}")


def test_interactive_auth_waits_for_user_then_rechecks_session() -> None:
    async def run():
        probes = iter(
            (
                SessionProbe(SessionStatus.AUTH_REQUIRED, "Scan QR in visible browser"),
                SessionProbe(SessionStatus.READY, "Session confirmed"),
            )
        )
        machine = AuthStateMachine()

        async def probe():
            return next(probes)

        task = asyncio.create_task(
            machine.establish(
                probe,
                interactive=True,
                timeout_seconds=5,
                cancellation=CancellationToken(),
            )
        )
        await wait_for_state(machine, AuthState.WAITING_FOR_USER)
        machine.continue_after_user_action()
        result = await task
        await machine.mark_running()
        await machine.mark_ready()
        await machine.close()
        return result, machine

    result, machine = asyncio.run(run())
    assert result.status is SessionStatus.READY
    assert [transition.current for transition in machine.history] == [
        AuthState.OPENING,
        AuthState.CHECKING_SESSION,
        AuthState.AUTH_REQUIRED,
        AuthState.WAITING_FOR_USER,
        AuthState.CHECKING_SESSION,
        AuthState.READY,
        AuthState.RUNNING,
        AuthState.READY,
        AuthState.CLOSING,
        AuthState.CLOSED,
    ]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (SessionStatus.AUTH_REQUIRED, CrawlerErrorCode.AUTH_REQUIRED),
        (SessionStatus.CHALLENGE_REQUIRED, CrawlerErrorCode.CHALLENGE_REQUIRED),
    ],
)
def test_background_auth_never_enters_waiting_for_user(status, code) -> None:
    async def run():
        machine = AuthStateMachine()

        async def probe():
            return SessionProbe(status, "User interaction required")

        with pytest.raises(CrawlerFailure) as captured:
            await machine.establish(
                probe,
                interactive=False,
                timeout_seconds=5,
                cancellation=CancellationToken(),
            )
        await machine.close()
        return captured.value, machine

    failure, machine = asyncio.run(run())
    assert failure.code is code
    assert AuthState.WAITING_FOR_USER not in {
        transition.current for transition in machine.history
    }
    assert machine.state is AuthState.CLOSED


def test_auth_timeout_is_typed_and_does_not_retry_forever() -> None:
    async def run():
        machine = AuthStateMachine()

        async def probe():
            return SessionProbe(SessionStatus.AUTH_REQUIRED)

        with pytest.raises(CrawlerFailure) as captured:
            await machine.establish(
                probe,
                interactive=True,
                timeout_seconds=0.01,
                cancellation=CancellationToken(),
            )
        await machine.close()
        return captured.value, machine

    failure, machine = asyncio.run(run())
    assert failure.code is CrawlerErrorCode.AUTH_TIMEOUT
    assert failure.retryable is True
    assert machine.state is AuthState.CLOSED


def test_cancellation_while_waiting_is_observed() -> None:
    async def run():
        token = CancellationToken()
        machine = AuthStateMachine()

        async def probe():
            return SessionProbe(SessionStatus.CHALLENGE_REQUIRED)

        task = asyncio.create_task(
            machine.establish(
                probe,
                interactive=True,
                timeout_seconds=5,
                cancellation=token,
            )
        )
        await wait_for_state(machine, AuthState.WAITING_FOR_USER)
        token.cancel()
        with pytest.raises(CrawlerFailure) as captured:
            await task
        await machine.close()
        return captured.value

    failure = asyncio.run(run())
    assert failure.code is CrawlerErrorCode.CANCELLED


def test_auth_continue_outside_waiting_state_is_rejected() -> None:
    with pytest.raises(RuntimeError, match="only valid"):
        AuthStateMachine().continue_after_user_action()
