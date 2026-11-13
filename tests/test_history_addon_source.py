"""Static checks on the C# add-on's GET /history (it cannot be compiled or run here): the tick options are present and the handler stays
read-only. A compile in NinjaTrader is still needed; see the PR text."""
import os
import re
import unittest

SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "NtStatusAddOn.cs"), encoding="utf-8").read()


def method_body(name):
    i = SRC.index(f"private string {name}(")
    j = SRC.index("{", i)
    depth = 0
    for k in range(j, len(SRC)):
        depth += SRC[k] == "{"
        depth -= SRC[k] == "}"
        if depth == 0:
            return SRC[j:k + 1]
    raise AssertionError("unbalanced braces")


class HistoryAddOnSourceTest(unittest.TestCase):
    def test_history_is_routed_for_get_only(self):
        handle = method_body("Handle") if "private string Handle(" in SRC else SRC[SRC.index("private void Handle("):SRC.index("private string HandlePost(")]
        get_branch = handle[handle.index('HttpMethod != "GET"'):]
        self.assertIn('"/history") body = BuildHistory', get_branch)
        self.assertNotIn("BuildHistory", handle[:handle.index('HttpMethod == "POST"')] if 'HttpMethod == "POST"' in handle else "")
        self.assertNotIn("BuildHistory", method_body("HandlePost"))

    def test_history_touches_no_orders_accounts_or_strategies(self):
        body = method_body("BuildHistory")
        for banned in ("Account", "Order", "Strategy", "Submit", "Cancel", "Flatten", "Kill", "Enable", "Disable", "Execution"):
            self.assertIsNone(re.search(r"\b" + banned, body), banned)

    def test_tick_options_are_parsed_and_echoed(self):
        body = method_body("BuildHistory")
        for needle in ('QueryString["price"]', 'QueryString["merge"]', 'QueryString["fmt"]', '"offset"', '"limit"',
                       "MarketDataType = mdt", "MergePolicy.MergeNonBackAdjusted", "MergePolicy.DoNotMerge",
                       '\\"price\\"', '\\"total\\"', '\\"offset\\"', '\\"truncated\\"', "yyyy-MM-ddTHH:mm:ss.fffffff"):
            self.assertIn(needle, body, needle)

    def test_default_reply_is_unchanged_for_old_clients(self):
        body = method_body("BuildHistory")
        self.assertIn('?? "Last"', body)
        self.assertIn('?? "backadjusted"', body)
        self.assertIn('"offset", 0, 0', body)
        self.assertIn('"limit", 250000, 1, 500000', body)

    def test_loaded_session_is_kept_between_pages_and_always_released(self):
        body = method_body("BuildHistory")
        self.assertIn("histKey == key", body)                                   # a page of the same request reuses the loaded bars
        for part in ("instrument.FullName", "bpt", "from.ToString", "to.ToString", "mdt", "mergeName"):
            self.assertIn(part, body[body.index("string key ="):body.index("lock (histLock)")], part)
        self.assertIn("keep = truncated", body)                                 # kept only while more pages remain
        self.assertIn("else DropHistory();", body)                              # last page (or an exception) releases it at once
        self.assertIn("if (!loaded) fresh.Dispose();", body)                    # a failed / timed-out load is never cached
        self.assertIn("histTimer.Change((HistKeepSeconds + 1) * 1000", body)    # an abandoned request is released by the idle timer
        self.assertIn("lock (histLock) { DropHistory();", SRC[SRC.index("private void Stop()"):SRC.index("private void Loop()")])
        self.assertIn('\\"cached\\"', body)

    def test_quotes_option_is_opt_in(self):
        body = method_body("BuildHistory")
        self.assertIn('QueryString["quotes"] == "1"', body)
        self.assertIn("if (quotes) sb.Append", body)
        self.assertIn("GetBid(i)", body)
        self.assertIn("GetAsk(i)", body)


if __name__ == "__main__":
    unittest.main()
