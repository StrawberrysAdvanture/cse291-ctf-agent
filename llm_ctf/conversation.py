import time
import subprocess
import os
import json
import signal
import sys
import traceback as tb
import openai
import anthropic
from typing import Tuple, Optional, List

from pathlib import Path
from nyuctf.challenge import CTFChallenge

from .ctflogging import status
from .backends import Backend
from .prompts.prompts import PromptManager
from .tools import ToolCall, ToolResult, Tool, TOOLSETS
from .environment import CTFEnvironment
from .utils import timestamp

now = lambda: time.time()

class Terminated(Exception):
    """Raised from the SIGTERM handler so the attempt is logged before the process exits."""

def _raise_terminated(signum, frame):
    raise Terminated(f"received signal {signum}")

def is_context_length_error(msg: str) -> bool:
    # OpenAI error codes, and the message vLLM's OpenAI-compatible server returns
    return any(s in msg for s in (
        "'code': 'context_length_exceeded'",
        "'code': 'string_above_max_length'",
        "maximum context length",
    ))

def summarize_tool_result(tool_call: ToolCall, result: ToolResult, latency: float) -> dict:
    """Per-call record used for the error, timeout and latency metrics."""
    res = result.result if isinstance(result.result, dict) else {}
    return {
        "id": result.id,
        "name": result.name,
        "arguments": tool_call.function.parsed_arguments if tool_call.function.parsed_arguments is not None
                     else tool_call.function.arguments,
        "latency": latency,
        "error": res.get("error"),
        "returncode": res.get("returncode"),
        "timed_out": res.get("timed_out", False),
    }

class CTFConversation:
    def __init__(self, environment: CTFEnvironment, challenge: CTFChallenge, prompt_manager: PromptManager, backend: Backend, logfile: Path, max_rounds:int=30, max_cost:float=1.0, max_time:Optional[float]=None, args=None, config=None):
        self.challenge = challenge
        self.environment = environment
        self.prompt_manager = prompt_manager
        self.backend = backend
        self.logfile = logfile

        self.available_functions : dict[str,Tool] = {}

        self.max_rounds = max_rounds
        self.max_cost = max_cost
        self.max_time = max_time
        self.config = config
        self.args = args

        self.rounds = 0
        self.cost = 0
        self.finish_reason = "unknown"
        self.model_time = 0
        self.tool_time = 0
        # One record per model call: latency, token usage and the tool calls it made
        self.round_log = []
        self.exception_info = None
        self.teardown_errors = []
        self.network_internal = None
        self.start_time = now()
        self.end_time = None

    def __enter__(self):
        signal.signal(signal.SIGTERM, _raise_terminated)
        try:
            self.network_internal = self.environment.network_is_internal()
            self.backend.setup()
            self.challenge.start_challenge_container()
            self.environment.setup()
        except BaseException:
            # __exit__ is not called when __enter__ fails, so log and clean up here
            self.finish_reason = "setup_error"
            self.__exit__(*sys.exc_info())
            raise

        self.start_time = now()
        self.save_log()
        return self

    def out_of_time(self):
        return self.max_time is not None and now() - self.start_time > self.max_time

    def run(self):
        next_msg = self.prompt_manager.initial_message(self.challenge)
        while not self.environment.solved and not self.environment.giveup \
                and self.rounds < self.max_rounds and self.cost <= self.max_cost \
                and not self.out_of_time():
            try:
                tools_run = self.run_conversation_step(next_msg)
                if tools_run == 0:
                    next_msg = self.prompt_manager.keep_going().strip()
                else:
                    next_msg = None
            except KeyboardInterrupt:
                status.print("[red bold]Interrupted by user[/red bold]", markup=True)
                self.finish_reason = "user_cancel"
                break
            except Terminated as e:
                status.print(f"[red bold]Terminated: {e}[/red bold]", markup=True)
                self.finish_reason = "terminated"
                break
            # TODO Normalize the ratelimiterrors
            except (openai.RateLimitError, anthropic.RateLimitError):
                status.print("[red bold]Rate limit reached![/red bold]", markup=True)
                self.finish_reason = "rate_limit"
                break
            except openai.BadRequestError as e:
                if is_context_length_error(str(e)):
                    status.print("[red bold]Context length exceeded![/red bold]", markup=True)
                    self.finish_reason = "context_length"
                    break
                self.record_exception()
                break
            except Exception:
                self.record_exception()
                break
            finally:
                self.save_log()

        # Look for a finish reason
        if self.environment.solved:
            status.print("[red bold]Challenge solved by our robot overlords![/red bold]", markup=True)
            self.finish_reason = "solved"
        elif self.environment.giveup:
            status.print("[red bold]The LLM decided to give up! NGMI.[/red bold]", markup=True)
            self.finish_reason = "give_up"
        elif self.finish_reason != "unknown":
            pass
        elif self.cost > self.max_cost:
            status.print(f"[red bold]Challenge is unsolved after {self.max_cost} dollars of cost; exiting[/red bold]", markup=True)
            self.finish_reason = "max_cost"
        elif self.rounds >= self.max_rounds:
            status.print(f"[red bold]Challenge is unsolved after {self.max_rounds} rounds; exiting[/red bold]", markup=True)
            self.finish_reason = "max_rounds"
        elif self.out_of_time():
            status.print(f"[red bold]Challenge is unsolved after {self.max_time} seconds; exiting[/red bold]", markup=True)
            self.finish_reason = "max_time"

    def record_exception(self):
        self.exception_info = tb.format_exc()
        self.finish_reason = "error"
        status.print(f"[red bold]Error during attempt:[/red bold]\n{self.exception_info}", markup=True)

    def run_tools(self, tool_calls: List[ToolCall]) -> Tuple[List[ToolResult], List[dict]]:
        tool_results = []
        records = []
        for tool_call in tool_calls:
            st = now()
            # Tool lookup
            tool = self.environment.available_tools.get(tool_call.name)
            if not tool:
                status.error_message(f"Unknown tool {tool_call.name}")
                tool_res = tool_call.error(f"Unknown tool {tool_call.name}")
            else:
                # Parse arguments
                parsed, tool_call = self.backend.parse_tool_arguments(tool, tool_call)
                if not parsed:
                    tool_res = tool_call
                else:
                    try:
                        tool_res = tool.run(tool_call)
                    except TypeError as e:
                        status.debug_message(f"Error encoding results from {tool.name}: {e}")
                        tool_res = tool_call.error(f"{type(e).__name__} running {tool.name}: {e}")
                    except Exception as e:
                        status.debug_message(f"Error running {tool.name}: {e}")
                        tool_res = tool_call.error(f"{type(e).__name__} running {tool.name}: {e}")
            tool_results.append(tool_res)
            records.append(summarize_tool_result(tool_call, tool_res, now() - st))
        return tool_results, records

    def run_conversation_step(self, message: Optional[str]=None):
        if message:
            status.user_message(message)
        status.assistant_message("🤔 ...thinking... 🤔")

        # A round that raises before finishing keeps model_latency None and is excluded from metrics
        record = {"round": self.rounds, "start": timestamp(), "model_latency": None, "tool_calls": []}
        self.round_log.append(record)

        # Prompt the model to produce a response and tool_calls
        st = now()
        content, tool_calls, cost = self.backend.send(message)
        record["model_latency"] = now() - st
        record.update(getattr(self.backend, "last_call_info", {}))
        self.model_time += record["model_latency"]
        self.rounds += 1
        self.cost += cost

        assistant_response = content if content is not None else ""
        for tc in tool_calls:
            assistant_response += f"\n\n```\n{tc.name}: {tc.arguments}\n```"
        if assistant_response:
            status.assistant_message(assistant_response)
        else:
            status.assistant_message("[ no response ]")

        # Run tool calls
        if tool_calls:
            st = now()
            tool_results, record["tool_calls"] = self.run_tools(tool_calls)
            record["tool_time"] = now() - st
            self.tool_time += record["tool_time"]

            env_response = "## Tool Responses:"
            for tr in tool_results:
                env_response += f"\n\n```\n{tr.name}: {tr.result}\n```\n"
            status.user_message(env_response)
            self.backend.append(tool_results)
            return len(tool_calls)
        else:
            return 0 # No tools run

    def metrics(self) -> dict:
        """Per-attempt totals; analysis/metrics.py recomputes these from round_log."""
        calls = [c for r in self.round_log for c in r.get("tool_calls", [])]
        prompt_tokens = [r["prompt_tokens"] for r in self.round_log if r.get("prompt_tokens") is not None]
        return {
            "prompt_tokens": sum(prompt_tokens),
            "completion_tokens": sum(r.get("completion_tokens") or 0 for r in self.round_log),
            "max_context_tokens": max(prompt_tokens, default=0),
            "tool_calls": len(calls),
            "tool_errors": sum(1 for c in calls if c["error"]),
            "nonzero_returncodes": sum(1 for c in calls if c["returncode"] not in (None, 0)),
            "command_timeouts": sum(1 for c in calls if c["timed_out"]),
            "tool_extract_errors": sum(1 for r in self.round_log if r.get("tool_extract_error")),
            "rounds_without_tool_calls": sum(1 for r in self.round_log
                                             if r["model_latency"] is not None and not r["tool_calls"]),
        }

    def save_log(self):
        end_time = self.end_time if self.end_time is not None else now()
        log = {
            "args": vars(self.args) if self.args is not None else None,
            "config": self.config,
            "messages": self.backend.get_timestamped_messages() if self.backend.messages else [],
            "challenge": self.challenge.challenge_info,
            "solved": self.environment.solved,
            "submitted_flags": self.environment.submitted_flags,
            "rounds": self.rounds,
            "cost": self.cost,
            "round_log": self.round_log,
            "metrics": self.metrics(),
            "network_internal": self.network_internal,
            "debug_log": status.debug_log,
            "start_time": self.start_time,
            "end_time": end_time,
            "complete": self.end_time is not None,
            "runtime": {
                "total": end_time - self.start_time,
                "tools": self.tool_time,
                "model": self.model_time
            },
            "exception_info": self.exception_info,
            "teardown_errors": self.teardown_errors,
            "finish_reason": self.finish_reason,
        }
        # Write to a temporary file first so a kill mid-write never leaves a truncated log
        tmp = self.logfile.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(log, indent=4, default=str))
        os.replace(tmp, self.logfile)

    def __exit__(self, exc_type, exc_value, traceback):
        self.end_time = now()
        if exc_type is not None and self.exception_info is None:
            self.exception_info = "".join(tb.format_exception(exc_type, exc_value, traceback))
            if self.finish_reason == "unknown":
                self.finish_reason = "terminated" if exc_type is Terminated else "error"
        # Save before teardown so a failing teardown cannot lose the attempt
        self.save_log()
        for step in (lambda: self.environment.teardown(exc_type, exc_value, traceback),
                     self.challenge.stop_challenge_container):
            try:
                step()
            except Exception:
                self.teardown_errors.append(tb.format_exc())
        self.save_log()
        status.print(f"Conversation saved to {self.logfile}")
        # A Terminated raised outside run() has been logged; don't re-raise it
        return exc_type is Terminated
