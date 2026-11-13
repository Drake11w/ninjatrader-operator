// NtStatusAddOn.cs - status server plus risk-reducing controls for NinjaTrader 8.
// GET  http://127.0.0.1:8765: /status (accounts, P&L, positions), /orders (working),
//      /executions?limit=N (fills), /strategies, /health (heartbeat + connection status), /killswitch.
// POST /flatten?account=X[&instrument=Y]   flatten positions (also cancels their orders)
//      /cancel_all?account=X[&instrument=Y] cancel working orders
//      /killswitch/arm?account=X&max_loss=N flatten + cancel (latched) once P&L since arming <= -N
//      /killswitch/disarm
// Controls only REDUCE risk: there is no order entry and no modify call.
// Guards: only accounts named "Sim*" or listed (exact names, comma-separated) in env NT_OPERATOR_ALLOWED_ACCOUNTS,
// unless env NT_OPERATOR_ALLOW_LIVE=1; if env NT_OPERATOR_TOKEN is set,
// POSTs need a matching X-NT-Token header. The kill switch lives in memory: restarting NT disarms it.
// NOT yet compiled or run in NT8: Account.Flatten / Account.Cancel usage is unverified.
#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Net;
using System.Reflection;
using System.Text;
using System.Threading;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
#endregion

namespace NinjaTrader.NinjaScript.AddOns
{
    public class NtStatusAddOn : AddOnBase
    {
        private const int Port = 8765;
        private HttpListener listener;
        private Thread worker;
        private volatile bool running;
        private DateTime startedUtc = DateTime.UtcNow;

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "NtStatusAddOn";
            }
            else if (State == State.Active)
            {
                Start();
            }
            else if (State == State.Terminated)
            {
                Stop();
            }
        }

        private void Start()
        {
            try
            {
                listener = new HttpListener();
                listener.Prefixes.Add("http://127.0.0.1:" + Port + "/");
                listener.Start();
                running = true;
                worker = new Thread(Loop) { IsBackground = true, Name = "NtStatusAddOn" };
                worker.Start();
                Print("NtStatusAddOn: listening on http://127.0.0.1:" + Port + "/status");
            }
            catch (Exception ex)
            {
                Print("NtStatusAddOn: failed to start: " + ex.Message);
            }
        }

        private void Stop()
        {
            running = false;
            DisarmKill();
            lock (histLock) { DropHistory(); if (histTimer != null) { histTimer.Dispose(); histTimer = null; } }
            try { if (listener != null) { listener.Stop(); listener.Close(); } } catch { }
            listener = null;
        }

        private void Loop()
        {
            while (running)
            {
                try
                {
                    HttpListenerContext ctx = listener.GetContext();
                    Handle(ctx);
                }
                catch (Exception)
                {
                    if (!running) return;
                }
            }
        }

        private void Handle(HttpListenerContext ctx)
        {
            int code = 200;
            string body;
            try
            {
                if (ctx.Request.HttpMethod == "POST") body = HandlePost(ctx, out code);
                else if (ctx.Request.HttpMethod != "GET") { code = 405; body = "{\"error\":\"method not allowed\"}"; }
                else if (ctx.Request.Url.AbsolutePath == "/killswitch") body = BuildKill();
                else if (ctx.Request.Url.AbsolutePath == "/status") body = BuildStatus();
                else if (ctx.Request.Url.AbsolutePath == "/orders") body = BuildOrders();
                else if (ctx.Request.Url.AbsolutePath == "/executions") body = BuildExecutions(QueryInt(ctx, "limit", 200));
                else if (ctx.Request.Url.AbsolutePath == "/strategies") body = BuildStrategies();
                else if (ctx.Request.Url.AbsolutePath == "/strategy/rows") body = DoStrategyRows(null, false, null);
                else if (ctx.Request.Url.AbsolutePath == "/health") body = BuildHealth();
                else if (ctx.Request.Url.AbsolutePath == "/history") body = BuildHistory(ctx, out code);
                else if (ctx.Request.Url.AbsolutePath == "/backtest/job") body = BuildBacktestJob(QueryInt(ctx, "id", 0), ctx.Request.QueryString["trades"] == "1");
                else if (ctx.Request.Url.AbsolutePath == "/debug/types") body = BuildTypes(ctx.Request.QueryString["filter"], ctx.Request.QueryString["members"] == "1");
                else { code = 404; body = "{\"error\":\"not found\"}"; }
            }
            catch (Exception ex)
            {
                code = 500;
                body = "{\"error\":" + Q(ex.Message) + "}";
            }
            byte[] bytes = Encoding.UTF8.GetBytes(body);
            ctx.Response.StatusCode = code;
            ctx.Response.ContentType = "application/json";
            ctx.Response.ContentLength64 = bytes.Length;
            ctx.Response.OutputStream.Write(bytes, 0, bytes.Length);
            ctx.Response.OutputStream.Close();
        }

        // ---- Controls (risk-reducing only) ----

        private System.Threading.Timer killTimer;
        private readonly object killLock = new object();
        private string killAccount = "";
        private double killMaxLoss, killBaseline, killLastPnl;
        private bool killArmed, killTripped;
        private DateTime killArmedUtc, killTrippedUtc;
        private int killActions;

        private static bool AccountAllowed(string name)
        {
            if (name.StartsWith("Sim", StringComparison.OrdinalIgnoreCase)
                || Environment.GetEnvironmentVariable("NT_OPERATOR_ALLOW_LIVE") == "1") return true;
            string list = Environment.GetEnvironmentVariable("NT_OPERATOR_ALLOWED_ACCOUNTS");
            if (string.IsNullOrEmpty(list)) return false;
            foreach (string part in list.Split(','))
                if (part.Trim() == name) return true;
            return false;
        }

        // Strategy starts on real accounts require a separate, exact live-small allowlist and unlock.
        // NT_OPERATOR_ALLOW_LIVE is intentionally not accepted here.
        private static bool LiveSmallStrategyAccountAllowed(string name)
        {
            if (Environment.GetEnvironmentVariable("NT_OPERATOR_ALLOW_LIVE_SMALL") != "1") return false;
            string list = Environment.GetEnvironmentVariable("NT_OPERATOR_LIVE_SMALL_ACCOUNTS");
            if (string.IsNullOrEmpty(list)) return false;
            foreach (string part in list.Split(','))
                if (part.Trim() == name) return true;
            return false;
        }

        private static Account FindAccount(string name)
        {
            lock (Account.All)
            {
                foreach (Account a in Account.All) if (a.Name == name) return a;
            }
            return null;
        }

        // Read-only reflection listing (type full names, optionally members) from the NinjaTrader assemblies, to find out
        // what a backtest entry point looks like. GET /debug/types?filter=Backtest&members=1
        private static string BuildTypes(string filter, bool members)
        {
            if (string.IsNullOrEmpty(filter)) return Err("filter is required");
            var sb = new StringBuilder("{\"types\":[");
            bool first = true;
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                string an = asm.GetName().Name;
                if (an != "NinjaTrader.Gui" && an != "NinjaTrader.Core") continue;
                Type[] types;
                try { types = asm.GetTypes(); } catch (ReflectionTypeLoadException e) { types = e.Types.Where(t => t != null).ToArray(); }
                foreach (Type t in types)
                {
                    if (t.FullName == null || t.FullName.IndexOf(filter, StringComparison.OrdinalIgnoreCase) < 0) continue;
                    if (!first) sb.Append(","); first = false;
                    sb.Append("{\"type\":" + Q(an + ": " + t.FullName + " : " + (t.BaseType == null ? "" : t.BaseType.Name)));
                    if (members)
                    {
                        sb.Append(",\"members\":[");
                        bool f2 = true;
                        foreach (MemberInfo m in t.GetMembers(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
                        {
                            if (m.Name.StartsWith("get_") || m.Name.StartsWith("set_") || m.Name.StartsWith("add_") || m.Name.StartsWith("remove_") || m.MemberType == MemberTypes.NestedType) continue;
                            string sig = m.MemberType + " " + m.Name; if (m is PropertyInfo) sig += " : " + ((PropertyInfo)m).PropertyType.FullName + (((PropertyInfo)m).CanWrite ? " rw" : " r"); if (m is EventInfo) sig += " : " + ((EventInfo)m).EventHandlerType.FullName;
                            MethodBase mb = m as MethodBase;
                            if (mb != null) sig += "(" + string.Join(",", mb.GetParameters().Select(pp => pp.ParameterType.FullName + " " + pp.Name)) + " -> " + (m is MethodInfo ? ((MethodInfo)m).ReturnType.Name : "") + ")";
                            if (!f2) sb.Append(","); f2 = false;
                            sb.Append(Q(sig));
                        }
                        sb.Append("]");
                    }
                    sb.Append("}");
                }
            }
            return sb.Append("]}").ToString();
        }

        private static string Err(string msg) { return "{\"ok\":false,\"error\":" + Q(msg) + "}"; }

        private string HandlePost(HttpListenerContext ctx, out int code)
        {
            code = 200;
            string token = Environment.GetEnvironmentVariable("NT_OPERATOR_TOKEN");
            if (!string.IsNullOrEmpty(token) && ctx.Request.Headers["X-NT-Token"] != token)
            { code = 401; return Err("bad or missing X-NT-Token"); }

            string path = ctx.Request.Url.AbsolutePath;
            if (path == "/killswitch/disarm") { DisarmKill(); return BuildKill(); }
            if (path == "/backtest/run") return DoBacktestRun(ctx, out code);

            string name = ctx.Request.QueryString["account"];
            string instrument = ctx.Request.QueryString["instrument"];
            if (string.IsNullOrEmpty(name)) { code = 400; return Err("account is required"); }
            bool strategyPath = path == "/strategy/enable" || path == "/strategy/disable" || path == "/strategy/purge";
            bool simAccount = name.StartsWith("Sim", StringComparison.OrdinalIgnoreCase);
            bool accountAllowed = strategyPath
                ? (simAccount || LiveSmallStrategyAccountAllowed(name))
                : AccountAllowed(name);
            if (!accountAllowed) { code = 403; return Err(strategyPath
                ? "strategy start/stop requires a Sim* account or NT_OPERATOR_ALLOW_LIVE_SMALL=1 plus exact NT_OPERATOR_LIVE_SMALL_ACCOUNTS membership"
                : "account '" + name + "' is not a Sim* account or in NT_OPERATOR_ALLOWED_ACCOUNTS; live control is locked"); }
            Account a = FindAccount(name);
            if (a == null) { code = 404; return Err("unknown account '" + name + "'"); }

            if (path == "/strategy/enable") return DoStrategyEnable(ctx, a, out code);
            if (path == "/strategy/disable") return DoStrategyDisable(ctx, a, out code);
            if (path == "/strategy/purge")
            {
                if (!StrategyStartAllowed()) { code = 403; return Err("strategy control is locked: set NT_OPERATOR_ALLOW_STRATEGY_START=1 in NinjaTrader's environment"); }
                return DoStrategyRows(a, ctx.Request.QueryString["confirm"] == "1", ctx.Request.QueryString["name"],
                    ctx.Request.QueryString["stale"] == "1");
            }
            if (path == "/flatten") return DoFlatten(a, instrument);
            if (path == "/cancel_all") return DoCancelAll(a, instrument);
            if (path == "/killswitch/arm")
            {
                double max;
                if (!double.TryParse(ctx.Request.QueryString["max_loss"], NumberStyles.Float, CultureInfo.InvariantCulture, out max) || max <= 0)
                { code = 400; return Err("max_loss must be a positive number"); }
                ArmKill(a, max);
                return BuildKill();
            }
            code = 404;
            return Err("not found");
        }

        // ---- Strategy start/stop (EXPERIMENTAL, off by default) -------------------------------------------------
        // POST /strategy/enable?account=Sim101&class=OrbEvalMNQ&name=<unique name>&instrument=MNQ%2012-26
        //   body: one "Property=urlencoded value" per line (the spec's ninjascript.inputs).
        // POST /strategy/disable?account=Sim101&name=<name>
        // Uses the same private routines the Strategies tab uses (StrategiesGrid.StrategySetDefaults / StrategyAdd /
        // StrategyEnable / StrategyDisable), found by reflection and pinned to the NT8 build they were found in.
        // Needs NT_OPERATOR_ALLOW_STRATEGY_START=1 in NinjaTrader's environment, a Sim* or allow-listed account
        // (checked in HandlePost), and refuses a name that already exists on the account.
        // Verified on NT 8.1.8.4 / Sim101 (2026-11-06) only with skip_defaults=1: StrategySetDefaults itself throws a NullReferenceException
        // when called from here. This sets bars, hours, instrument and DaysToLoad itself. May break on any NinjaTrader update.
        private const string TestedGuiVersion = "8.1.8.4";

        private static bool StrategyStartAllowed() { return Environment.GetEnvironmentVariable("NT_OPERATOR_ALLOW_STRATEGY_START") == "1"; }

        private static Type FindStrategyType(string cls)
        {
            foreach (Assembly asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type t = null;
                try { t = asm.GetType("NinjaTrader.NinjaScript.Strategies." + cls, false); } catch { }
                if (t != null && typeof(StrategyBase).IsAssignableFrom(t)) return t;
            }
            return null;
        }

        private static Dictionary<string, string> ReadBody(HttpListenerContext ctx)
        {
            var d = new Dictionary<string, string>();
            string text;
            using (var r = new StreamReader(ctx.Request.InputStream, Encoding.UTF8)) text = r.ReadToEnd();
            foreach (string raw in text.Split('\n'))
            {
                string line = raw.Trim();
                int eq = line.IndexOf('=');
                if (eq <= 0) continue;
                d[line.Substring(0, eq)] = Uri.UnescapeDataString(line.Substring(eq + 1));
            }
            return d;
        }

        private static object ConvertTo(Type t, string v)
        {
            if (t == typeof(string)) return v;
            if (t.IsEnum) return Enum.Parse(t, v, true);
            if (t == typeof(bool)) return bool.Parse(v);
            return Convert.ChangeType(v, t, CultureInfo.InvariantCulture);
        }

        private static StrategyBase FindStrategy(Account a, string name)
        {
            lock (a.Strategies)
                foreach (StrategyBase st in a.Strategies) if (st.Name == name) return st;
            return null;
        }

        private static MethodInfo GridMethod(out string problem, string method)
        {
            problem = null;
            Assembly gui = AppDomain.CurrentDomain.GetAssemblies().FirstOrDefault(x => x.GetName().Name == "NinjaTrader.Gui");
            if (gui == null) { problem = "NinjaTrader.Gui not loaded"; return null; }
            if (gui.GetName().Version.ToString() != TestedGuiVersion)
            { problem = "NinjaTrader.Gui is " + gui.GetName().Version + "; strategy start was only checked against " + TestedGuiVersion; return null; }
            Type grid = gui.GetType("NinjaTrader.Gui.NinjaScript.StrategiesGrid", false);
            MethodInfo m = grid == null ? null : grid.GetMethod(method, BindingFlags.NonPublic | BindingFlags.Public | BindingFlags.Static);
            if (m == null) problem = "StrategiesGrid." + method + " not found in this NinjaTrader build";
            return m;
        }

        private string DoStrategyEnable(HttpListenerContext ctx, Account a, out int code)
        {
            code = 200;
            if (!StrategyStartAllowed()) { code = 403; return Err("strategy start is locked: set NT_OPERATOR_ALLOW_STRATEGY_START=1 in NinjaTrader's environment"); }
            if (!a.Name.StartsWith("Sim", StringComparison.OrdinalIgnoreCase)
                && string.IsNullOrEmpty(Environment.GetEnvironmentVariable("NT_OPERATOR_TOKEN")))
            { code = 403; return Err("live-small strategy start requires NT_OPERATOR_TOKEN in both the MCP server and NinjaTrader environments"); }
            string cls = ctx.Request.QueryString["class"], name = ctx.Request.QueryString["name"], instrument = ctx.Request.QueryString["instrument"];
            if (string.IsNullOrEmpty(cls) || string.IsNullOrEmpty(name) || string.IsNullOrEmpty(instrument)) { code = 400; return Err("class, name and instrument are required"); }
            Type type = FindStrategyType(cls);
            if (type == null) { code = 404; return Err("strategy class '" + cls + "' not found: compile it first (F5 in the NinjaScript Editor)"); }
            if (FindStrategy(a, name) != null) { code = 409; return Err("a strategy named '" + name + "' already exists on " + a.Name); }
            Dictionary<string, string> inputs = ReadBody(ctx);
            bool skipDefaults = ctx.Request.QueryString["skip_defaults"] == "1";

            string problem;
            MethodInfo setDefaults = GridMethod(out problem, "StrategySetDefaults");
            if (skipDefaults) { setDefaults = null; problem = null; }
            MethodInfo add = problem == null ? GridMethod(out problem, "StrategyAdd") : null;
            MethodInfo enable = problem == null ? GridMethod(out problem, "StrategyEnable") : null;
            if (problem != null) { code = 501; return Err(problem); }

            string error = null;
            string state = "";
            string step = "start";
            StrategyBase added = null;
            string cleanup = "";
            System.Windows.Application.Current.Dispatcher.Invoke(new Action(() =>
            {
                try
                {
                    step = "create";
                    var st = (StrategyBase)Activator.CreateInstance(type);
                    step = "state_set_defaults";
                    st.SetState(State.SetDefaults);
                    step = "set_defaults";
                    if (setDefaults != null) setDefaults.Invoke(null, new object[] { st, true });
                    step = "set_inputs";
                    foreach (var kv in inputs)
                    {
                        PropertyInfo pi = type.GetProperty(kv.Key, BindingFlags.Public | BindingFlags.Instance);
                        if (pi == null || !pi.CanWrite) { error = "unknown or read-only input '" + kv.Key + "' on " + cls; return; }
                        pi.SetValue(st, ConvertTo(pi.PropertyType, kv.Value), null);
                    }
                    step = "set_name_instrument_account";
                    PropertyInfo pn = typeof(StrategyBase).GetProperty("Name");
                    pn.SetValue(st, ConvertTo(pn.PropertyType, name), null);
                    PropertyInfo pins = typeof(StrategyBase).GetProperty("InstrumentOrInstrumentList");
                    pins.SetValue(st, ConvertTo(pins.PropertyType, instrument), null);
                    st.Account = a;
                    step = "resolve_instrument";
                    Instrument inst = Instrument.GetInstrument(instrument);
                    if (inst == null) { error = "instrument '" + instrument + "' not found"; return; }
                    step = "set_instrument_bars_hours";
                    st.Instrument = inst;
                    st.Instruments = new Instrument[] { inst };
                    var bp = new BarsPeriod();
                    bp.BarsPeriodType = BarsPeriodType.Minute;
                    bp.Value = 1;
                    st.BarsPeriod = bp;
                    st.BarsPeriods = new BarsPeriod[] { bp };
                    st.TradingHours = inst.MasterInstrument.TradingHours;
                    if (st.DaysToLoad < 30) st.DaysToLoad = 30;
                    step = "strategy_add";
                    add.Invoke(null, new object[] { st });
                    added = st;
                    step = "strategy_enable";
                    enable.Invoke(null, new object[] { st, null, null });
                    state = st.State.ToString();
                }
                catch (Exception ex)
                {
                    Exception e = ex.InnerException ?? ex;
                    string where = e.StackTrace == null ? "" : e.StackTrace.Split((char)10)[0].Trim();
                    error = "at step " + step + ": " + e.GetType().Name + ": " + e.Message + " | " + where;
                    if (added != null)
                    {
                        try
                        {
                            string rp;
                            MethodInfo rm = GridMethod(out rp, "StrategyRemove");
                            if (rm != null) { rm.Invoke(null, new object[] { added }); cleanup = " (added strategy removed)"; }
                            else cleanup = " (could not remove the added strategy: " + rp + ")";
                        }
                        catch (Exception ex2) { cleanup = " (removing the added strategy failed: " + (ex2.InnerException ?? ex2).Message + ")"; }
                    }
                }
            }));
            if (error != null) { code = 500; return Err("enable failed " + error + cleanup); }
            return "{\"ok\":true,\"action\":\"strategy_enable\",\"account\":" + Q(a.Name) + ",\"name\":" + Q(name) + ",\"class\":" + Q(cls)
                + ",\"state\":" + Q(state) + ",\"inputs_set\":" + inputs.Count.ToString(CultureInfo.InvariantCulture) + ",\"timestamp\":" + Ts(DateTime.UtcNow) + "}";
        }

        private string DoStrategyDisable(HttpListenerContext ctx, Account a, out int code)
        {
            code = 200;
            if (!StrategyStartAllowed()) { code = 403; return Err("strategy control is locked: set NT_OPERATOR_ALLOW_STRATEGY_START=1 in NinjaTrader's environment"); }
            string name = ctx.Request.QueryString["name"];
            StrategyBase st = string.IsNullOrEmpty(name) ? null : FindStrategy(a, name);
            if (st == null) { code = 404; return Err("no strategy named '" + name + "' on " + a.Name); }
            string problem;
            MethodInfo disable = GridMethod(out problem, "StrategyDisable");
            if (problem != null) { code = 501; return Err(problem); }
            string error = null;
            System.Windows.Application.Current.Dispatcher.Invoke(new Action(() =>
            {
                try
                {
                    if (ctx.Request.QueryString["remove"] == "1")
                    {
                        string rp;
                        MethodInfo rm = GridMethod(out rp, "StrategyRemove");
                        if (rm == null) { error = rp; return; }
                        rm.Invoke(null, new object[] { st });
                    }
                    else disable.Invoke(null, new object[] { st });
                }
                catch (Exception ex) { error = (ex.InnerException ?? ex).Message; }
            }));
            if (error != null) { code = 500; return Err("disable failed: " + error); }
            return "{\"ok\":true,\"action\":\"strategy_disable\",\"account\":" + Q(a.Name) + ",\"name\":" + Q(name) + ",\"timestamp\":" + Ts(DateTime.UtcNow) + "}";
        }

        // ---- Stale strategy rows and purge (EXPERIMENTAL; NOT verified) ---------------------------------------------
        // NinjaTrader drops stopped instances from Account.Strategies, but the Strategies tab keeps their rows, so
        // /strategies and disable cannot see them. StrategyBase.All (static, no UI thread needed) still lists every
        // strategy object, so this reads that.
        //   GET  /strategy/rows                         lists every strategy object NinjaTrader still holds (read-only)
        //   POST /strategy/purge?account=X              dry run: lists the ones it WOULD remove
        //   POST /strategy/purge?account=X&confirm=1    removes them (optional &name=<exact name> for one)
        //   POST /strategy/purge?account=X&confirm=1&name=<exact name>&stale=1
        //                                               also removes a stopped (Configure) row with that exact name, flat. The launcher
        //                                               uses this to clear a leg's old row right before it enables the leg again, so the
        //                                               tab does not collect a new pair of rows every day.
        // Only a Finalized or Terminated (already stopped), flat strategy is removed; Realtime never. A Configure object is removed only
        // when it duplicates a running strategy (same name and account) and is named with &name=. Same guards as
        // /strategy/disable. Removal calls StrategiesGrid.StrategyRemove (what right-click Remove uses) on the main UI
        // dispatcher with a bounded wait, so a busy window can never hang the HTTP thread.
        private static List<StrategyBase> AllStrategies()
        {
            var list = new List<StrategyBase>();
            var all = StrategyBase.All;
            lock (all) { foreach (StrategyBase st in all) list.Add(st); }
            return list;
        }

        private static string SafeName(StrategyBase o) { try { return o.Name; } catch { return null; } }
        private static string SafeAccount(StrategyBase o) { try { return o.Account != null ? o.Account.Name : ""; } catch { return null; } }
        private static string SafeState(StrategyBase o) { try { return o.State.ToString(); } catch { return null; } }

        private string DoStrategyRows(Account account, bool remove, string onlyName, bool staleNamed = false)
        {
            string problem = null;
            MethodInfo rm = null;
            if (account != null && remove) rm = GridMethod(out problem, "StrategyRemove");
            if (problem != null) return Err(problem);
            var rows = new List<string>();
            int removed = 0, would = 0, errors = 0;
            System.Windows.Threading.Dispatcher main = null;
            try
            {
                Assembly core = AppDomain.CurrentDomain.GetAssemblies().FirstOrDefault(x => x.GetName().Name == "NinjaTrader.Core");
                Type gt = core == null ? null : core.GetType("NinjaTrader.Core.Globals", false);
                PropertyInfo mp = gt == null ? null : gt.GetProperty("MainThreadDispatcher", BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic);
                main = mp == null ? null : mp.GetValue(null, null) as System.Windows.Threading.Dispatcher;
            }
            catch { }
            if (main == null) main = System.Windows.Application.Current.Dispatcher;
            var all = AllStrategies();
            foreach (StrategyBase st in all)
            {
                string acct = "", state = "", inst = "", name = "";
                bool flat = true;
                try
                {
                    acct = st.Account != null ? st.Account.Name : "";
                    state = st.State.ToString();
                    name = st.Name;
                    inst = st.Instrument != null ? st.Instrument.FullName : "";
                    flat = st.Position == null || st.Position.Quantity == 0;
                }
                catch (Exception ex) { state = "unreadable: " + ex.GetType().Name; }
                // Only objects NinjaTrader has already stopped. "Configure" rows are saved templates or never-started strategies: never removed.
                bool stopped = state == "Finalized" || state == "Terminated";
                // A Configure object is also removable if it is a duplicate of a RUNNING strategy (same name and account) AND the
                // caller named it explicitly. Templates and never-started rows without a running twin are never removed.
                if (!stopped && state == "Configure" && !string.IsNullOrEmpty(onlyName) && name == onlyName)
                    stopped = staleNamed
                        || all.Any(o => !object.ReferenceEquals(o, st) && SafeName(o) == name && SafeAccount(o) == acct && SafeState(o) == "Realtime");
                bool eligible = account != null && acct == account.Name && stopped && flat
                    && (string.IsNullOrEmpty(onlyName) || name == onlyName);
                string outcome = "";
                if (eligible)
                {
                    if (!remove) { would++; outcome = "would_remove"; }
                    else
                    {
                        try
                        {
                            StrategyBase target = st;
                            string err = null;
                            var op = main.InvokeAsync(() =>
                            {
                                try { rm.Invoke(null, new object[] { target }); }
                                catch (Exception ex) { err = (ex.InnerException ?? ex).Message; }
                            }, System.Windows.Threading.DispatcherPriority.Send);
                            if (op.Wait(TimeSpan.FromSeconds(8)) != System.Windows.Threading.DispatcherOperationStatus.Completed)
                            { op.Abort(); errors++; outcome = "error: main window did not respond within 8 seconds"; }
                            else if (err != null) { errors++; outcome = "error: " + err; }
                            else { removed++; outcome = "removed"; }
                        }
                        catch (Exception ex) { errors++; outcome = "error: " + (ex.InnerException ?? ex).Message; }
                    }
                }
                rows.Add("{\"account\":" + Q(acct) + ",\"name\":" + Q(name) + ",\"instrument\":" + Q(inst)
                    + ",\"state\":" + Q(state) + ",\"flat\":" + (flat ? "true" : "false") + ",\"outcome\":" + Q(outcome) + "}");
            }
            return "{\"ok\":true,\"action\":\"strategy_purge\",\"dry_run\":" + (remove ? "false" : "true") + ",\"count\":" + rows.Count
                + ",\"would_remove\":" + would + ",\"removed\":" + removed + ",\"errors\":" + errors
                + ",\"rows\":[" + string.Join(",", rows.ToArray()) + "],\"timestamp\":" + Ts(DateTime.UtcNow) + "}";
        }

        // ---- Backtests through an open Strategy Analyzer window (EXPERIMENTAL) -----------------------------------
        // POST /backtest/run?class=OrbEvalMNQ&instrument=MNQ%2012-26&start=2020-01-02&end=2026-10-16&slippage=2&bar=1
        //   body: "Property=urlencoded value" per line (the spec's ninjascript.inputs). Returns {job:id}; poll GET /backtest/job?id=N
        //   (add &trades=1 for the trade list). One run at a time; needs the token when NT_OPERATOR_TOKEN is set.
        // It does not create a window (doing so crashed the Analyzer's Run command): it finds the user's OPEN Strategy Analyzer
        // through StrategyAnalyzerViewModel.Instances, switches the tab's strategy, sets inputs / instrument / dates / slippage and
        // calls the view model's own OnRun on that window's dispatcher, then reads SystemPerformance.AllTrades from the new result row.
        // Places no orders and touches no account. Use a specific contract (MNQ 12-26): NinjaTrader merges earlier contracts into it;
        // the continuous MNQ ##-## gave empty results. Every recompile of this file closes the Analyzer window: reopen it.
        // Pinned to NT 8.1.8.4 (TestedGuiVersion); relies on private members and may break on any NinjaTrader update.
        private class BtJob
        {
            public int Id; public string State = "queued", Step = "start", Error, Cls, Instrument;
            public string TradesJson = "[]", Info = ""; public int TradeCount; public double Net;
            public DateTime Started = DateTime.UtcNow, Ended;
        }
        private class BtProgress : NinjaTrader.Core.IProgress
        {
            public bool IsAborted { get { return false; } }
            public string Message { get; set; }
            public event EventHandler Aborted { add { } remove { } }
            public long Max, Steps;
            public void PerformStep() { Steps++; }
            public void SetUp(long maxSteps, bool isAbortable) { Max = maxSteps; }
            public void TearDown() { }
        }
        private static readonly Dictionary<int, BtJob> btJobs = new Dictionary<int, BtJob>();
        private static int btNext = 1;

        private string DoBacktestRun(HttpListenerContext ctx, out int code)
        {
            code = 200;
            string cls = ctx.Request.QueryString["class"], instrument = ctx.Request.QueryString["instrument"];
            string sStart = ctx.Request.QueryString["start"], sEnd = ctx.Request.QueryString["end"];
            if (string.IsNullOrEmpty(cls) || string.IsNullOrEmpty(instrument) || string.IsNullOrEmpty(sStart) || string.IsNullOrEmpty(sEnd))
            { code = 400; return Err("class, instrument, start, end are required"); }
            DateTime start, end;
            if (!DateTime.TryParse(sStart, CultureInfo.InvariantCulture, DateTimeStyles.None, out start) || !DateTime.TryParse(sEnd, CultureInfo.InvariantCulture, DateTimeStyles.None, out end))
            { code = 400; return Err("start/end must be yyyy-mm-dd"); }
            double slippage = 0; double.TryParse(ctx.Request.QueryString["slippage"] ?? "0", NumberStyles.Float, CultureInfo.InvariantCulture, out slippage);
            int bar = 1; int.TryParse(ctx.Request.QueryString["bar"] ?? "1", out bar);
            string commission = ctx.Request.QueryString["commission_template"] ?? "";
            var inputs = new Dictionary<string, string>();
            using (var rd = new StreamReader(ctx.Request.InputStream, Encoding.UTF8))
            {
                string line;
                while ((line = rd.ReadLine()) != null)
                {
                    int eq = line.IndexOf('='); if (eq <= 0) continue;
                    inputs[line.Substring(0, eq).Trim()] = Uri.UnescapeDataString(line.Substring(eq + 1).Trim());
                }
            }
            Type type = AppDomain.CurrentDomain.GetAssemblies().Select(x => { try { return x.GetType("NinjaTrader.NinjaScript.Strategies." + cls, false); } catch { return null; } }).FirstOrDefault(t => t != null);
            if (type == null) { code = 404; return Err("strategy class '" + cls + "' not found (compiled?)"); }
            BtJob job;
            lock (btJobs)
            {
                if (btJobs.Values.Any(j => j.State == "queued" || j.State == "running")) { code = 409; return Err("a backtest is already running"); }
                job = new BtJob { Id = btNext++, Cls = cls, Instrument = instrument };
                btJobs[job.Id] = job;
            }
            System.Windows.Application.Current.Dispatcher.BeginInvoke(new Action(() => RunBacktestJob(job, type, inputs, instrument, start, end, slippage, bar, commission)));
            return "{\"ok\":true,\"job\":" + job.Id + "}";
        }

        private static void RunBacktestJob(BtJob job, Type type, Dictionary<string, string> inputs, string instrument, DateTime start, DateTime end, double slippage, int bar, string commission)
        {
            try
            {
                job.State = "running";
                job.Step = "find_window";
                Assembly gui = AppDomain.CurrentDomain.GetAssemblies().First(x => x.GetName().Name == "NinjaTrader.Gui");
                if (gui.GetName().Version.ToString() != TestedGuiVersion) throw new Exception("NinjaTrader.Gui is " + gui.GetName().Version + "; only checked against " + TestedGuiVersion);
                Type vmT = gui.GetType("NinjaTrader.Gui.NinjaScript.StrategyAnalyzer.StrategyAnalyzerViewModel", true);
                PropertyInfo instP = vmT.GetProperty("Instances", BindingFlags.Static | BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
                object vm = null; System.Windows.Threading.Dispatcher od = null; int seen = 0;
                foreach (object cand in (System.Collections.IEnumerable)instP.GetValue(null, null))
                {
                    seen++;
                    var owner = vmT.GetProperty("Owner").GetValue(cand, null) as System.Windows.Threading.DispatcherObject;
                    if (owner == null) continue;
                    vm = cand; od = owner.Dispatcher;
                }
                job.Info = "analyzer instances=" + seen;
                if (vm == null) throw new Exception("no Strategy Analyzer window is open (instances: " + seen + "): open one (New > Strategy Analyzer) first");
                if (!od.CheckAccess()) { od.Invoke(new Action(() => RunOnOwner(job, type, inputs, instrument, start, end, slippage, bar, commission, vm, od))); return; }
                RunOnOwner(job, type, inputs, instrument, start, end, slippage, bar, commission, vm, od);
                return;
            }
            catch (Exception ex)
            {
                Exception e = ex.InnerException ?? ex;
                job.Error = "at step " + job.Step + ": " + e.GetType().Name + ": " + e.Message;
                job.State = "failed"; job.Ended = DateTime.UtcNow;
            }
        }

        private static void RunOnOwner(BtJob job, Type type, Dictionary<string, string> inputs, string instrument, DateTime start, DateTime end, double slippage, int bar, string commission, object vm, System.Windows.Threading.Dispatcher od)
        {
            try
            {
                object tab = vm.GetType().GetProperty("SelectedTab").GetValue(vm, null);
                if (tab == null) throw new Exception("the Analyzer window has no selected tab");
                object tp = tab.GetType().GetProperty("TabStrategyProperties").GetValue(tab, null);
                Type tpt = tp.GetType();
                job.Step = "select_strategy";
                PropertyInfo tmplP = tpt.GetProperty("StrategyTemplate");
                object tmpl = tmplP.GetValue(tp, null);
                job.Info = "template0=" + (tmpl == null ? "null" : tmpl.GetType().Name) + " strategyProp=" + tpt.GetProperty("Strategy").GetValue(tp, null);
                {
                    // always switch away and back so the Analyzer runs its own strategy-changed setup (a tab that already showed this
                    // strategy silently ignored Run)
                    if (tmpl != null && tmpl.GetType().Name == type.Name) tpt.GetProperty("Strategy").SetValue(tp, "buyandhold", null);
                    tpt.GetProperty("Strategy").SetValue(tp, type.Name, null);
                    tmpl = tmplP.GetValue(tp, null);
                    job.Info += " | after set: template=" + (tmpl == null ? "null" : tmpl.GetType().Name);
                }
                var st = tmpl as StrategyBase;
                if (st == null || st.GetType().Name != type.Name) throw new Exception("could not select " + type.Name + " in the Analyzer tab");
                type = st.GetType();
                job.Step = "set_inputs";
                foreach (var kv in inputs)
                {
                    PropertyInfo pi = type.GetProperty(kv.Key, BindingFlags.Public | BindingFlags.Instance);
                    if (pi == null || !pi.CanWrite) throw new Exception("unknown or read-only input '" + kv.Key + "' on " + type.Name);
                    pi.SetValue(st, ConvertTo(pi.PropertyType, kv.Value), null);
                }
                {
                    var bad = new List<string>();
                    foreach (var kv in inputs)
                    {
                        object cur = type.GetProperty(kv.Key).GetValue(st, null);
                        object want = ConvertTo(type.GetProperty(kv.Key).PropertyType, kv.Value);
                        if (!object.Equals(cur, want)) bad.Add(kv.Key + " wanted " + want + " got " + cur);
                    }
                    job.Info += " | inputs applied " + (inputs.Count - bad.Count) + "/" + inputs.Count + (bad.Count > 0 ? " MISMATCH: " + string.Join("; ", bad.ToArray()) : "");
                }
                job.Step = "set_settings";
                tpt.GetProperty("InstrumentOrInstrumentList").SetValue(tp, instrument, null);   // exactly what the first working run did
                st.From = start.Date; st.To = end.Date;
                st.Slippage = slippage;
                st.IncludeTradeHistoryInBacktest = true;
                if (!string.IsNullOrEmpty(commission)) { st.IncludeCommission = true; st.BacktestCommissionTemplate = commission; }
                job.Info += " | instr tab=" + tpt.GetProperty("InstrumentOrInstrumentList").GetValue(tp, null) + " tmpl=" + st.InstrumentOrInstrumentList + " from=" + st.From.ToString("yyyy-MM-dd") + " to=" + st.To.ToString("yyyy-MM-dd") + " slip=" + st.Slippage;
                BarsPeriod bp = (BarsPeriod)tpt.GetProperty("BarsPeriod").GetValue(tp, null);
                bp.BarsPeriodType = BarsPeriodType.Minute; bp.Value = bar;
                PropertyInfo resP = tab.GetType().GetProperty("Results");
                int before = ((System.Collections.IEnumerable)resP.GetValue(tab, null)).Cast<object>().Count();
                Func<object, string> rowId = r => { var g = r.GetType().GetProperty("Guid"); return g == null ? "" : Convert.ToString(g.GetValue(r, null)); };
                string lastBefore = before == 0 ? "" : rowId(((System.Collections.IEnumerable)resP.GetValue(tab, null)).Cast<object>().Last());
                job.Step = "on_run";
                vm.GetType().GetMethod("OnRun", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic).Invoke(vm, new object[] { null, null });
                job.Step = "waiting_for_result";
                ThreadPool.QueueUserWorkItem(_ =>
                {
                    try
                    {
                        DateTime t0 = DateTime.UtcNow; int after = before; bool finished = false; string note = ""; int quiet = 0; bool seenProgress = false;
                        while ((DateTime.UtcNow - t0).TotalMinutes < 40 && !finished)
                        {
                            Thread.Sleep(2000);
                            if (after <= before && (DateTime.UtcNow - t0).TotalSeconds > 90) break;
                            od.Invoke(new Action(() =>
                            {
                                after = ((System.Collections.IEnumerable)resP.GetValue(tab, null)).Cast<object>().Count();
                                object entry = after == 0 ? null : ((System.Collections.IEnumerable)resP.GetValue(tab, null)).Cast<object>().Last();
                                if (entry == null || (after <= before && rowId(entry) == lastBefore)) return;
                                after = Math.Max(after, before + 1);
                                Type et = entry.GetType();
                                TimeSpan dur = (TimeSpan)et.GetProperty("Duration").GetValue(entry, null);
                                var perf = et.GetProperty("Results").GetValue(entry, null) as NinjaTrader.Cbi.SystemPerformance;
                                int nTr = perf == null ? -1 : perf.AllTrades.Count;
                                note = "duration=" + dur + " trades=" + nTr;
                                bool progress = (bool)tab.GetType().GetProperty("IsProgressVisible").GetValue(tab, null);
                                note += " progress=" + progress;
                                if (progress) { seenProgress = true; quiet = 0; return; }
                                if (!seenProgress && (DateTime.UtcNow - t0).TotalSeconds < 30) return;
                                if (++quiet < 3) return;   // not running for 3 polls in a row
                                finished = true;
                                var sb = new StringBuilder("[");
                                int n = 0; double net = 0;
                                foreach (NinjaTrader.Cbi.Trade t in perf.AllTrades)
                                {
                                    if (n > 0) sb.Append(",");
                                    sb.Append("{\"entry_time\":" + Q(t.Entry.Time.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture))
                                        + ",\"exit_time\":" + Q(t.Exit.Time.ToString("yyyy-MM-dd HH:mm:ss", CultureInfo.InvariantCulture))
                                        + ",\"side\":" + Q(t.Entry.MarketPosition.ToString()) + ",\"qty\":" + t.Quantity
                                        + ",\"entry_price\":" + t.Entry.Price.ToString("R", CultureInfo.InvariantCulture)
                                        + ",\"exit_price\":" + t.Exit.Price.ToString("R", CultureInfo.InvariantCulture)
                                        + ",\"entry_name\":" + Q(t.Entry.Name ?? "") + ",\"exit_name\":" + Q(t.Exit.Name ?? "")
                                        + ",\"profit\":" + t.ProfitCurrency.ToString("R", CultureInfo.InvariantCulture)
                                        + ",\"commission\":" + t.Commission.ToString("R", CultureInfo.InvariantCulture) + "}");
                                    net += t.ProfitCurrency; n++;
                                }
                                sb.Append("]");
                                job.TradesJson = sb.ToString(); job.TradeCount = n; job.Net = net;
                            }));
                        }
                        job.Info += " | results " + before + "->" + after + " " + note;
                        job.State = finished ? "done" : "failed"; job.Step = "done"; job.Ended = DateTime.UtcNow;
                        if (!finished) job.Error = after <= before ? "Run did nothing: no result row appeared within 90 s" : "run did not finish in 40 minutes (" + note + ")";
                    }
                    catch (Exception ex) { job.Error = "poll: " + ex.Message; job.State = "failed"; job.Ended = DateTime.UtcNow; }
                });
            }
            catch (Exception ex)
            {
                Exception e = ex.InnerException ?? ex;
                string where = e.StackTrace == null ? "" : e.StackTrace.Split((char)10)[0].Trim();
                job.Error = "at step " + job.Step + ": " + e.GetType().Name + ": " + e.Message + " | " + where;
                job.State = "failed"; job.Ended = DateTime.UtcNow;
            }
        }

        private static string BuildBacktestJob(int id, bool withTrades)
        {
            BtJob j;
            lock (btJobs) { if (!btJobs.TryGetValue(id, out j)) return Err("no such job"); }
            string s = "{\"ok\":true,\"job\":" + j.Id + ",\"state\":" + Q(j.State) + ",\"step\":" + Q(j.Step) + ",\"class\":" + Q(j.Cls)
                + ",\"error\":" + (j.Error == null ? "null" : Q(j.Error)) + ",\"info\":" + Q(j.Info) + ",\"trade_count\":" + j.TradeCount
                + ",\"net\":" + j.Net.ToString("R", CultureInfo.InvariantCulture)
                + ",\"seconds\":" + (((j.Ended == default(DateTime) ? DateTime.UtcNow : j.Ended) - j.Started).TotalSeconds).ToString("F0", CultureInfo.InvariantCulture);
            if (withTrades) s += ",\"trades\":" + j.TradesJson;
            return s + "}";
        }

        private static bool Matches(Instrument i, string filter)
        {
            return string.IsNullOrEmpty(filter) || (i != null && i.FullName == filter);
        }

        private static List<Order> CancellableOrders(Account a, string instrument)
        {
            var list = new List<Order>();
            lock (a.Orders)
            {
                foreach (Order o in a.Orders)
                {
                    if (o.OrderState == OrderState.Filled || o.OrderState == OrderState.Cancelled
                        || o.OrderState == OrderState.Rejected || o.OrderState == OrderState.CancelPending
                        || o.OrderState == OrderState.CancelSubmitted) continue;
                    if (Matches(o.Instrument, instrument)) list.Add(o);
                }
            }
            return list;
        }

        // Flatten positions (NT also cancels the working orders on those instruments).
        private string DoFlatten(Account a, string instrument)
        {
            var instruments = new List<Instrument>();
            lock (a.Positions)
            {
                foreach (Position p in a.Positions)
                    if (p.MarketPosition != MarketPosition.Flat && Matches(p.Instrument, instrument)) instruments.Add(p.Instrument);
            }
            foreach (Order o in CancellableOrders(a, instrument))
                if (!instruments.Contains(o.Instrument)) instruments.Add(o.Instrument);
            if (instruments.Count > 0) a.Flatten(instruments);
            var names = new List<string>();
            foreach (Instrument i in instruments) names.Add(Q(i.FullName));
            return "{\"ok\":true,\"action\":\"flatten\",\"account\":" + Q(a.Name) + ",\"instruments\":[" + string.Join(",", names.ToArray())
                + "],\"timestamp\":" + Ts(DateTime.UtcNow) + "}";
        }

        private string DoCancelAll(Account a, string instrument)
        {
            List<Order> orders = CancellableOrders(a, instrument);
            if (orders.Count > 0) a.Cancel(orders);
            return "{\"ok\":true,\"action\":\"cancel_all\",\"account\":" + Q(a.Name) + ",\"cancel_requested\":"
                + orders.Count.ToString(CultureInfo.InvariantCulture) + ",\"timestamp\":" + Ts(DateTime.UtcNow) + "}";
        }

        private static double PnlNow(Account a)
        {
            double r = 0, u = 0;
            try { r = a.Get(AccountItem.RealizedProfitLoss, Currency.UsDollar); } catch { }
            try { u = a.Get(AccountItem.UnrealizedProfitLoss, Currency.UsDollar); } catch { }
            return r + u;
        }

        // Latched: once P&L since arming reaches -max_loss, keep flattening and cancelling every 2s
        // (strategies may try to re-enter) until disarmed. This does NOT disable the strategies themselves.
        private void ArmKill(Account a, double maxLoss)
        {
            lock (killLock)
            {
                if (killTimer != null) killTimer.Dispose();
                killAccount = a.Name;
                killMaxLoss = maxLoss;
                killBaseline = PnlNow(a);
                killLastPnl = killBaseline;
                killArmed = true;
                killTripped = false;
                killActions = 0;
                killArmedUtc = DateTime.UtcNow;
                killTimer = new System.Threading.Timer(KillTick, null, 2000, 2000);
            }
        }

        private void DisarmKill()
        {
            lock (killLock)
            {
                if (killTimer != null) { killTimer.Dispose(); killTimer = null; }
                killArmed = false;
                killTripped = false;
            }
        }

        private void KillTick(object state)
        {
            try
            {
                lock (killLock)
                {
                    if (!killArmed) return;
                    Account a = FindAccount(killAccount);
                    if (a == null) return;
                    killLastPnl = PnlNow(a);
                    if (!killTripped && killLastPnl - killBaseline <= -killMaxLoss)
                    {
                        killTripped = true;
                        killTrippedUtc = DateTime.UtcNow;
                    }
                    if (killTripped)
                    {
                        DoFlatten(a, null);
                        DoCancelAll(a, null);
                        killActions++;
                    }
                }
            }
            catch (Exception ex) { Print("NtStatusAddOn kill switch error: " + ex.Message); }
        }

        private string BuildKill()
        {
            lock (killLock)
            {
                var sb = new StringBuilder();
                sb.Append("{\"ok\":true,\"timestamp\":").Append(Ts(DateTime.UtcNow));
                sb.Append(",\"armed\":").Append(killArmed ? "true" : "false");
                sb.Append(",\"tripped\":").Append(killTripped ? "true" : "false");
                sb.Append(",\"account\":").Append(Q(killAccount));
                sb.Append(",\"max_loss\":").Append(Num(killMaxLoss));
                sb.Append(",\"baseline_pnl\":").Append(Num(killBaseline));
                sb.Append(",\"pnl_since_arm\":").Append(Num(killLastPnl - killBaseline));
                sb.Append(",\"armed_at\":").Append(killArmed ? Ts(killArmedUtc) : "null");
                sb.Append(",\"tripped_at\":").Append(killTripped ? Ts(killTrippedUtc) : "null");
                sb.Append(",\"flatten_cycles\":").Append(killActions.ToString(CultureInfo.InvariantCulture));
                sb.Append("}");
                return sb.ToString();
            }
        }

        private static int QueryInt(HttpListenerContext ctx, string key, int def)
        {
            string v = ctx.Request.QueryString[key];
            int n;
            return int.TryParse(v, NumberStyles.Integer, CultureInfo.InvariantCulture, out n) && n > 0 ? Math.Min(n, 5000) : def;
        }

        private static int QueryRange(HttpListenerContext ctx, string key, int def, int min, int max)
        {
            int n;
            if (!int.TryParse(ctx.Request.QueryString[key], NumberStyles.Integer, CultureInfo.InvariantCulture, out n)) return def;
            return Math.Max(min, Math.Min(n, max));
        }

        // Read-only historical bars through NinjaTrader's own BarsRequest (the data the Strategy Analyzer uses).
        // GET /history?instrument=MNQ%2012-26&type=Minute&value=1&from=2026-01-01&to=2026-01-31
        // Bar times are NinjaTrader local time (bar close), "yyyy-MM-ddTHH:mm:ss". Capped at 250000 bars per call (`limit`, up to 500000).
        // Optional, all read-only query options (a client that sends none gets the old reply plus the new fields):
        //   price=Last|Bid|Ask   which market data the bars are built from (BarsPeriod.MarketDataType); default Last. Echoed in the reply.
        //   merge=backadjusted|nonbackadjusted|none   contract merge (default backadjusted, as before). Echoed in the reply.
        //   offset=N&limit=M     page through a big reply: bars [offset, offset+M) of `total`; `truncated` means more pages remain.
        //   fmt=tick             Tick bars of value 1 only: rows [time, price, volume] with time "yyyy-MM-ddTHH:mm:ss.fffffff".
        //   quotes=1             with fmt=tick: rows [time, price, volume, bid, ask], the bid / ask NinjaTrader stored with that tick
        //                        (Bars.GetBid / GetAsk). Echoed as "quotes":true. UNVERIFIED: not compiled or checked against Bid / Ask series.
        // For Tick bars NinjaTrader returns the whole trading session that contains `from` (it starts 17:00 the evening before), so a day
        // can hold millions of ticks: page with offset / limit. The loaded session is kept between the pages of one request (same
        // instrument / type / value / from / to / price / merge) and released after the last page or HistKeepSeconds without a page, so
        // a 16-million-tick Bid day is loaded once instead of once per page. The reply says "cached":true when a page reused it.
        // The Nov 12 version (price / merge / paging / fmt=tick) runs in NT8 and feeds the nightly tick job. The session cache and
        // quotes=1 are NOT yet compiled in NT8.
        private const int HistKeepSeconds = 120;
        private readonly object histLock = new object();
        private BarsRequest histReq;
        private string histKey;
        private DateTime histUsedUtc;
        private System.Threading.Timer histTimer;

        // Call with histLock held.
        private void DropHistory()
        {
            if (histReq != null) { try { histReq.Dispose(); } catch { } }
            histReq = null;
            histKey = null;
        }

        private void HistoryIdle(object state)
        {
            lock (histLock)
            {
                if (histReq != null && (DateTime.UtcNow - histUsedUtc).TotalSeconds >= HistKeepSeconds) DropHistory();
            }
        }

        private string BuildHistory(HttpListenerContext ctx, out int code)
        {
            code = 200;
            string inst = ctx.Request.QueryString["instrument"];
            string type = ctx.Request.QueryString["type"] ?? "Minute";
            DateTime from, to;
            int value = QueryInt(ctx, "value", 1);
            if (string.IsNullOrEmpty(inst)) { code = 400; return Err("instrument is required"); }
            if (!DateTime.TryParse(ctx.Request.QueryString["from"], CultureInfo.InvariantCulture, DateTimeStyles.None, out from)
                || !DateTime.TryParse(ctx.Request.QueryString["to"], CultureInfo.InvariantCulture, DateTimeStyles.None, out to)
                || to < from) { code = 400; return Err("from and to (yyyy-MM-dd) are required, to >= from"); }
            to = to.Date.AddDays(1).AddSeconds(-1);
            BarsPeriodType bpt;
            if (!Enum.TryParse(type, true, out bpt) || (bpt != BarsPeriodType.Minute && bpt != BarsPeriodType.Second
                && bpt != BarsPeriodType.Tick && bpt != BarsPeriodType.Day))
            { code = 400; return Err("type must be Tick, Second, Minute or Day"); }
            Instrument instrument = Instrument.GetInstrument(inst);
            if (instrument == null) { code = 404; return Err("unknown instrument '" + inst + "'"); }

            string priceName = ctx.Request.QueryString["price"] ?? "Last";
            MarketDataType mdt;
            if (!Enum.TryParse(priceName, true, out mdt) || (mdt != MarketDataType.Last && mdt != MarketDataType.Bid && mdt != MarketDataType.Ask))
            { code = 400; return Err("price must be Last, Bid or Ask"); }
            string mergeName = (ctx.Request.QueryString["merge"] ?? "backadjusted").ToLowerInvariant();
            MergePolicy mergePolicy;
            if (mergeName == "backadjusted") mergePolicy = MergePolicy.MergeBackAdjusted;
            else if (mergeName == "nonbackadjusted") mergePolicy = MergePolicy.MergeNonBackAdjusted;
            else if (mergeName == "none") mergePolicy = MergePolicy.DoNotMerge;
            else { code = 400; return Err("merge must be backadjusted, nonbackadjusted or none"); }
            bool tickFmt = ctx.Request.QueryString["fmt"] == "tick";
            if (tickFmt && (bpt != BarsPeriodType.Tick || value != 1)) { code = 400; return Err("fmt=tick needs type=Tick and value=1"); }
            int offset = QueryRange(ctx, "offset", 0, 0, int.MaxValue);
            int limit = QueryRange(ctx, "limit", 250000, 1, 500000);
            bool quotes = ctx.Request.QueryString["quotes"] == "1";
            if (quotes && !tickFmt) { code = 400; return Err("quotes=1 needs fmt=tick"); }

            string key = instrument.FullName + "|" + bpt + "|" + value.ToString(CultureInfo.InvariantCulture) + "|" + from.ToString("s", CultureInfo.InvariantCulture)
                + "|" + to.ToString("s", CultureInfo.InvariantCulture) + "|" + mdt + "|" + mergeName;
            lock (histLock)
            {
                bool cached = histReq != null && histKey == key;
                if (!cached)
                {
                    DropHistory();
                    string error = null;
                    var done = new ManualResetEvent(false);
                    BarsRequest fresh = new BarsRequest(instrument, from, to);
                    fresh.BarsPeriod = new BarsPeriod { BarsPeriodType = bpt, Value = value, MarketDataType = mdt };
                    fresh.MergePolicy = mergePolicy;
                    fresh.TradingHours = instrument.MasterInstrument.TradingHours;
                    bool loaded = false;
                    try
                    {
                        fresh.Request((r, ec, msg) =>
                        {
                            if (ec != ErrorCode.NoError) error = ec + ": " + msg;
                            done.Set();
                        });
                        if (!done.WaitOne(TimeSpan.FromSeconds(90))) { code = 504; return Err("BarsRequest timed out after 90s"); }
                        if (error != null) { code = 502; return Err(error); }
                        loaded = true;
                    }
                    finally { if (!loaded) fresh.Dispose(); }
                    histReq = fresh;
                    histKey = key;
                }
                BarsRequest req = histReq;
                bool keep = false;
                try
                {
                    var sb = new StringBuilder();
                    int total = req.Bars == null ? 0 : req.Bars.Count;
                    int first = Math.Min(offset, total);
                    int n = Math.Min(limit, total - first);
                    bool truncated = first + n < total;
                    sb.Append("{\"ok\":true,\"instrument\":").Append(Q(instrument.FullName));
                    sb.Append(",\"type\":").Append(Q(bpt.ToString())).Append(",\"value\":").Append(value.ToString(CultureInfo.InvariantCulture));
                    sb.Append(",\"price\":").Append(Q(mdt.ToString())).Append(",\"merge\":").Append(Q(mergeName));
                    sb.Append(",\"fmt\":").Append(Q(tickFmt ? "tick" : "bar"));
                    if (quotes) sb.Append(",\"quotes\":true");
                    sb.Append(",\"cached\":").Append(cached ? "true" : "false");
                    sb.Append(",\"total\":").Append(total.ToString(CultureInfo.InvariantCulture));
                    sb.Append(",\"offset\":").Append(first.ToString(CultureInfo.InvariantCulture));
                    sb.Append(",\"count\":").Append(n.ToString(CultureInfo.InvariantCulture));
                    sb.Append(",\"truncated\":").Append(truncated ? "true" : "false").Append(",\"bars\":[");
                    for (int k = 0; k < n; k++)
                    {
                        int i = first + k;
                        if (k > 0) sb.Append(",");
                        if (tickFmt)
                        {
                            sb.Append("[").Append(Q(req.Bars.GetTime(i).ToString("yyyy-MM-ddTHH:mm:ss.fffffff", CultureInfo.InvariantCulture)));
                            sb.Append(",").Append(NumHi(req.Bars.GetClose(i)));
                            sb.Append(",").Append(req.Bars.GetVolume(i).ToString(CultureInfo.InvariantCulture));
                            if (quotes) sb.Append(",").Append(NumHi(req.Bars.GetBid(i))).Append(",").Append(NumHi(req.Bars.GetAsk(i)));
                            sb.Append("]");
                            continue;
                        }
                        sb.Append("[").Append(Q(req.Bars.GetTime(i).ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture)));
                        sb.Append(",").Append(NumHi(req.Bars.GetOpen(i))).Append(",").Append(NumHi(req.Bars.GetHigh(i)));
                        sb.Append(",").Append(NumHi(req.Bars.GetLow(i))).Append(",").Append(NumHi(req.Bars.GetClose(i)));
                        sb.Append(",").Append(req.Bars.GetVolume(i).ToString(CultureInfo.InvariantCulture)).Append("]");
                    }
                    keep = truncated;                              // more pages to come: keep the loaded session for them
                    return sb.Append("]}").ToString();
                }
                finally
                {
                    if (keep)
                    {
                        histUsedUtc = DateTime.UtcNow;
                        if (histTimer == null) histTimer = new System.Threading.Timer(HistoryIdle, null, System.Threading.Timeout.Infinite, System.Threading.Timeout.Infinite);
                        histTimer.Change((HistKeepSeconds + 1) * 1000, System.Threading.Timeout.Infinite);
                    }
                    else DropHistory();
                }
            }
        }

        private static string Ts(DateTime t)
        {
            return Q(t.ToUniversalTime().ToString("o"));
        }

        // Working orders (not Filled / Cancelled / Rejected) on every account.
        private string BuildOrders()
        {
            var sb = new StringBuilder();
            sb.Append("{\"timestamp\":").Append(Ts(DateTime.UtcNow)).Append(",\"orders\":[");
            bool first = true;
            lock (Account.All)
            {
                foreach (Account a in Account.All)
                {
                    lock (a.Orders)
                    {
                        foreach (Order o in a.Orders)
                        {
                            if (o.OrderState == OrderState.Filled || o.OrderState == OrderState.Cancelled
                                || o.OrderState == OrderState.Rejected) continue;
                            if (!first) sb.Append(",");
                            first = false;
                            sb.Append("{\"account\":").Append(Q(a.Name));
                            sb.Append(",\"name\":").Append(Q(o.Name));
                            sb.Append(",\"instrument\":").Append(Q(o.Instrument.FullName));
                            sb.Append(",\"action\":").Append(Q(o.OrderAction.ToString()));
                            sb.Append(",\"type\":").Append(Q(o.OrderType.ToString()));
                            sb.Append(",\"state\":").Append(Q(o.OrderState.ToString()));
                            sb.Append(",\"quantity\":").Append(o.Quantity.ToString(CultureInfo.InvariantCulture));
                            sb.Append(",\"limit_price\":").Append(Num(o.LimitPrice));
                            sb.Append(",\"stop_price\":").Append(Num(o.StopPrice));
                            sb.Append(",\"oco\":").Append(Q(o.Oco));
                            sb.Append(",\"time\":").Append(Ts(o.Time));
                            sb.Append("}");
                        }
                    }
                }
            }
            sb.Append("]}");
            return sb.ToString();
        }

        // Most recent fills (newest last), capped at `limit` per account.
        private string BuildExecutions(int limit)
        {
            var sb = new StringBuilder();
            sb.Append("{\"timestamp\":").Append(Ts(DateTime.UtcNow)).Append(",\"executions\":[");
            bool first = true;
            lock (Account.All)
            {
                foreach (Account a in Account.All)
                {
                    var rows = new List<Execution>();
                    lock (a.Executions) { foreach (Execution e in a.Executions) rows.Add(e); }
                    rows.Sort((x, y) => x.Time.CompareTo(y.Time));
                    int start = Math.Max(0, rows.Count - limit);
                    for (int i = start; i < rows.Count; i++)
                    {
                        Execution e = rows[i];
                        if (!first) sb.Append(",");
                        first = false;
                        sb.Append("{\"account\":").Append(Q(a.Name));
                        sb.Append(",\"execution_id\":").Append(Q(e.ExecutionId));
                        sb.Append(",\"instrument\":").Append(Q(e.Instrument.FullName));
                        sb.Append(",\"time\":").Append(Ts(e.Time));
                        // Prefer the order's own action. If the Order object is gone (fills from before an NT restart),
                        // derive it from the execution's market position plus entry/exit. UNVERIFIED in NT: this assumes
                        // Execution.MarketPosition is the side of the position the fill belongs to (long entry = Buy,
                        // long exit = Sell, short entry = SellShort, short exit = BuyToCover). "action_source" says
                        // which path was used so the Python side and a human can tell.
                        string act = "", actSrc = "order";
                        if (e.Order != null) act = e.Order.OrderAction.ToString();
                        else
                        {
                            actSrc = "derived";
                            bool isLong = e.MarketPosition == MarketPosition.Long;
                            bool isShort = e.MarketPosition == MarketPosition.Short;
                            if (e.IsEntry && isLong) act = "Buy";
                            else if (e.IsEntry && isShort) act = "SellShort";
                            else if (e.IsExit && isLong) act = "Sell";
                            else if (e.IsExit && isShort) act = "BuyToCover";
                            else actSrc = "unknown";
                        }
                        sb.Append(",\"action\":").Append(Q(act));
                        sb.Append(",\"action_source\":").Append(Q(actSrc));
                        sb.Append(",\"order_name\":").Append(Q(e.Order != null ? e.Order.Name : ""));
                        sb.Append(",\"quantity\":").Append(e.Quantity.ToString(CultureInfo.InvariantCulture));
                        sb.Append(",\"price\":").Append(Num(e.Price));
                        sb.Append(",\"commission\":").Append(Num(e.Commission));
                        sb.Append(",\"is_entry\":").Append(e.IsEntry ? "true" : "false");
                        sb.Append(",\"is_exit\":").Append(e.IsExit ? "true" : "false");
                        sb.Append("}");
                    }
                }
            }
            sb.Append("]}");
            return sb.ToString();
        }

        // Strategies running on each account (Account.Strategies). Not compiled in this repo's CI.
        private string BuildStrategies()
        {
            var sb = new StringBuilder();
            sb.Append("{\"timestamp\":").Append(Ts(DateTime.UtcNow)).Append(",\"strategies\":[");
            bool first = true;
            lock (Account.All)
            {
                foreach (Account a in Account.All)
                {
                    foreach (StrategyBase st in a.Strategies)
                    {
                        if (!first) sb.Append(",");
                        first = false;
                        sb.Append("{\"account\":").Append(Q(a.Name));
                        sb.Append(",\"name\":").Append(Q(st.Name));
                        sb.Append(",\"instrument\":").Append(Q(st.Instrument != null ? st.Instrument.FullName : ""));
                        sb.Append(",\"state\":").Append(Q(st.State.ToString()));
                        sb.Append(",\"position\":").Append(Q(st.Position != null ? st.Position.MarketPosition.ToString() : ""));
                        sb.Append(",\"quantity\":").Append((st.Position != null ? st.Position.Quantity : 0).ToString(CultureInfo.InvariantCulture));
                        sb.Append("}");
                    }
                }
            }
            sb.Append("]}");
            return sb.ToString();
        }

        // Add-on heartbeat plus connection / data-feed status per account.
        private string BuildHealth()
        {
            var sb = new StringBuilder();
            sb.Append("{\"ok\":true,\"timestamp\":").Append(Ts(DateTime.UtcNow));
            sb.Append(",\"uptime_seconds\":").Append(((long)(DateTime.UtcNow - startedUtc).TotalSeconds).ToString(CultureInfo.InvariantCulture));
            sb.Append(",\"connections\":[");
            bool first = true;
            lock (Account.All)
            {
                foreach (Account a in Account.All)
                {
                    if (a.Connection == null) continue;
                    if (!first) sb.Append(",");
                    first = false;
                    sb.Append("{\"account\":").Append(Q(a.Name));
                    sb.Append(",\"connection\":").Append(Q(a.Connection.Options.Name));
                    sb.Append(",\"status\":").Append(Q(a.Connection.Status.ToString()));
                    sb.Append(",\"price_status\":").Append(Q(a.Connection.PriceStatus.ToString()));
                    sb.Append("}");
                }
            }
            sb.Append("]}");
            return sb.ToString();
        }

        private string BuildStatus()
        {
            var sb = new StringBuilder();
            sb.Append("{\"timestamp\":").Append(Q(DateTime.UtcNow.ToString("o"))).Append(",\"accounts\":[");
            bool first = true;
            lock (Account.All)
            {
                foreach (Account a in Account.All)
                {
                    if (!first) sb.Append(",");
                    first = false;
                    sb.Append("{");
                    sb.Append("\"name\":").Append(Q(a.Name));
                    sb.Append(",\"connection\":").Append(Q(a.Connection != null ? a.Connection.Options.Name : ""));
                    sb.Append(",\"cash_value\":").Append(Item(a, AccountItem.CashValue));
                    sb.Append(",\"realized_pnl\":").Append(Item(a, AccountItem.RealizedProfitLoss));
                    sb.Append(",\"unrealized_pnl\":").Append(Item(a, AccountItem.UnrealizedProfitLoss));
                    sb.Append(",\"positions\":[");
                    bool firstPos = true;
                    lock (a.Positions)
                    {
                        foreach (Position p in a.Positions)
                        {
                            if (p.MarketPosition == MarketPosition.Flat) continue;
                            if (!firstPos) sb.Append(",");
                            firstPos = false;
                            sb.Append("{\"instrument\":").Append(Q(p.Instrument.FullName));
                            sb.Append(",\"direction\":").Append(Q(p.MarketPosition.ToString()));
                            sb.Append(",\"quantity\":").Append(p.Quantity.ToString(CultureInfo.InvariantCulture));
                            sb.Append(",\"avg_price\":").Append(Num(p.AveragePrice));
                            sb.Append("}");
                        }
                    }
                    sb.Append("]}");
                }
            }
            sb.Append("]}");
            return sb.ToString();
        }

        private static string Item(Account a, AccountItem item)
        {
            try { return Num(a.Get(item, Currency.UsDollar)); }
            catch { return "null"; }
        }

        // Historical bars keep up to 6 decimals (Num truncates to 4, which loses the 6J / FX tick size).
        private static string NumHi(double v)
        {
            if (double.IsNaN(v) || double.IsInfinity(v)) return "null";
            return v.ToString("0.######", CultureInfo.InvariantCulture);
        }

        private static string Num(double v)
        {
            if (double.IsNaN(v) || double.IsInfinity(v)) return "null";
            return v.ToString("0.####", CultureInfo.InvariantCulture);
        }

        private static string Q(string s)
        {
            if (s == null) return "\"\"";
            var sb = new StringBuilder("\"");
            foreach (char c in s)
            {
                if (c == '"' || c == '\\') sb.Append('\\').Append(c);
                else if (c < 0x20) sb.Append(' ');
                else sb.Append(c);
            }
            return sb.Append("\"").ToString();
        }
    }
}
