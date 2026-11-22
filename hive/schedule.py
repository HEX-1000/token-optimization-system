"""Trial days, the random schedule and the scheduled task (plan section 5).

A trial day runs from 04:00 to 04:00 local time. The schedule is drawn from os.urandom in pairs of
calendar days: in each pair one day is on and one off, in random order.

The scheduled task runs `python -m hive tick` at 04:00 and at logon. On Windows it is a Task Scheduler
task with "run as soon as possible after a missed start" (StartWhenAvailable), defined by an XML file and
registered with schtasks for the user who starts the trial (their logon only, no elevation); on Linux and
macOS the program prints cron lines for the user to add. Only a live trial creates the task (hive.trial);
any other trial is a dry run that writes the XML and returns the command it would run.
"""
import getpass
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

DAY_START_HOUR = 4
TASK_NAME = "TokenOptimizationSystemHiveTick"


def local(ts, tz=None):
    """An ISO timestamp (UTC 'Z' or with offset) or a datetime as an aware local datetime."""
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(tz) if tz is not None else ts.astimezone()


def trial_date(ts, tz=None):
    """Calendar date of the trial day that holds ts (the day starts at 04:00 local time)."""
    return (local(ts, tz) - timedelta(hours=DAY_START_HOUR)).date()


def day_start(date, tz=None):
    """Aware datetime of 04:00 local time on `date`."""
    naive = datetime(date.year, date.month, date.day, DAY_START_HOUR)
    if tz is not None:
        return naive.replace(tzinfo=tz)
    return naive.astimezone()


def draw(n_days, rand=os.urandom):
    """n_days states, 'on' or 'off', in pairs with one of each in random order."""
    out = []
    while len(out) < n_days:
        first_on = rand(1)[0] & 1
        out += ["on", "off"] if first_on else ["off", "on"]
    return out[:n_days]


def package_parent():
    """The folder that holds the hive package: the task's working directory, so that `-m hive` finds it."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def task_python(python=None):
    """The interpreter the task runs: pythonw.exe next to python.exe when it exists, so that no console
    window opens at logon. Output is not needed; errors go to tick_errors.jsonl (hive tick)."""
    python = python or sys.executable
    head, name = os.path.split(python)
    if name.lower() == "python.exe" and os.path.isfile(os.path.join(head, "pythonw.exe")):
        return os.path.join(head, "pythonw.exe")
    return python


def task_user():
    """DOMAIN\\user of the user who runs this process; the task runs for this user only."""
    user = getpass.getuser()
    domain = os.environ.get("USERDOMAIN")
    return "{}\\{}".format(domain, user) if domain else user


def task_xml(python, workdir, user, enabled=True):
    """Task Scheduler XML: daily at 04:00 and at this user's logon, start when available, run only while the
    user is logged on (InteractiveToken) with the user's normal rights, so that no password and no elevation
    is needed. `enabled` False registers a task that never runs (the registration check)."""
    start = datetime.now().strftime("%Y-%m-%dT") + "%02d:00:00" % DAY_START_HOUR
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Token Optimization System hive: apply the trial day's setting and run the sensor.</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger><StartBoundary>{start}</StartBoundary><ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger>
    <LogonTrigger><UserId>{user}</UserId><Delay>PT2M</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>{user}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <Enabled>{enabled}</Enabled>
    <StartWhenAvailable>true</StartWhenAvailable>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{python}</Command><Arguments>-B -m hive tick</Arguments><WorkingDirectory>{workdir}</WorkingDirectory></Exec>
  </Actions>
</Task>
""".format(start=start, python=escape(python), workdir=escape(workdir), user=escape(user),
           enabled="true" if enabled else "false")


def task_plan(data_dir, python=None, workdir=None, platform=None, user=None, name=TASK_NAME, enabled=True):
    """What creating the task means on this system: files to write and commands to run."""
    python = task_python(python)
    workdir = workdir or package_parent()
    platform = platform or sys.platform
    if platform.startswith("win"):
        xml_path = os.path.join(str(data_dir), name + ".xml")
        return {"files": {xml_path: task_xml(python, workdir, user or task_user(), enabled)},
                "create": ["schtasks", "/Create", "/TN", name, "/XML", xml_path, "/F"],
                "remove": ["schtasks", "/Delete", "/TN", name, "/F"],
                "query": ["schtasks", "/Query", "/TN", name, "/XML"]}
    cmd = 'cd "{}" && "{}" -B -m hive tick'.format(workdir, python)
    return {"files": {}, "cron": ["0 {} * * * {}".format(DAY_START_HOUR, cmd), "@reboot sleep 120 && " + cmd],
            "create": None, "remove": None, "query": None}


class TaskError(RuntimeError):
    pass


def _run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="oem" if os.name == "nt" else None,
                       errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def create_task(data_dir, dry_run=True, **kw):
    """Write the XML (Windows) and, unless dry_run, register the task. Returns the plan with `created`
    (False in a dry run and on Linux and macOS, where the cron lines are printed). Raises TaskError with
    schtasks' message when the registration fails."""
    plan = task_plan(data_dir, **kw)
    for path, text in plan["files"].items():
        with open(path, "w", encoding="utf-16") as fh:
            fh.write(text)
    plan["created"] = False
    if not dry_run and plan["create"]:
        code, out = _run(plan["create"])
        if code != 0:
            raise TaskError("schtasks /Create failed ({}): {}".format(code, out.strip()))
        plan["created"] = True
    return plan


def remove_task(data_dir, dry_run=True, **kw):
    """Unless dry_run, delete the task. Returns the plan with `removed` (None in a dry run)."""
    plan = task_plan(data_dir, **kw)
    plan["removed"] = None
    if not dry_run and plan["remove"]:
        code, _out = _run(plan["remove"])
        plan["removed"] = code == 0
    return plan
