// NtStatusAddOn.cs - READ-ONLY status server for NinjaTrader 8.
// Serves JSON on http://127.0.0.1:8765: /status (accounts, P&L, positions), /orders (working),
// /executions?limit=N (fills), /strategies, /health (heartbeat + connection status).
// Contains NO order, cancel, or modify calls. GET requests only.
#region Using declarations
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Net;
using System.Text;
using System.Threading;
using NinjaTrader.Cbi;
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
                if (ctx.Request.HttpMethod != "GET") { code = 405; body = "{\"error\":\"read-only\"}"; }
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
