"""Exercise research retries through the actual Realtime function-tool interface."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx2
import pytest
from agents.exceptions import ModelTimeoutError, UserError
from agents.tool_context import ToolContext
from openai import APIConnectionError, APIStatusError, APITimeoutError

import realtime_agent


ANSWER = "Verified answer. Source: https://example.com/source"
REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


def status_error(status):
    return APIStatusError(
        "request payload must not appear in logs",
        response=httpx2.Response(status, request=REQUEST), body=None,
    )


def invoke(question="Public question"):
    tool = realtime_agent.make_research_tool()
    arguments = json.dumps({"question": question})
    return asyncio.run(tool.on_invoke_tool(
        ToolContext(
            context=None, tool_name=tool.name, tool_call_id="research-call",
            tool_arguments=arguments,
        ), arguments,
    ))


@pytest.fixture
def mocked_research(monkeypatch):
    monkeypatch.delenv("SPECTER_RESEARCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv("SPECTER_TRACE_ENABLED", "false")
    run = AsyncMock()
    sleep = AsyncMock()
    monkeypatch.setattr(realtime_agent.Runner, "run", run)
    monkeypatch.setattr(realtime_agent.asyncio, "sleep", sleep)
    return run, sleep


@pytest.mark.parametrize("failures", [
    [],
    [asyncio.TimeoutError()],
    [ModelTimeoutError(15)],
    [APITimeoutError(REQUEST)],
    [APIConnectionError(request=REQUEST)],
    [status_error(code) for code in (408, 409)],
    [status_error(429)],
    [status_error(503)],
    [SimpleNamespace(final_output=None)],
    [SimpleNamespace(final_output="  "), status_error(500)],
])
def test_success_returns_only_answer_after_transient_failures(mocked_research, caplog, failures):
    run, sleep = mocked_research
    run.side_effect = [*failures, SimpleNamespace(final_output=f"  {ANSWER}  ")]
    assert invoke("  Public question  ") == ANSWER
    assert run.await_count == len(failures) + 1
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1][:len(failures)]
    for call in run.await_args_list:
        researcher, question = call.args
        assert question == "Public question"
        assert researcher.tools[0].search_context_size == "medium"
        assert researcher.model_settings.retry.max_retries == 0
        assert call.kwargs["max_turns"] == 6
        assert call.kwargs["run_config"].tracing_disabled is True
    assert "request payload" not in caplog.text
    for attempt in range(1, len(failures) + 1):
        assert f"attempt {attempt}/3 failed" in caplog.text


@pytest.mark.parametrize("failure, expected", [
    (asyncio.TimeoutError(), "Web research timed out."),
    (APITimeoutError(REQUEST), "Web research timed out."),
    (APIConnectionError(request=REQUEST), "Web research failed."),
    (status_error(503), "Web research failed."),
    (SimpleNamespace(final_output=""), "Web research failed."),
])
def test_three_failures_return_final_failure(mocked_research, caplog, failure, expected):
    run, sleep = mocked_research
    run.side_effect = [failure] * 3
    assert invoke().startswith(expected)
    assert run.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1]
    assert "attempt 3/3 failed" in caplog.text


@pytest.mark.parametrize("error", [
    *[status_error(code) for code in (400, 401, 403, 404, 422)],
    UserError("configuration error"), ValueError("bad parameter"), RuntimeError("unknown"),
])
def test_non_transient_errors_do_not_retry(mocked_research, error):
    run, sleep = mocked_research
    run.side_effect = error
    assert invoke().startswith("Web research failed.")
    assert run.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize("setting", ["0", "-1", "nan", "inf", "invalid"])
def test_invalid_timeout_does_not_start_research(monkeypatch, mocked_research, setting):
    run, sleep = mocked_research
    monkeypatch.setenv("SPECTER_RESEARCH_TIMEOUT_SECONDS", setting)
    assert invoke().startswith("Web research failed.")
    run.assert_not_awaited()
    sleep.assert_not_awaited()


@pytest.mark.parametrize("setting, expected", [(None, 15), ("2.5", 2.5)])
def test_timeout_applies_to_each_full_attempt(monkeypatch, mocked_research, setting, expected):
    run, sleep = mocked_research
    if setting is not None:
        monkeypatch.setenv("SPECTER_RESEARCH_TIMEOUT_SECONDS", setting)
    real_wait_for = asyncio.wait_for
    wait_for = AsyncMock(wraps=real_wait_for)
    monkeypatch.setattr(realtime_agent.asyncio, "wait_for", wait_for)
    run.side_effect = [SimpleNamespace(final_output=None)] * 2 + [SimpleNamespace(final_output=ANSWER)]
    assert invoke() == ANSWER
    assert [call.kwargs["timeout"] for call in wait_for.await_args_list] == [expected] * 3


def test_real_deadline_cancels_each_attempt(monkeypatch, mocked_research):
    run, sleep = mocked_research
    monkeypatch.setenv("SPECTER_RESEARCH_TIMEOUT_SECONDS", "0.005")
    cancelled = []

    async def blocked(*args, **kwargs):
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    run.side_effect = blocked
    assert invoke().startswith("Web research timed out.")
    assert run.await_count == len(cancelled) == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.5, 1]


def test_session_cancellation_propagates(mocked_research):
    run, sleep = mocked_research
    run.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        invoke()
    assert run.await_count == 1
    sleep.assert_not_awaited()


@pytest.mark.parametrize("question", ["   ", "q" * 1001])
def test_question_validation_still_skips_research(mocked_research, question):
    run, sleep = mocked_research
    assert "research question" in invoke(question).lower()
    run.assert_not_awaited()
    sleep.assert_not_awaited()


def test_trace_opt_in_is_preserved(monkeypatch, mocked_research):
    run, sleep = mocked_research
    monkeypatch.setenv("SPECTER_TRACE_ENABLED", "true")
    run.return_value = SimpleNamespace(final_output=ANSWER)
    assert invoke() == ANSWER
    assert run.await_args.kwargs["run_config"].tracing_disabled is False
