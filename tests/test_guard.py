import asyncio
import inspect
import tempfile
import unittest

from tool_guard import (APPROVAL, PER_TOOL, FileApprovals, FileCounter, FileJournal, FileStops, Guard, MemoryStops,
                       OBSERVE, ToolCallBlocked, granted, limit, rate_limit, requires_approval)

PAID = []


def make(**kw):
    guard = Guard(stops=MemoryStops(), **kw)

    @guard.tool()
    @limit("amount", max=50, above=APPROVAL, min=0)
    def refund(order: str, amount: float) -> str:
        """Refund an order."""
        PAID.append((order, amount))
        return f"refunded {amount}"

    @guard.tool()
    @limit("amount", max=1000)
    def transfer(amount: float) -> str:
        return "sent"

    return guard, refund, transfer


class GuardTest(unittest.TestCase):
    def setUp(self):
        PAID.clear()

    def test_within_the_rules_it_runs(self):
        guard, refund, _ = make()
        self.assertEqual("refunded 30", refund("A-1", 30))
        self.assertEqual([("A-1", 30)], PAID)
        self.assertEqual("allow", guard.journal.entries[-1]["verdict"])

    def test_above_the_limit_it_waits_for_a_human_then_runs_once_approved(self):
        guard, refund, _ = make()
        msg = refund("A-2", amount=180)
        self.assertIn("NOT done yet", msg)
        self.assertIn("amount 180 is above the limit of 50", msg)
        self.assertEqual([], PAID)
        waiting = guard.approvals.all("pending")
        self.assertEqual(1, len(waiting))
        done = guard.approve(waiting[0].id, by="sophie")
        self.assertEqual("approved", done.status)
        self.assertEqual([("A-2", 180)], PAID)
        with self.assertRaises(ValueError):
            guard.approve(waiting[0].id, by="sophie")

    def test_refused_rules_never_run(self):
        guard, refund, transfer = make()
        self.assertIn("Refused by policy: amount 2000 is above the limit of 1000", transfer(2000))
        self.assertIn("below the minimum of 0", refund("A", -5))
        self.assertIn('"amount" must be a number', refund("A", "lots"))
        self.assertIn('"amount" must be a number', refund("A", True))
        self.assertIn('"amount" must be a number', refund("A", float("nan")))
        self.assertEqual([], PAID)

    def test_the_signature_and_docstring_stay_for_agent_frameworks(self):
        _, refund, _ = make()
        self.assertEqual("refund", refund.__name__)
        self.assertEqual("Refund an order.", refund.__doc__)
        self.assertEqual(["order", "amount"], list(inspect.signature(refund).parameters))

    def test_raise_blocked_and_call(self):
        guard, _, _ = make()
        with self.assertRaises(ToolCallBlocked) as e:
            guard.call("refund", {"order": "B", "amount": 99})
        self.assertTrue(e.exception.held)
        self.assertEqual("refunded 10", guard.call("refund", {"order": "B", "amount": 10}))

    def test_a_flood_of_waiting_requests_is_refused(self):
        guard, refund, _ = make(max_pending_per_user=2)
        refund("x", 100)
        refund("x", 100)
        self.assertIn("2 requests already wait for a human", refund("x", 100))

    def test_permissions(self):
        guard = Guard(is_granted=lambda perm, subject, user: user == "admin" and perm == "ROLE_FINANCE")

        @guard.tool(user=lambda: CURRENT[0])
        @granted("ROLE_FINANCE")
        def close_account(account: str) -> str:
            return "closed"

        CURRENT[0] = "visitor"
        self.assertIn('permission "ROLE_FINANCE" not granted', close_account("C1"))
        CURRENT[0] = "admin"
        self.assertEqual("closed", close_account("C1"))
        bare = Guard()
        f = bare.tool()(granted("X")(lambda a: a))
        self.assertIn("cannot be checked", f(1))

    def test_rate_limit_per_user_and_per_tool(self):
        guard = Guard()

        @guard.tool()
        @rate_limit(2, per_seconds=60)
        def ping() -> str:
            return "pong"

        self.assertEqual("pong", ping())
        self.assertEqual("pong", ping())
        self.assertIn("more than 2 calls in 1 minute", ping())
        with tempfile.TemporaryDirectory() as d:
            c = FileCounter(d + "/rate.json")
            self.assertTrue(c.consume("k", 1, 60, at=1000.0))
            self.assertFalse(FileCounter(d + "/rate.json").consume("k", 1, 60, at=1010.0), "shared across processes")
            self.assertTrue(c.consume("k", 1, 60, at=1061.0), "a new window")
        tool_wide = Guard()
        f = tool_wide.tool(user=lambda: CURRENT[0])(rate_limit(1, per_seconds=60, per=PER_TOOL)(lambda: "ok"))
        CURRENT[0] = "a"
        self.assertEqual("ok", f())
        CURRENT[0] = "b"
        self.assertIn("more than 1 calls", f())

    def test_requires_approval(self):
        guard = Guard()
        f = guard.tool("wipe")(requires_approval("wiping is reviewed")(lambda: "wiped"))
        self.assertIn("This action needs a human approval (wiping is reviewed)", f())

    def test_stop_button(self):
        guard, refund, transfer = make()
        guard.stop("tool", "refund", by="cyril", reason="incident")
        self.assertIn('tool "refund" is stopped (incident)', refund("A", 1))
        self.assertEqual("sent", transfer(1))
        guard.stop("agents", by="cyril")
        self.assertIn("AI agents are stopped", transfer(1))
        self.assertEqual("sent", guard.call("transfer", {"amount": 1}, channel="code"), "a person or the app's own code is no agent")
        guard.resume("agents")
        guard.resume("tool", "refund")
        self.assertEqual("refunded 1", refund("A", 1))

    def test_agents_and_their_tools(self):
        guard = Guard(agents={"support": ["refund"]})
        f = guard.tool("refund")(lambda amount: "ok")
        g = guard.tool("transfer")(lambda amount: "ok")
        self.assertEqual("ok", guard.call("refund", {"amount": 1}, agent="support"))
        with self.assertRaises(ToolCallBlocked):
            guard.call("transfer", {"amount": 1}, agent="support")
        with self.assertRaises(ToolCallBlocked):
            guard.call("refund", {"amount": 1})

    def test_observe_mode_lets_it_run_and_writes_what_it_would_have_done(self):
        guard, refund, _ = make(mode=OBSERVE)
        self.assertEqual("refunded 500", refund("A", 500))
        self.assertEqual("pending", guard.journal.entries[-1]["would"])

    def test_four_eyes_and_expiry(self):
        guard, refund, _ = make(four_eyes=True, current_user=lambda: "tom")
        refund("A", 100)
        a = guard.approvals.all("pending")[0]
        with self.assertRaises(PermissionError):
            guard.approve(a.id, by="tom")
        guard.reject(a.id, by="sophie")
        self.assertEqual("rejected", guard.approvals.get(a.id).status)
        g2, refund2, _ = make(expire_after=0)
        refund2("A", 100)
        with self.assertRaises(ValueError):
            g2.approve(g2.approvals.all("pending")[0].id, by="x")

    def test_async_tools(self):
        guard = Guard()

        @guard.tool()
        @limit("amount", max=10)
        async def pay(amount: float) -> str:
            return "paid"

        self.assertEqual("paid", asyncio.run(pay(5)))
        self.assertIn("Refused by policy", asyncio.run(pay(50)))

    def test_file_stores_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            def build():
                return make(approvals=FileApprovals(d + "/approvals"), journal=FileJournal(d + "/journal.jsonl"))
            guard, refund, _ = build()
            refund("A-9", 75)
            again, _, _ = build()
            waiting = again.approvals.all("pending")
            self.assertEqual(75, waiting[0].arguments["amount"])
            again.approve(waiting[0].id, by="sophie")
            self.assertEqual([("A-9", 75)], PAID)
            self.assertEqual(["call", "approval"], [e["event"] for e in again.journal.tail()])
            self.assertIsNone(again.approvals.get("../etc"))
            stops = FileStops(d + "/stops.json")
            again.stops = stops
            again.stop("all", by="c")
            self.assertEqual(1, len(FileStops(d + "/stops.json").all()))

    def test_rules_are_listed(self):
        guard, _, _ = make()
        self.assertEqual({"refund": ["limit 0 ≤ amount ≤ 50, above: held for a human"],
                          "transfer": ["limit amount ≤ 1000, above: refused"]}, guard.rules())


    def test_allowed_values(self):
        from tool_guard import allowed_values, DENY
        guard = Guard()
        send = guard.tool()(allowed_values("to", "*@mycompany.com")(lambda to, body="": "sent"))
        self.assertEqual("sent", send("Anna@MyCompany.com"))
        self.assertIn('to "x@gmail.com" is not among the allowed values', send("x@gmail.com"))
        self.assertIn("NOT done yet", send(["a@mycompany.com", "x@gmail.com"]))
        strict = guard.tool("strict")(allowed_values("to", "*@mycompany.com", otherwise=DENY)(lambda to: "sent"))
        self.assertIn("Refused by policy", strict(None))


CURRENT = ["visitor"]

if __name__ == "__main__":
    unittest.main()
