# Tool Guard for Python

**A prompt asks. Tool Guard decides.**

An AI agent calls tools: refund an order, send an email, change a record. Telling it in the prompt "never refund
more than 50 €" is a request; a well-built message can talk it out of it. Tool Guard puts the rule on the tool
itself, in your code, and checks it **before** the tool runs, whatever the model was convinced of.

- **Limits** on an argument: above the maximum, refused, or held for a human.
- **Human approval**: the call waits; a person approves (then it runs) or rejects.
- **Permissions**: the current user must hold a permission.
- **Rate limits**: at most N calls in a window, per user or for the whole tool.
- **Agents and their tools**: each declared agent may only call the tools it is given.
- **Stop button**: stop everything, every AI agent, one tool or one agent, at once.
- **Journal**: one line per decision (who, which tool, which arguments, what was decided and why).
- **Observe mode**: run as usual and write down what would have been blocked, to try it on a live agent first.

- **A gateway in front of MCP servers you did not write** (mail, drive, CRM, bank): the same rules from a config
  file, and every tool fingerprinted, so a tool that changes or whose description gives the model orders cannot
  be called until a person trusts it again.

No dependency. Python 3.10+. Works with the OpenAI Agents SDK, LangChain, MCP servers, or plain function calling.

## Install

```bash
pip install tool-guard
```

## In five lines

```python
from tool_guard import APPROVAL, Guard, limit

guard = Guard()

@guard.tool()
@limit("amount", max=50, above=APPROVAL, min=0)
def refund(order: str, amount: float) -> str:
    """Refund an order. The amount is in euros."""
    return payments.refund(order, amount)
```

`refund("A-12", 30)` runs. `refund("A-13", 180)` does not run; the function returns, for the model:

> This action needs a human approval (amount 180 is above the limit of 50). Request b3e77e28 has been sent to the
> team; it is NOT done yet. Do not retry; tell the user it is pending review.

The function keeps its name, signature and docstring, so agent frameworks build the same tool schema as before.

## The rules

```python
from tool_guard import APPROVAL, DENY, PER_TOOL, allowed_values, granted, limit, rate_limit, requires_approval

@granted("finance")                                # the current user must hold "finance"
@granted("edit", subject="account")                # ... "edit" on the value of the `account` argument
@limit("amount", max=1000)                         # above 1000: refused
@allowed_values("to", "*@mycompany.com")            # any other recipient: held for a human (otherwise=DENY: refused)
@limit("amount", max=50, above=APPROVAL, min=0)    # above 50: held for a human; below 0: refused
@rate_limit(5, per_seconds=60)                     # 5 calls a minute per user (per=PER_TOOL: for everyone)
@requires_approval("every wire is reviewed")       # every call waits for a human
```

A non-numeric, infinite or boolean value for a limited argument is refused. A permission that cannot be checked
(no `is_granted` given) is refused. A stop list that cannot be read refuses everything. Tool Guard fails closed.

## The guard

```python
from tool_guard import FileApprovals, FileCounter, FileJournal, FileStops, Guard

guard = Guard(
    approvals=FileApprovals("var/tool-guard/approvals"),   # one JSON file per waiting request
    journal=FileJournal("var/tool-guard/journal.jsonl"),    # one JSON line per decision
    stops=FileStops("var/tool-guard/stops.json"),
    counter=FileCounter("var/tool-guard/rate.json"),        # rate counters shared by every process
    current_user=lambda: request_user_id(),                 # who is asking, for permissions and rate limits
    is_granted=lambda permission, subject, user: acl.allows(user, permission, subject),
    agents={"support": ["refund", "lookup_order"]},         # each agent and the tools it may call
    max_pending_per_user=3,                                 # past that, a new request is refused, not queued
    expire_after=3600,                                      # a request nobody answered in an hour expires
    four_eyes=True,                                         # nobody approves their own request
    mode="enforce",                                         # or "observe"
)
```

Without arguments, everything is kept in memory: fine for tests and scripts.

Calls made by your own code, not by the model, pass `channel="code"`: agent roles and the "stop all agents"
switch do not apply to them, every other rule does.

```python
guard.call("refund", {"order": "A-1", "amount": 20}, channel="code", user="tom")   # raises ToolCallBlocked if blocked
```

## The human side

```bash
tool-guard --guard myapp.agent:guard approvals             # what waits
tool-guard --guard myapp.agent:guard approve b3e77e28 --by sophie
tool-guard --guard myapp.agent:guard reject b3e77e28 --by sophie
tool-guard --guard myapp.agent:guard stop tool refund --by cyril --reason "incident"
tool-guard --guard myapp.agent:guard resume tool refund
tool-guard --guard myapp.agent:guard rules                 # every tool and its rules
tool-guard --guard myapp.agent:guard journal -n 50
```

Or from your own admin page: `guard.approvals.all("pending")`, `guard.approve(id, by=...)`, `guard.reject(id, by=...)`,
`guard.stop(...)`, `guard.resume(...)`. Approving runs the tool with the arguments it was asked with, and stores
the result.

## With your framework

**OpenAI Agents SDK**

```python
from agents import Agent, function_tool
agent = Agent(name="support", tools=[function_tool(refund)])
```

**LangChain**

```python
from langchain_core.tools import tool
tools = [tool(refund)]
```

**MCP server** (Python SDK)

```python
server.tool()(refund)
```

Async tools work the same way (`async def`). To get an exception instead of the message, use
`@guard.tool(raise_blocked=True)` and catch `ToolCallBlocked` (`.decision`, `.held`).

## In front of an MCP server you did not write

Your agent uses a mail or drive connector somebody else wrote: you cannot put a rule in its code. Put the
gateway in front of it. The agent starts `tool-guard gateway` instead of the server; the gateway starts the real
server and relays everything, except the calls it refuses or holds.

```json
{
  "upstream": {"command": "npx", "args": ["-y", "some-mail-mcp-server@1.4.2"], "env": {"MAIL_TOKEN": "..."}},
  "state_dir": "var/tool-guard/mail",
  "default": "allow",
  "trust_on_first_use": true,
  "tools": {
    "send_email": {"allowed_values": {"to": ["*@mycompany.com"]}},
    "delete_*": {"deny": "deleting mail is not allowed"},
    "create_payment": {"limit": {"amount": {"max": 50, "above": "approval", "min": 0}}},
    "search": {"rate": {"limit": 30, "per_seconds": 60}}
  }
}
```

In the agent's MCP settings (Claude, Cursor, your own client), replace the server's command with:

```json
{"command": "tool-guard", "args": ["gateway", "--config", "/path/to/mail.json"]}
```

- A mail to `thief@gmail.com` waits for a human; `delete_email` is refused; nothing blocked reaches the server.
- Every tool is fingerprinted (name, description, input schema). A tool that appears or changes after it was
  trusted cannot be called; neither can a tool whose description gives the model orders ("ignore previous
  instructions", "do not tell the user", "send … to http…", "read ~/.ssh…"), even on first use.
- `default: "approval"` or `"deny"` makes every tool without a rule wait or refused.

```bash
tool-guard gateway --config mail.json tools              # every tool, trusted or blocked and why
tool-guard gateway --config mail.json trust send_email --by cyril
tool-guard gateway --config mail.json approvals          # approve / reject / stop / resume / journal, as above
```

An approved call runs in the gateway at its next check (every 2 seconds), and its result is stored with the request.

## What it is not

Tool Guard does not read the conversation and does not judge intent. It checks the actions an agent may take,
where they are executed. Keep good instructions in your prompt; put the rules that must hold in your code.

## License

MIT
