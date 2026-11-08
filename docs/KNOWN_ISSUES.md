# Known issues

## BUG-001: Launcher cannot recover NinjaTrader unattended (login not remembered)

Status: open. Found 2026-11-08 on NinjaTrader 8.1.8.4. Severity: high for the "runs while I'm away" goal.

**Symptom.** After NinjaTrader is killed or crashes, `launcher.py` correctly restarts it, but NinjaTrader comes back at its
own login window (title "Welcome", username prefilled, password blank). The add-on never starts, `/health` does not
answer, and no strategy is enabled until someone logs in on the PC.

**Reproduction (2026-11-08).**
1. Log in to NinjaTrader; add-on up, Sim101 and the Lucid account Connected, no strategies running.
2. `Stop-Process -Name NinjaTrader -Force` (simulated crash).
3. `python launcher.py --apply` (what the 30-minute task does). It starts NinjaTrader.
4. After more than 3 minutes: login window, no add-on. Same result for a relaunch after a normal close, and after a
   close that went through an exit confirmation dialog.

**What is known.**
- The login has never been remembered between launches. The earlier launch that looked like it connected on its own
  (06:12) was a manual login by the user. Whether NinjaTrader can keep the session at all is unknown.
- The login window has no "remember me" option.
- This is the NinjaTrader account login, not a broker connection. `/health` lists only the `Simulation` connection.
- NinjaTrader staff have said on their forum that 8.1 cannot skip the password at launch and has no command-line
  login. There is no setting in `Config.xml` for it either. Option 1 below is therefore effectively closed.

**What the launcher does about it today.** Nothing can log in for it, by design (it never handles credentials). If
the add-on is not answering and a NinjaTrader window titled "Welcome" is visible, it alerts on that tick, whoever
started NinjaTrader (launcher, reboot, by hand). Failing that, it alerts 10 minutes after it started NinjaTrader
itself. One toast (and a push if `NT_WATCH_PUSH_URL` is set) per stuck episode; it re-arms once the add-on answers,
and the launch record survives midnight. If the supervisor daemon is running, the launcher stays quiet and the supervisor's `addon_unreachable` incident (cause `login_window`) is the alert instead, pushed by the watcher. Strategies are not enabled until the add-on answers, so nothing runs.
Before 2026-11-08 the alert was lost if NinjaTrader was relaunched before midnight CT or was not started by the
launcher.

**Impact.** A crash, Windows update reboot, or manual close while away stops trading until a person logs in.

**Options (none implemented).**
1. Find out whether NinjaTrader can keep the login across launches, and how (support question).
2. Remote access to the PC so the login can be done from a phone after an alert.
3. Password-manager autofill on the login window (untested).
4. User-written credential automation. Not provided by this project.

**Done when.** After a forced kill, `launcher.py --apply` brings NinjaTrader to a connected add-on with no human input.
