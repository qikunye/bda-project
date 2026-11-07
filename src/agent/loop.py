"""Runs the LLM on one case: it calls tools for a few steps, then submits a brief that code checks.
If the brief stays invalid or a limit is hit, the code-only brief is used instead."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from src import config
from src.agent import baseline, brief, findings, prompts, tools
from src.agent.brief import case_id_for
from src.agent.cases import Case
from src.agent.contracts import (
    Alert,
    CaseMember,
    InvestigateResult,
    RunStats,
    TraceStep,
    ValidationOutcome,
)
from src.agent.data import AgentData
from src.agent.llm import (
    AgentLLM,
    AgentTurn,
    LLMClientError,
    LLMUsage,
    SpendCapReached,
    SpendGuard,
    ToolCall,
)
from src.agent.evidence import RunEvidence
from src.agent.sql import assert_label_free, validate_window_hours
from src.agent.validate import validate_brief

TEMPERATURE = 0.0


@dataclass(frozen=True)
class LoopSettings:
    max_steps: int = config.AGENT_MAX_STEPS
    max_parallel_calls: int = config.AGENT_MAX_PARALLEL_CALLS
    max_output_tokens: int = config.AGENT_MAX_OUTPUT_TOKENS
    run_token_cap: int = config.AGENT_RUN_TOKEN_CAP
    timeout_s: float = config.AGENT_RUN_TIMEOUT_S
    # Window, top K and thresholds are shared with the code-only brief, so the fallback sees the same case.
    s1: baseline.Settings = field(default_factory=baseline.Settings)


class ToolInputError(ValueError):
    """The model asked for something its tools don't allow. It is told, and the run continues."""


class _FallbackNeeded(Exception):
    """Stops the LLM loop so the code-only brief is used instead. The message is the reason."""


def single_alert_case(txn_id: str, txn_date: str) -> Case:
    """A case made of one alert, for on-demand investigation."""

    return Case(case_id_for(txn_id), txn_date, txn_id, (txn_id,))


def _label_free(text: str) -> str:
    """Raises if the text mentions a ground-truth column, so labels never reach the model."""

    return assert_label_free(text)


def _tool_result_block(call: ToolCall, text: str, *, error: bool = False) -> dict:
    return {
        "toolResult": {
            "toolUseId": call.id,
            "content": [{"text": _label_free(text)}],
            "status": "error" if error else "success",
        }
    }


class _Run:
    """Holds one run's trace, what the model has seen, and the tool handlers."""

    def __init__(self, data: AgentData, case: Case, settings: LoopSettings, clock):
        self.data = data
        self.case = case
        self.settings = settings
        self.clock = clock
        self.trace: list[TraceStep] = []
        self.evidence = RunEvidence(
            representative=case.representative_txn_id,
            members=case.member_txn_ids,
            top_k=settings.s1.top_k,
            txn_dates={m: case.txn_date for m in case.member_txn_ids},
        )
        # Alerts fetched by code to serve a tool call. The LLM did not see them.
        self._member_alerts: dict[str, Alert] = {}

    def log(
        self, actor: str, tool: str, inputs: dict, result: dict, started: float
    ) -> None:
        self.trace.append(
            TraceStep(
                step=len(self.trace),
                actor=actor,
                tool=tool,
                inputs=inputs,
                result=result,
                latency_ms=int((self.clock() - started) * 1000),
            )
        )

    def show(self, tool: str, result: dict) -> str:
        """Records a result the model sees and returns the text to send."""

        text = _label_free(prompts.tool_result_view(result))
        self.evidence.record(tool, result)
        return text

    # --- Seed and tool handlers

    def fetch_seed(self) -> Alert:
        inputs = {
            "txn_id": self.case.representative_txn_id,
            "txn_date": self.case.txn_date,
        }
        started = self.clock()
        alert = tools.get_alert(
            self.data,
            inputs["txn_id"],
            inputs["txn_date"],
            top_k=self.settings.s1.top_k,
        )
        self.log("code", "get_alert", inputs, alert, started)
        return alert

    def _member_alert(self, txn_id: str) -> Alert:
        seed = self.evidence.alerts.get(txn_id)
        if seed is not None:
            return seed
        if txn_id not in self._member_alerts:
            inputs = {"txn_id": txn_id, "txn_date": self.case.txn_date}
            started = self.clock()
            alert = tools.get_alert(
                self.data, txn_id, self.case.txn_date, top_k=self.settings.s1.top_k
            )
            self.log("code", "get_alert", inputs, alert, started)
            if not alert.get("found"):
                raise ToolInputError(f"Case member {txn_id} was not found.")
            self._member_alerts[txn_id] = alert
        return self._member_alerts[txn_id]

    def _known_txn(self, value) -> str:
        if not isinstance(value, str) or value not in self.evidence.txn_dates:
            raise ToolInputError(
                f"{value!r} is not a transaction ID from this case or a tool result so far."
            )
        return value

    def _member(self, value) -> str:
        txn_id = self._known_txn(value)
        if txn_id not in self.case.member_txn_ids:
            raise ToolInputError(f"{txn_id} is not a member of this case.")
        return txn_id

    def _extra_accounts(self, args: dict) -> list[str]:
        """Accounts the model adds to a related-alerts search. Each must be one a tool returned."""

        accounts = args.get("accounts")
        if accounts is None:
            return []
        if not isinstance(accounts, list) or not all(isinstance(a, str) for a in accounts):
            raise ToolInputError("accounts must be a list of account ID strings.")
        unknown = [a for a in accounts if a not in self.evidence.known_ids]
        if unknown:
            raise ToolInputError(
                f"{unknown[:3]} is not an account ID from a tool result so far."
            )
        return accounts

    def _window(self, args: dict) -> int:
        try:
            return validate_window_hours(
                args.get("window_h", self.settings.s1.window_h)
            )
        except ValueError as error:
            raise ToolInputError(str(error)) from error

    def execute(self, call: ToolCall) -> tuple[str, bool]:
        """Runs one tool call from the model. Returns the reply text and whether it was an error."""

        started = self.clock()
        args = call.input
        try:
            if call.name == "get_alert":
                txn_id = self._known_txn(args.get("txn_id"))
                result = tools.get_alert(
                    self.data,
                    txn_id,
                    self.evidence.txn_dates[txn_id],
                    top_k=self.settings.s1.top_k,
                )
            elif call.name == "get_case_graph":
                alert = self._member_alert(self._member(args.get("txn_id")))
                result = tools.get_case_graph(
                    self.data, alert, self._window(args), as_of=self.settings.s1.as_of
                )
            elif call.name == "get_account_activity":
                alert = self._member_alert(self._member(args.get("txn_id")))
                side = args.get("side")
                if side not in ("sender", "receiver"):
                    raise ToolInputError("side must be 'sender' or 'receiver'.")
                result = tools.get_account_activity(
                    self.data,
                    alert,
                    side,
                    self._window(args),
                    as_of=self.settings.s1.as_of,
                )
            elif call.name == "get_neighbour_scores":
                txn_ids = args.get("txn_ids")
                if (
                    not isinstance(txn_ids, list)
                    or not txn_ids
                    or not all(isinstance(t, str) for t in txn_ids)
                ):
                    raise ToolInputError(
                        "txn_ids must be a non-empty list of ID strings."
                    )
                txn_ids = [self._known_txn(t) for t in dict.fromkeys(txn_ids)]
                result = tools.get_neighbour_scores(
                    self.data,
                    txn_ids,
                    [self.evidence.txn_dates[t] for t in txn_ids],
                    top_k=self.settings.s1.top_k,
                )
            elif call.name == "get_related_alerts":
                txn_id = self._member(args.get("txn_id"))
                alert = self._member_alert(txn_id)
                result = tools.get_related_alerts(
                    self.data,
                    [alert["sender"], alert["receiver"], *self._extra_accounts(args)],
                    alert["ts"],
                    self._window(args),
                    exclude_txn_ids=tuple(self.case.member_txn_ids),
                    top_k=self.settings.s1.top_k,
                    as_of=self.settings.s1.as_of,
                )
            else:
                raise ToolInputError(f"Unknown tool {call.name!r}.")
        except (ToolInputError, ValueError) as error:
            # ValueError covers input the tool layer rejects, such as more IDs than allowed.
            message = {"error": str(error)}
            self.log("llm", call.name, dict(args), message, started)
            return json.dumps(message), True

        self.log("llm", call.name, dict(args), result, started)
        return self.show(call.name, result), False

    def case_members(self) -> list[CaseMember]:
        """Every alert in the case with its payments, for the analyst.

        Code fetches any graph the LLM skipped. These never reach the LLM or the validator, because
        the brief is already made.
        """

        members: list[CaseMember] = []
        for txn_id in self.case.member_txn_ids:
            alert = self._member_alert(txn_id)
            graph = self.evidence.graphs.get(txn_id)
            if graph is None:
                started = self.clock()
                window_h = self.settings.s1.window_h
                graph = tools.get_case_graph(
                    self.data, alert, window_h, as_of=self.settings.s1.as_of
                )
                self.log(
                    "code",
                    "get_case_graph",
                    {"txn_id": txn_id, "window_h": window_h},
                    graph,
                    started,
                )
            members.append(CaseMember(alert=alert, graph=graph))
        return members


def _not_found(
    case: Case, seed: Alert, window_h: int, trace: list[TraceStep]
) -> InvestigateResult:
    return InvestigateResult(
        status="not_found",
        txn_id=case.representative_txn_id,
        txn_date=case.txn_date,
        window_h=window_h,
        alert=seed,
        case_id=None,
        summary=None,
        findings=[],
        gaps=[],
        next_checks=[],
        observations=[],
        money_flow=[],
        members=[],
        fallback=False,
        stats=None,
        trace=trace,
    )


def _fallback_result(
    run: _Run,
    llm: AgentLLM,
    reason: str,
    *,
    usage: LLMUsage,
    steps: int,
    repair_used: bool,
    started: float,
) -> InvestigateResult:
    """The code-only brief, keeping the model's trace and token counts."""

    llm_trace = list(run.trace)
    llm_trace.append(
        TraceStep(
            step=len(llm_trace),
            actor="code",
            tool="fallback",
            inputs={},
            result={"reason": reason},
            latency_ms=0,
        )
    )
    s1 = baseline.brief_case(run.data, run.case, run.settings.s1)
    for step in s1["trace"]:
        llm_trace.append({**step, "step": len(llm_trace)})
    s1["trace"] = llm_trace
    s1["fallback"] = True
    s1["stats"] = RunStats(
        model=llm.model_id,
        steps=steps,
        tokens_in=usage.input_tokens,
        tokens_out=usage.output_tokens,
        latency_ms=int((run.clock() - started) * 1000),
        validation="fallback",
        repair_used=repair_used,
        fallback=True,
    )
    return s1


def _notice(steps: int, settings: LoopSettings) -> str:
    left = settings.max_steps - steps
    if left <= 1:
        return f"Step {steps} of {settings.max_steps} used. Your next step must be {prompts.SUBMIT}."
    return f"Step {steps} of {settings.max_steps} used."


def run_agent(
    data: AgentData,
    llm: AgentLLM,
    case: Case,
    *,
    settings: LoopSettings | None = None,
    spend: SpendGuard | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> InvestigateResult:
    """Investigates one case with the LLM and returns a checked brief, or the code-only brief if the LLM
    fails or hits a limit. A label leak or data error raises instead.
    """

    settings = settings or LoopSettings()
    started = clock()
    run = _Run(data, case, settings, clock)

    seed = run.fetch_seed()
    if not seed.get("found"):
        return _not_found(case, seed, settings.s1.window_h, run.trace)
    seed_text = prompts.seed_message(case, seed, window_h=settings.s1.window_h)
    _label_free(seed_text)
    run.evidence.record("get_alert", seed)

    _label_free(prompts.SYSTEM_PROMPT)
    _label_free(
        json.dumps([[t.name, t.description, t.input_schema] for t in prompts.TOOLS])
    )

    messages: list[dict] = [{"role": "user", "content": [{"text": seed_text}]}]
    usage = LLMUsage(0, 0)
    steps = 0
    repair_used = False
    repairing = False
    submitted = None

    try:
        while submitted is None:
            if not repairing and steps >= settings.max_steps:
                raise _FallbackNeeded("No brief within the step limit.")
            if usage.input_tokens + usage.output_tokens >= settings.run_token_cap:
                raise _FallbackNeeded("Run token cap reached.")
            if clock() - started > settings.timeout_s:
                raise _FallbackNeeded("Run timeout reached.")
            if spend is not None:
                spend.check()

            last_step = steps == settings.max_steps - 1
            turn: AgentTurn = llm.converse(
                system=prompts.SYSTEM_PROMPT,
                messages=messages,
                tools=prompts.TOOLS,
                force_tool=prompts.SUBMIT if (last_step or repairing) else None,
                max_tokens=settings.max_output_tokens,
                temperature=TEMPERATURE,
            )
            usage = usage + turn.usage
            if spend is not None:
                spend.record(turn.usage)
            if not repairing:
                steps += 1
            messages.append({"role": "assistant", "content": turn.content})

            if not turn.tool_calls:
                if repairing:
                    raise _FallbackNeeded("No brief after the repair request.")
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "text": f"Call a tool or {prompts.SUBMIT}. {_notice(steps, settings)}"
                            }
                        ],
                    }
                )
                continue

            submit = next(
                (c for c in turn.tool_calls if c.name == prompts.SUBMIT), None
            )
            if submit is None:
                if repairing:
                    raise _FallbackNeeded("No brief after the repair request.")
                blocks = []
                for index, call in enumerate(turn.tool_calls):
                    if index >= settings.max_parallel_calls:
                        blocks.append(
                            _tool_result_block(
                                call,
                                f"Not run: at most {settings.max_parallel_calls} tool calls per step.",
                                error=True,
                            )
                        )
                        continue
                    text, is_error = run.execute(call)
                    blocks.append(_tool_result_block(call, text, error=is_error))
                blocks.append({"text": _notice(steps, settings)})
                messages.append({"role": "user", "content": blocks})
                continue

            started_submit = clock()
            validated = validate_brief(submit.input, run.evidence)
            run.log(
                "llm",
                prompts.SUBMIT,
                dict(submit.input),
                {"valid": validated.ok, "errors": list(validated.errors)},
                started_submit,
            )
            if validated.ok:
                submitted = validated.brief
                break
            if repair_used:
                raise _FallbackNeeded("Brief still invalid after one repair.")

            repair_used = repairing = True
            problems = "\n".join(f"- {e}" for e in validated.errors)
            blocks = [
                _tool_result_block(
                    call,
                    "Brief rejected:\n"
                    + problems
                    + f"\nCall {prompts.SUBMIT} again with these fixed."
                    if call is submit
                    else "Not run: the brief is being corrected.",
                    error=True,
                )
                for call in turn.tool_calls
            ]
            messages.append({"role": "user", "content": blocks})
    except _FallbackNeeded as stop:
        reason = str(stop)
    except LLMClientError as error:
        reason = f"LLM call failed: {error}"
    except SpendCapReached as error:
        reason = str(error)
    else:
        return _briefed_result(
            run,
            llm,
            submitted,
            usage=usage,
            steps=steps,
            repair_used=repair_used,
            started=started,
        )

    return _fallback_result(
        run,
        llm,
        reason,
        usage=usage,
        steps=steps,
        repair_used=repair_used,
        started=started,
    )


def _briefed_result(
    run: _Run,
    llm: AgentLLM,
    submitted,
    *,
    usage: LLMUsage,
    steps: int,
    repair_used: bool,
    started: float,
) -> InvestigateResult:
    """Adds what code writes (sentences, gaps, money flow, case_id) to the LLM's checked brief."""

    evidence = run.evidence
    instances = findings.detect(evidence)
    submitted_findings = [(f["code"], f["evidence_ids"]) for f in submitted["findings"]]
    gaps = brief.compute_gaps(evidence)
    validation: ValidationOutcome = "repaired" if repair_used else "valid"
    members = run.case_members()
    return InvestigateResult(
        status="ok",
        txn_id=run.case.representative_txn_id,
        txn_date=run.case.txn_date,
        window_h=run.settings.s1.window_h,
        alert=evidence.alerts[run.case.representative_txn_id],
        case_id=case_id_for(run.case.representative_txn_id)
        if len(run.case.member_txn_ids) > 1
        else None,
        summary=submitted["summary"],
        findings=brief.render_findings(submitted_findings, instances),
        gaps=gaps,
        next_checks=brief.render_next_checks(submitted["next_checks"]),
        observations=submitted["observations"],
        money_flow=brief.build_money_flow(members),
        members=members,
        fallback=False,
        stats=RunStats(
            model=llm.model_id,
            steps=steps,
            tokens_in=usage.input_tokens,
            tokens_out=usage.output_tokens,
            latency_ms=int((run.clock() - started) * 1000),
            validation=validation,
            repair_used=repair_used,
            fallback=False,
        ),
        trace=run.trace,
    )
