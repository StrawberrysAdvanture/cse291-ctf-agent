from argparse import Namespace
import re
from bs4 import BeautifulSoup
from .formatter import Formatter
from ..tools import Tool, ToolCall, ToolResult
from ..prompts import PromptManager
from typing import List, Optional, Tuple

TOOL_USE_START = '<function_calls>'
TOOL_USE_STOP = '</function_calls>'

class XMLFormatter(Formatter):
    NAME = 'xml'

    def __init__(self, tools: dict[str,Tool], prompt_manager: PromptManager):
        self.tools = tools
        self.prompt_manager = prompt_manager
        self.render_delimiters = False
        self.code_blocks = True
        self.delims_in_code_block = True

    def format_tool(self, tool : Tool) -> str:
        param_desc = []
        for name, info in tool.parameters.items():
            if info['required']:
                param = f'<parameter required="true">\n'
            else:
                param = f"<parameter>\n"
            param += f"<name>{name}</name>\n"
            param += f"<type>{info['type']}</type>\n"
            param += f"<description>{info['description']}</description>\n"
            param += f"</parameter>"
            param_desc.append(param)

        param_desc = "\n".join(param_desc)
        constructed_prompt = (
            "<tool_description>\n"
            f"<tool_name>{tool.name}</tool_name>\n"
            "<description>\n"
            f"{tool.description}\n"
            "</description>\n"
            "<parameters>\n"
            f"{param_desc}\n"
            "</parameters>\n"
            "</tool_description>"
        )
        return constructed_prompt

    def format_tools(self, tools : List[Tool]) -> str:
        return "<tools>\n" + '\n'.join([self.format_tool(tool) for tool in tools]) + "\n</tools>"

    def format_result(self, tr : ToolResult) -> str:
        res = "\n".join([f"<{key}>{value}</{key}>" for key, value in tr.result.items()])
        return f"<result>\n<tool_name>{tr.name}</tool_name>\n<call_id>{tr.id}</call_id>\n{res}\n</result>"

    def format_results(self, results : List[ToolResult]):
        return ("<function_results>\n" +
                '\n'.join([self.format_result(result) for result in results]) +
                "\n</function_results>")

    def extract_tool_calls(self, message) -> List[ToolCall]:
        soup = BeautifulSoup(message, "lxml")
        invocations = soup.find_all("invoke")
        tool_calls = []
        for invocation in invocations:
            if elem := invocation.find("tool_name"):
                name = elem.text
            else:
                name = "[not provided]"
            if self.lenient:
                # Issue I13: a reply cut off by the token cap can end in the middle of an
                # <invoke>; after the closing tags are re-added that fragment parses as an
                # invoke with no (or an empty) tool name. Drop it instead of running "Unknown tool".
                name = name.strip() if elem else ""
                if not name:
                    self.dropped_invokes += 1
                    continue
            if elem := invocation.find("call_id"):
                id = elem.text
            else:
                id = None
            # Defer parsing of parameters until we're actually ready to call the tools
            arguments = invocation
            tool_calls.append(ToolCall.create_unparsed(name, id, arguments))
        return tool_calls

    def format_tool_call(self, tool_call : ToolCall, placeholder : bool = False):
        param_str = "\n".join([
            f"<{key}>{value}</{key}>"
            for key, value in tool_call.function.parsed_arguments.items()
        ])
        if placeholder:
            param_str += '\n...'
        invoke_str = ""
        invoke_str += f"<invoke>\n"
        invoke_str += f"<tool_name>{tool_call.name}</tool_name>\n"
        invoke_str += f"<call_id>{tool_call.id}</call_id>\n"
        invoke_str += f"<parameters>\n"
        invoke_str += param_str + "\n"
        invoke_str += f"</parameters>\n"
        invoke_str += f"</invoke>"
        return invoke_str

    def format_tool_calls(self, tool_calls : List[ToolCall], placeholder : bool = False) -> str:
        return f"{TOOL_USE_START}\n" + "\n".join([
            self.format_tool_call(tc, placeholder)
            for tc in tool_calls
        ]) + f"\n{TOOL_USE_STOP}\n"

    # Set by the backend (issue I11). When true, a bare <invoke> counts as the start of the
    # tool calls and schema-style parameters (<parameter><name>x</name><value>v</value>) are accepted.
    lenient = False
    # Invokes without a tool name dropped in lenient mode (issue I13: truncated fragments).
    dropped_invokes = 0

    def extract_content(self, message) -> str:
        marker = re.escape(TOOL_USE_START)
        if self.lenient:
            marker = "(?:" + marker + "|<invoke>)"
        content = re.split(r'(```xml\s*)?' + marker, message)[0].strip()
        if not content: content = None
        return content

    def extract_params(self, tool : Tool, tc : ToolCall) -> ToolCall:
        invocation : BeautifulSoup = tc.function.arguments
        extracted_parameters = {}
        for param_name in tool.parameters:
            if elem := invocation.find(param_name):
                value = elem.text
                extracted_parameters[param_name] = value
        if self.lenient:
            # The model sometimes writes the tool-description schema inside the invoke:
            # <parameter><name>command</name><type>string</type><value>ls</value></parameter>,
            # or just <name>command</name> followed by <value>ls</value>.
            for name_elem in invocation.find_all("name"):
                name = name_elem.text.strip()
                if name not in tool.parameters or name in extracted_parameters:
                    continue
                container = name_elem.parent if name_elem.parent is not None and name_elem.parent.name == "parameter" else invocation
                value_elem = container.find("value") if container is not invocation else name_elem.find_next("value")
                if value_elem is not None:
                    extracted_parameters[name] = value_elem.text
        parsed_tc = ToolCall.create_parsed(tc.name, tc.id, extracted_parameters)
        self.validate_args(tool, parsed_tc)
        self.convert_args(tool, parsed_tc)
        return parsed_tc

    # Parameters that may hold a program's arguments when the model names the program as the
    # tool, in the order they are joined; shells get the command as written.
    REROUTE_PARAMS = ("path", "file", "filename", "command", "cmd", "args", "arguments")
    SHELLS = ("bash", "sh", "dash", "zsh", "shell")
    NOT_PARAMS = ("tool_name", "call_id", "parameters", "parameter", "name", "type", "value", "description")

    @staticmethod
    def known_tool_names() -> set:
        """Every tool defined in the code base, whatever the challenge's category offers."""
        return {getattr(cls, "NAME", "") for cls in Tool.__subclasses__()} - {""}

    def invoke_parameters(self, invocation) -> dict:
        """name -> text for an invoke's parameters: direct ``<x>v</x>`` elements and
        schema-style ``<parameter><name>x</name><value>v</value></parameter>`` pairs."""
        params = {}
        container = invocation.find("parameters") or invocation
        for child in container.find_all(recursive=False):
            if child.name not in self.NOT_PARAMS and child.text.strip():
                params.setdefault(child.name, child.text.strip())
        for name_elem in invocation.find_all("name"):
            key = name_elem.text.strip()
            holder = name_elem.parent if name_elem.parent is not None and name_elem.parent.name == "parameter" else None
            value_elem = holder.find("value") if holder is not None else name_elem.find_next("value")
            if key and value_elem is not None and value_elem.text.strip():
                params.setdefault(key, value_elem.text.strip())
        return params

    def reroute_unknown_call(self, tc: ToolCall, tools: dict) -> Optional[ToolCall]:
        """Lenient mode (issue I11): the model sometimes names a program as the tool
        (``<tool_name>python3</tool_name><parameters><command>x.py</command>``). When the name
        looks like a program and a parameter holds its arguments, return a parsed
        ``run_command`` call for ``<name> <arguments>``; otherwise None. A name that is a
        prefix or an extension of a real tool (``run``, ``dis``, ``run_commands``) is a
        damaged tool call, not a program, and stays unknown."""
        if not self.lenient or "run_command" not in tools:
            return None
        name = (tc.name or "").strip()
        lname = name.lower()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.+-]*", name):
            return None
        known = {t.lower() for t in self.known_tool_names()} | {t.lower() for t in tools}
        if any(t.startswith(lname) or lname.startswith(t) for t in known):
            return None
        invocation = tc.function.arguments
        if invocation is None or isinstance(invocation, str) or not hasattr(invocation, "find"):
            return None
        params = self.invoke_parameters(invocation)
        values = [params[p] for p in self.REROUTE_PARAMS if p in params]
        if not values:
            return None
        joined = " ".join(values)
        if lname in self.SHELLS or re.search(r"(^|\s|/)" + re.escape(name) + r"(\s|$)", joined):
            command = joined          # the program is already part of the command line
        else:
            command = f"{name} {joined}"
        parsed = {"command": command}
        timeout = params.get("timeout")
        if timeout:
            try:
                parsed["timeout"] = float(timeout)
            except ValueError:
                pass                  # an unparseable timeout is dropped, not fatal
        new_call = ToolCall.create_parsed("run_command", tc.id, parsed)
        self.validate_args(tools["run_command"], new_call)
        self.convert_args(tools["run_command"], new_call)
        return new_call

    def get_delimiters(self) -> Tuple[List[str],List[str]]:
        """Return the start and stop delimiters for this formatter."""
        return [ TOOL_USE_START ], [ TOOL_USE_STOP ]
