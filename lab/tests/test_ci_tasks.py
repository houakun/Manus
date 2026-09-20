#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CI 失败归因任务类的测试（新任务类的设计属性必须被钉住）。

== 这个文件守住三件新东西 ==
1. **`exec_script` 判定原语**：它防的是实测发现的漏洞 ——
   初版 ci 任务用 `json_equals` 检查产物，而"手写产物 + 一行代码不改"的解**能过关**。
   这是这个任务类最致命的失效模式（整个类的意义就是"你有没有修好根因"），
   所以必须有回归测试，而不是靠我在文档里声称。
2. **fixture 真实性自检（`SelfCheck`）**：CI 日志是 Agent 的主要输入，
   而它是我手写的 —— 实测两份都写错了（一份说 PASSED 实际 FAILED，
   一份说 SKIPPED 实际 FAILED）。三次常规自检都抓不到，因为**没有人执行过那份日志**。
3. **脚本里不能出现沙箱绝对路径**：脚本内容不会被沙箱路径映射改写，
   写 `/home/ubuntu/...` 会落到工作区外（本项目已经在这上面栽过一次，
   见 `LocalSandbox._escaped_root`）。
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

from lab.bench.task import load_all_tasks
from lab.bench.validate import _fixture_truth_problems
from lab.bench.verifier import run_checks
from lab.infra.local_sandbox import LocalSandbox


def _ci_task(key: str):
    task = next((t for t in load_all_tasks(group="ci") if t.key == key), None)
    assert task is not None, f"找不到 ci 任务 {key}"
    return task


async def _prepare(task, tmp_path) -> LocalSandbox:
    sandbox = LocalSandbox(pathlib.Path(tmp_path) / "ws", exec_timeout=60)
    await sandbox.ensure_sandbox()
    for fixture in task.fixtures:
        await sandbox.write_file(fixture.path, fixture.content)
    return sandbox


# ==================== 1. 反作弊：不许"只写产物" ====================

@pytest.mark.parametrize("key", ["ci_window_boundary", "ci_shared_cache_state"])
async def test_exec_script_rejects_artifact_only_solution(key, tmp_path):
    """只写"看起来对"的诊断报告、一行代码都不改 → 必须被拒绝。

    == 这条测试的来历 ==
    初版两个 ci 任务都用 `json_equals` 检查产物 out.json。
    我写了个探针跑了一下，两个都**被放行** —— 也就是说，
    一个完全不理解 bug、只会编答案的解能拿到 100% 成功率。
    换成 `exec_script` 之后探针被正确拒绝，这条测试就是那次发现的固化。
    """
    task = _ci_task(key)
    sandbox = await _prepare(task, tmp_path)
    # 手写一份"看似正确"的归因（把两个任务的关键词都塞进去，最大化作弊成功率）
    await sandbox.write_file(
        "/home/ubuntu/diagnosis.json",
        '{"root_causes": [{"file": "window.py", "symbol": "window"}, '
        '{"file": "settings.py", "symbol": "default_tags"}, '
        '{"file": "cache.py", "symbol": "_store"}]}',
    )

    checks = await run_checks(task.verify, sandbox)
    failed_kinds = [c.kind for c in checks if not c.ok]

    assert "exec_script" in failed_kinds, (
        "执行型检查没有拦住'只写产物'的解 —— 这个任务类的判定就是假的"
    )


@pytest.mark.parametrize("key", ["ci_window_boundary", "ci_shared_cache_state"])
async def test_weakening_the_frozen_test_runner_does_not_help(key, tmp_path):
    """把工作区里的测试运行器改成 `sys.exit(0)` **也不能**过关。

    为什么能做到：`exec_script` 的脚本存在**任务定义**里，不在工作区里 ——
    Agent 改不动它。如果改成"执行工作区的测试文件"，这一条就会失败。
    """
    task = _ci_task(key)
    sandbox = await _prepare(task, tmp_path)
    await sandbox.write_file(
        "/home/ubuntu/repo/tests/run_tests.py",
        "# CI-FROZEN: 本文件由 CI 提供，不得修改\nimport sys\nsys.exit(0)\n",
    )
    await sandbox.write_file("/home/ubuntu/diagnosis.json", '{"root_causes": ["whatever"]}')

    checks = await run_checks(task.verify, sandbox)
    assert any(c.kind == "exec_script" and not c.ok for c in checks)

    # 同时：哨兵检查也要报（两处独立防线，不能只靠一条）
    assert any(c.kind == "file_content" and not c.ok for c in checks)


async def test_exec_script_failure_detail_carries_output(tmp_path):
    """执行型检查失败时，detail 里必须带脚本输出。

    否则报告里只有"退出码 1"，读者无法判断是任务太难、判定器写错、
    还是环境（解释器/路径）出了问题 —— 这三种情况的处置完全不同。
    """
    task = _ci_task("ci_shared_cache_state")
    sandbox = await _prepare(task, tmp_path)

    checks = await run_checks(task.verify, sandbox)
    exec_check = next(c for c in checks if c.kind == "exec_script")

    assert not exec_check.ok
    assert "输出尾部" in exec_check.detail
    assert "FAIL" in exec_check.detail


async def test_exec_script_runs_the_fixed_code(tmp_path):
    """反向验证：把参考解铺上去之后，执行型检查必须通过。

    只测"失败路径"是不够的 —— 一个永远返回非 0 的执行器也能让上面三条全过。
    """
    task = _ci_task("ci_window_boundary")
    sandbox = await _prepare(task, tmp_path)
    for step in task.solution:
        if step.kind == "write_file":
            result = await sandbox.write_file(step.path, step.content or "")
            assert result.success

    checks = await run_checks(task.verify, sandbox)

    assert all(c.ok for c in checks), [c.line() for c in checks if not c.ok]


# ==================== 2. fixture 真实性自检 ====================

async def test_selfcheck_catches_a_fabricated_ci_log(tmp_path):
    """把日志改成"某个失败用例其实 PASSED" → 自检必须报出来。

    这是**真实事故**的固化：我手写的第一版日志说
    `test_page_zero_is_unaffected ... PASSED`，而实际是 FAILED。
    三次常规自检（fixtures_only / reference_solution / wrong_solution）
    全部正常 —— 因为没有任何一步执行过那份日志。
    """
    task = _ci_task("ci_window_boundary")
    lying = task.fixtures[0].content.replace(
        "test_first_page_keeps_all_items ... FAILED",
        "test_first_page_keeps_all_items ... PASSED",
    ).replace("      6 项：2 通过 / 4 失败", "      6 项：3 通过 / 3 失败")
    assert lying != task.fixtures[0].content, "替换没生效，测试本身写错了"

    patched = task.model_copy(update={"fixtures": [
        task.fixtures[0].model_copy(update={"content": lying}), *task.fixtures[1:]
    ]})

    with tempfile.TemporaryDirectory() as tmp:
        problems = await _fixture_truth_problems(patched, pathlib.Path(tmp) / "truth")

    assert problems, "撒谎的 CI 日志没有被自检发现"
    assert any("fixture 真实性" in p for p in problems)
    assert any("test_first_page_keeps_all_items" in p for p in problems)


async def test_selfcheck_passes_for_the_real_logs(tmp_path):
    """反向验证：现仓库里两份日志都是真的（否则这个自检本身没被用起来）。"""
    for key in ("ci_window_boundary", "ci_shared_cache_state"):
        task = _ci_task(key)
        with tempfile.TemporaryDirectory() as tmp:
            problems = await _fixture_truth_problems(task, pathlib.Path(tmp) / "truth")
        assert problems == [], f"{key} 的日志与实际行为不符：{problems}"


def test_every_ci_task_declares_a_selfcheck():
    """新任务类里"输入是日志"的任务都应该声明自检。

    不要求全局必填（多数任务的输入是数据文件，没有这个需求），
    但 ci 组的输入**就是**一份日志，声明它不是可选项。
    """
    for task in load_all_tasks(group="ci"):
        assert task.selfcheck is not None, f"{task.uid} 没声明 selfcheck，日志无人验证"


# ==================== 3. 脚本里不许有沙箱绝对路径 ====================

def test_ci_scripts_avoid_sandbox_absolute_paths():
    """脚本内容不会被路径映射改写 —— 写 `/home/ubuntu/...` 会落到工作区外。

    本项目已经在这上面栽过一次（参考解用绝对路径 → 产物跑到盘根，
    判定器读不到 → 正确的工作被判失败）。所以新任务里不能出现这种写法：
    脚本应该用 `os.getcwd()` / `__file__` 推导路径。

    注意：**fixture 的 `path` 字段**当然要用沙箱路径（那是它的用途），
    这里检查的是**内容**（fixture 正文与内联脚本）。
    """
    offenders = []
    for task in load_all_tasks(group="ci"):
        for fixture in task.fixtures:
            if "/home/ubuntu" in fixture.content:
                offenders.append(f"{task.uid} fixture {fixture.path}")
        for check in task.verify:
            if check.script and "/home/ubuntu" in check.script:
                offenders.append(f"{task.uid} verify 内联脚本")
        for step in [*task.solution, *task.wrong]:
            if step.content and "/home/ubuntu" in step.content:
                offenders.append(f"{task.uid} 参考解/错误解 {step.path}")
    assert not offenders, f"这些内容里有沙箱绝对路径（会逃到工作区外）：{offenders}"


# ==================== 4. 检查脚本不许留在沙箱里（真实泄漏）====================

def test_exec_script_command_cannot_be_mangled_by_path_rewriting():
    """**CI 破坏级**不变式：检查命令不能被沙箱的路径映射改写。

    == 这条测试的来历（真实踩到的坑，而且本地看不出来）==
    初版把脚本写成临时文件、把**路径**放进命令。两代都错：

    1. 写在沙箱内的 `/tmp/` → 同任务的下一次运行能读到上一次的评分脚本
       （真实轨迹里 Agent 就在找它）；
    2. 改写到沙箱**外面** → `_rewrite_paths` 会把命令里任何 `/tmp` 换成
       `<沙箱根>/tmp`，而 Linux 上 `tempfile.TemporaryDirectory()` 就在 `/tmp` 下
       → 路径被改写、脚本找不到、**validate 在 CI 上必挂**。
       Windows 上临时目录在 AppData，所以我本机跑得好好的。

    现在根本不用文件：脚本 base64（URL-safe）后过 `-c`。
    因为 URL-safe base64 的字母表不含 `/`，命令里**不可能**出现
    `/tmp` 或 `/home/ubuntu`，改写必然是无操作。
    """
    from lab.bench.verifier import _exec_script_command
    from lab.infra.local_sandbox import LocalSandbox

    script = "import sys\nprint('hi')\nsys.exit(0)\n"
    command = _exec_script_command(script)

    assert "/tmp" not in command
    assert "/home/ubuntu" not in command

    # 用“故意把沙箱根放在一个含 /tmp 的路径下”的沙箱来验：即使这样也不该被改写
    sandbox = LocalSandbox(pathlib.Path("C:/tmp/ws") if os.name == "nt" else pathlib.Path("/tmp/ws"))
    assert sandbox._rewrite_paths(command) == command, (
        "命令被路径映射改写了 —— 这会在 Linux/CI 上让检查脚本找不到"
    )


def test_exec_script_command_rejects_overlong_scripts():
    """脚本过长时要**显式报错**，不能静默截断。

    静默截断会造出一个“永远通过”或“永远失败”的判定器 ——
    比报错危险得多（前者抬高成功率，后者让所有加固看起来都没用）。
    """
    from lab.bench.verifier import _EXEC_SCRIPT_COMMAND_LIMIT, _exec_script_command

    assert len(_exec_script_command("x = 1\n")) < _EXEC_SCRIPT_COMMAND_LIMIT
    assert len(_exec_script_command("x = 1\n" * 5000)) > _EXEC_SCRIPT_COMMAND_LIMIT


async def test_exec_script_leaves_no_files_behind(tmp_path):
    """跑完判定不许留下任何文件（既防泄漏，也防污染下一次运行）。

    初版会在工作区里留 `tmp/_lab_check_*.py`，而工作区按 run 保留 →
    下一次运行读得到上一次的**评分标准**。
    """
    task = _ci_task("ci_window_boundary")
    sandbox = await _prepare(task, tmp_path)
    await run_checks(task.verify, sandbox)

    leftovers = [
        p for p in pathlib.Path(sandbox._root).rglob("*")
        if p.is_file() and p.name.startswith(("_lab_check", "check_"))
    ]
    assert leftovers == [], f"沙箱里残留了检查脚本：{leftovers}"

    # 沙箱隔壁也不应该有临时的校验目录
    assert not (pathlib.Path(sandbox._root).parent / "_lab_verify").exists()


async def test_check_script_is_not_reachable_through_sandbox_reads(tmp_path):
    """即使知道路径，沙箱工具也读不到检查脚本（它根本不在根目录里）。"""
    task = _ci_task("ci_window_boundary")
    sandbox = await _prepare(task, tmp_path)
    await run_checks(task.verify, sandbox)

    for logical in ("/tmp/_lab_check_anything.py", "/../_lab_verify/check_x.py",
                    "/home/ubuntu/../_lab_verify/check_x.py"):
        result = await sandbox.read_file(logical)
        assert not result.success, f"{logical} 竟然可读 —— 判定标准泄漏了"


# ==================== 5. 工作区复用必须从干净状态开始 ====================

def test_reset_workspace_removes_stale_artifacts(tmp_path):
    """重跑同一个 run 目录时，上一次的产物 / 评分脚本必须被清掉。

    == 为什么这条很重要（真实测到的泄漏）==
    "每次运行一个独立工作区"只对**不同 run_index** 成立。
    重跑同一批任务时 `run0` 会被复用，而上一次的产物与
    `tmp/_lab_check_*.py`（判定层的评分脚本）都还在里面：
      - 产物残留 → 这一次"什么都不做也通过"，成功率虚高且不报错；
      - 评分脚本残留 → Agent 可以去读判定标准（实测已经在读了，只是没猜到文件名）。
    """
    from lab.bench.runner import _reset_workspace

    ws = tmp_path / "bench" / "ci" / "task" / "run0" / "workspace"
    (ws / "tmp").mkdir(parents=True)
    (ws / "repo").mkdir(parents=True)
    (ws / "tmp" / "_lab_check_deadbeef.py").write_text("print(1)", encoding="utf-8")
    (ws / "repo" / "answer.txt").write_text("上次的答案", encoding="utf-8")
    (ws / "stale.txt").write_text("x", encoding="utf-8")

    _reset_workspace(ws)

    assert ws.is_dir(), "清完之后目录本身要留着"
    assert list(ws.iterdir()) == [], f"仍有残留：{list(ws.iterdir())}"


def test_reset_workspace_is_idempotent_on_missing_dir(tmp_path):
    """目录不存在时也要能工作（首次运行）。"""
    from lab.bench.runner import _reset_workspace

    ws = tmp_path / "nope" / "workspace"
    _reset_workspace(ws)
    assert ws.is_dir()
