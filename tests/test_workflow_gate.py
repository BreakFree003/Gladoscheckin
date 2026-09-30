"""workflow 里「今天是否已经签到成功」那一步 (id: today) 的测试。

为什么单独测这段: 它是整个仓库风险最高、却最难被发现的逻辑 ——
- ea5767a 修的就是它把北京日界点算错 16 小时。错的方向是"窗口往前多算一天",
  于是新的一天会被误判成"昨天已签到成功", 整天不再签到 (静默漏签, 比多签一次严重得多)。
- 它决定了 4 个槽位到底是"签到一次就够"还是"重复签到 4 次"。
- 它全写在 YAML 的 shell 里, 不在 python 的覆盖范围内。

所以这里不重写一遍公式, 而是把 YAML 里那段 shell **真的跑起来**, 断言它的性质:
窗口起点换算回北京时间必须正好是当天 00:00; 各分支的 skip 取值; 手动触发绕过。
"""

import datetime as dt
import os
import re
import subprocess

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "gladosCheck.yml")
BEIJING = dt.timezone(dt.timedelta(hours=8))

# 假的 gh: 记录自己被怎么调用, 再按环境变量决定返回什么。
# 真 gh 需要联网和 token, 这里替换掉外部边界, 只验证 gate 自己的判断。
GH_STUB = """#!/bin/sh
echo "$@" >> "$STUB_GH_CALLS"
if [ "${STUB_GH_FAIL:-}" = "1" ]; then
  echo "stub gh: boom" >&2
  exit 1
fi
printf '%s\\n' "${STUB_GH_COUNT:-0}"
"""


def _workflow() -> dict:
    with open(WORKFLOW_PATH, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _triggers() -> dict:
    """workflow 的 on: 段。

    注意 YAML 1.1 把裸写的 `on:` 当成布尔真, 所以 PyYAML 给出的键是 True 而不是 "on"。
    """
    workflow = _workflow()
    return workflow.get("on") or workflow[True]


def _steps() -> list:
    """workflow 的全部步骤。不写死作业名 (它被改过名), 但要求只有一个作业。"""
    jobs = _workflow()["jobs"]
    assert len(jobs) == 1, f"workflow 应当只有一个作业, 实际有 {list(jobs)}"
    (job,) = jobs.values()
    return job["steps"]


def _gate_step() -> dict:
    steps = [s for s in _steps() if s.get("id") == "today"]
    assert len(steps) == 1, f"workflow 里应当有且只有一个 id: today 的步骤, 实际 {len(steps)} 个"
    return steps[0]


def _gate_script() -> str:
    """从 workflow 里取出 gate 那一步要执行的 shell。

    从 YAML 现取而不是抄一份: 抄一份的话, 改了 workflow 而忘了改测试, 测试仍会绿。
    """
    return _gate_step()["run"]


def _run_gate(tmp_path, *, event: str = "schedule", count: str = "0", fail: bool = False):
    """在真 bash 里跑一次 gate。返回 (进程, GITHUB_OUTPUT 解析结果, gh 被调用的参数)。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(GH_STUB, encoding="utf-8")
    gh.chmod(0o755)

    calls = tmp_path / "gh_calls"
    calls.write_text("", encoding="utf-8")
    github_output = tmp_path / "github_output"
    github_output.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["GITHUB_REPOSITORY"] = "owner/repo"
    env["GITHUB_EVENT_NAME"] = event
    env["GITHUB_OUTPUT"] = str(github_output)
    env["STUB_GH_CALLS"] = str(calls)
    env["STUB_GH_COUNT"] = count
    env.pop("STUB_GH_FAIL", None)
    if fail:
        env["STUB_GH_FAIL"] = "1"

    proc = subprocess.run(
        ["bash", "-e", "-c", _gate_script()],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # workflow 与 bash 都靠 GITHUB_OUTPUT 传递结果, 所以断言这个文件而不是 stdout
    outputs = dict(re.findall(r"^([^=\n]+)=(.*)$", github_output.read_text(encoding="utf-8"), re.M))
    return proc, outputs, calls.read_text(encoding="utf-8")


def _since(proc: subprocess.CompletedProcess) -> dt.datetime:
    matched = re.search(r"北京时间今天 00:00 = (\S+) \(UTC\)", proc.stdout)
    assert matched, f"gate 没有打印窗口起点:\n{proc.stdout}\n{proc.stderr}"
    return dt.datetime.strptime(matched.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------
# 窗口起点: 必须是北京时间当天 00:00
# --------------------------------------------------------------------------


def test_gate_window_starts_at_beijing_midnight(tmp_path):
    """需求: 窗口只覆盖"北京时间今天"这一天。

    cron 用 UTC 而签到日界点是北京时间, 所以这里是最容易算错的地方:
    算错 16 小时那次, 这条会红, 且差值正好 16 小时。
    """
    proc, _, _ = _run_gate(tmp_path)

    since = _since(proc)
    beijing_now = dt.datetime.now(BEIJING)
    expected = beijing_now.replace(hour=0, minute=0, second=0, microsecond=0)

    assert since.astimezone(BEIJING) == expected, (
        f"窗口起点应是北京时间今天 00:00 ({expected}), 实际 {since.astimezone(BEIJING)}"
    )


def test_gate_window_covers_at_most_one_day(tmp_path):
    """窗口不能超过一天, 否则"今天"会把昨天也算进来, 新的一天会被误判成已签到。"""
    proc, _, _ = _run_gate(tmp_path)

    since = _since(proc)
    now = dt.datetime.now(dt.timezone.utc)

    assert since <= now < since + dt.timedelta(hours=24), (
        f"窗口起点 {since} 与当前时刻 {now} 相距应当小于 24 小时"
    )


# --------------------------------------------------------------------------
# 三个分支: 查到成功就跳过, 其余一律照常签到
# --------------------------------------------------------------------------


def test_gate_skips_when_today_already_has_a_success(tmp_path):
    """需求: 今天已经成功签到过, 后面的槽位直接跳过, 不再重复签到。"""
    proc, outputs, _ = _run_gate(tmp_path, count="3")

    assert outputs.get("skip") == "true", proc.stdout + proc.stderr
    assert "跳过本次签到" in proc.stdout


def test_gate_runs_when_today_has_no_success(tmp_path):
    proc, outputs, _ = _run_gate(tmp_path, count="0")

    assert outputs.get("skip") == "false", proc.stdout + proc.stderr


def test_gate_runs_when_the_history_query_fails(tmp_path):
    """失败模式: 查不到历史时宁可多签一次, 也绝不静默漏签。

    回归的是"gate 自己坏掉导致整天不签到"这一类故障 —— 那正是加槽位想解决的问题,
    gate 不能再把它引回来。
    """
    proc, outputs, _ = _run_gate(tmp_path, fail=True)

    assert outputs.get("skip") == "false", proc.stdout + proc.stderr
    assert "::warning::" in proc.stdout


def test_gate_runs_when_the_count_is_not_a_number(tmp_path):
    """失败模式: gh 返回了非数字 (报错文本等), 不能让比较报错或误判成已签到。"""
    proc, outputs, _ = _run_gate(tmp_path, count="not-a-number")

    assert outputs.get("skip") == "false", proc.stdout + proc.stderr


# --------------------------------------------------------------------------
# 手动触发与查询方式
# --------------------------------------------------------------------------


def test_gate_manual_dispatch_bypasses_skip_without_querying(tmp_path):
    """需求: 手动点 Run workflow 意味着"现在就跑一次", 不能被静默跳过。

    所以也不该去查历史记录 —— 查了反而多一次可能失败的外部调用。
    """
    proc, outputs, calls = _run_gate(tmp_path, event="workflow_dispatch", count="5")

    assert outputs.get("skip") == "false", proc.stdout + proc.stderr
    assert calls == "", f"手动触发不应查询运行记录, 实际的 gh 调用: {calls!r}"


def test_gate_queries_run_history_with_an_explicit_get(tmp_path):
    """回归: `gh api -f` 会把请求方法变成 POST, 必须显式 -X GET。

    少了它这个查询会 404 -> 每次都认为"今天没签到过" -> 4 个槽位全部重复签到。
    """
    _, _, calls = _run_gate(tmp_path, count="0")

    assert "-X GET" in calls, f"gh 调用里缺少显式的 -X GET: {calls!r}"
    assert "actions/workflows/gladosCheck.yml/runs" in calls, f"查询的接口不对: {calls!r}"


# --------------------------------------------------------------------------
# workflow 的接线: 这些改动不会让上面任何一条 gate 测试变红
# --------------------------------------------------------------------------


def test_workflow_keeps_several_schedule_slots_as_fallbacks():
    """失败模式: 计划任务被删掉, 或只剩一个槽位。

    GitHub 的 schedule 是 best-effort: 本仓库实测计划时间点会迟到 3~10 小时, 还会整段
    丢弃。只留一个槽位时, 一次漂移就是整天漏签 —— 而漏签没有任何日志、没有任何邮件。
    """
    schedule = _triggers()["schedule"]
    crons = [entry["cron"] for entry in schedule]

    assert len(crons) >= 2, f"至少要留一个兜底槽位, 实际只有 {crons}"
    for cron in crons:
        assert len(cron.split()) == 5, f"cron 应当是 5 个字段 (分 时 日 月 周): {cron!r}"


def test_the_gate_step_gets_a_token():
    """失败模式: gate 少了 GH_TOKEN。

    gate 用 gh api 查今天的成功运行; 没有 token 时 gh 会失败, 脚本回退成"照常签到"
    (带 ::warning::)。生产里 4 个槽位于是各签一次 —— 不致命, 但闸门等于没有。
    测试里看不见, 因为这里的测试自己造 env、直接把那段 shell 跑起来。
    """
    env = _gate_step().get("env") or {}

    assert "GH_TOKEN" in env, f"gate 需要 GH_TOKEN 才能查运行记录, 实际 env: {list(env)}"


def test_the_checkin_step_only_runs_when_the_gate_did_not_skip():
    """失败模式: 跑签到那一步的 if 被删掉, 或者方向写反。

    写反 (把 != 写成 ==) 的结果是"只有今天已经签过才签到": 整天不签到, 而作业是绿的 ——
    没有日志、没有邮件、没有测试会响。这正是本仓库最怕的那种静默漏签。
    """
    runners = [s for s in _steps() if "checkin.py" in (s.get("run") or "")]

    assert len(runners) == 1, f"应当只有一步跑 checkin.py, 实际 {len(runners)} 步"
    condition = runners[0].get("if", "")
    assert "steps.today.outputs.skip" in condition, f"跑签到的步骤没有被闸门控制: {condition!r}"
    assert "!=" in condition, f"条件方向应当是「闸门没跳过才跑」: {condition!r}"
