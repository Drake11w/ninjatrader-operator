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
                else if (ctx.Request.Url.AbsolutePath == "/health") body = BuildHealth();
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

        private static Account FindAccount(string name)
        {
            lock (Account.All)
            {
                foreach (Account a in Account.All) if (a.Name == name) return a;
            }
            return null;
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

            string name = ctx.Request.QueryString["account"];
            string instrument = ctx.Request.QueryString["instrument"];
            if (string.IsNullOrEmpty(name)) { code = 400; return Err("account is required"); }
            if (!AccountAllowed(name)) { code = 403; return Err("account '" + name + "' is not a Sim* account or in NT_OPERATOR_ALLOWED_ACCOUNTS; live control is locked"); }
            Account a = FindAccount(name);
            if (a == null) { code = 404; return Err("unknown account '" + name + "'"); }

            if (path == "/strategy/enable") return DoStrategyEnable(ctx, a, out code);
            if (path == "/strategy/disable") return DoStrategyDisable(ctx, a, out code);
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
                        sb.Append(",\"action\":").Append(Q(e.Order != null ? e.Order.OrderAction.ToString() : ""));
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
