#!/usr/bin/env bash
#
# scripts/verify.sh —— 一键可重复验证（发布前最后一道）
#
# 装依赖 → 生成评估表 → 跑一轮 fixture 评估 → doctor 自检 → 断言产物存在。
# 退出码 0 = 全绿；非 0 = 哪一步没过（每一步都打印中文原因，不留悬念）。
#
# 用法：
#   bash scripts/verify.sh              # 正常跑一遍（临时目录跑完即清）
#   bash scripts/verify.sh --keep       # 保留临时目录，便于排查
#   PYTHON=python3.11 bash scripts/verify.sh
#
# **这条脚本只做本地验证**：它起的替身在 tests/ 层（协议替身 —— 真 socket、
# 真 requests.Session、真 Authorization 头、真 HTTP 状态码），产品代码一行不改，
# 默认出站路径永远不命中替身。替身门是显式开关（--fixture / IMAGING_EVAL_FIXTURE=1），
# 本脚本**只在跑 fixture 的那一个子进程里**打开它，doctor 那一段走的是默认路径。
#
# **它不代表真实院内端点跑通**：真端点要凭据（见 .env.example），
# 院内装好后请按 docs/install.md 配好 .env 再跑 `nodule-eval doctor`。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
KEEP=0
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) printf '未知参数：%s（可用：--keep）\n' "$arg" >&2; exit 2 ;;
  esac
done

# src/ 永远在 PYTHONPATH 前面：装没装成功这条脚本都能跑（院内离线机常见）。
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONIOENCODING=utf-8

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/nodule-eval-verify.XXXXXX")"
cleanup() {
  # 可编辑安装会在源码树里留下 src/*.egg-info —— 那是构建产物，
  # 验证脚本自己装的，就该自己带走（.gitignore 也已排除它）。
  rm -rf "$ROOT"/src/*.egg-info
  if [ "$KEEP" -eq 1 ]; then
    printf '\n[保留] 临时目录：%s\n' "$WORKDIR"
  else
    rm -rf "$WORKDIR"
  fi
}
trap cleanup EXIT

STEP=0
TOTAL_STEPS=6
step() { STEP=$((STEP + 1)); printf '\n[%d/%d] %s\n' "$STEP" "$TOTAL_STEPS" "$1"; }
ok()   { printf '      [OK] %s\n' "$1"; }
warn() { printf '      [!]  %s\n' "$1"; }
die()  { printf '      [FAIL] %s\n' "$1" >&2; exit 1; }

printf '肺结节AI模型评估系统 · 一键验证\n'
printf '项目目录：%s\n' "$ROOT"
printf '临时目录：%s\n' "$WORKDIR"

# ---------------------------------------------------------------------------
step "检查运行环境（Python 版本）"
command -v "$PY" >/dev/null 2>&1 || die "找不到 Python 解释器（可用 PYTHON=... 指定，例如 PYTHON=python3.11）"
"$PY" - <<'PY' || die "Python 版本过低（要求 ≥ 3.10）"
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
ok "解释器：$("$PY" -c 'import sys; print(sys.executable, sys.version.split()[0])')"

# ---------------------------------------------------------------------------
step "安装依赖（pip install -e .；离线机回退到依赖可导入检查）"
if "$PY" -m pip install --disable-pip-version-check -q -e . >"$WORKDIR/pip.log" 2>&1; then
  ok "pip install -e . 成功（包与运行期依赖就位）"
else
  warn "pip install -e . 失败（这台机器多半没有外网；日志：$WORKDIR/pip.log）"
  if "$PY" - <<'PY' >/dev/null 2>&1
import importlib
for name in ("requests", "yaml", "openpyxl"):
    importlib.import_module(name)
PY
  then
    ok "运行期依赖已在本机（requests / PyYAML / openpyxl），继续验证"
  else
    die "依赖缺失且无法安装 —— 先按 docs/install.md 离线装好 wheel 再重跑"
  fi
fi

# ---------------------------------------------------------------------------
step "生成评估表模板（scripts/build_xlsm.py）"
TEMPLATE="$WORKDIR/评估表模板.xlsx"
"$PY" scripts/build_xlsm.py --out "$TEMPLATE" >"$WORKDIR/build_xlsm.log" 2>&1 \
  || die "评估表模板生成失败（日志：$WORKDIR/build_xlsm.log）"
[ -f "$TEMPLATE" ] || die "评估表模板没落盘：$TEMPLATE"
"$PY" - "$TEMPLATE" <<'PY' || die "评估表模板列不对（既有三列 + 新增六列应当齐备）"
import sys

from openpyxl import load_workbook

from nodule_eval.sheets.columns import ALL_COLUMNS, EXISTING_COLUMNS, NEW_COLUMNS
from nodule_eval.sheets.template import HEADER_ROW

workbook = load_workbook(sys.argv[1])
worksheet = workbook["评估表"]
header = [
    worksheet.cell(row=HEADER_ROW, column=column).value
    for column in range(1, len(ALL_COLUMNS) + 1)
]
assert tuple(header) == tuple(ALL_COLUMNS), f"表头不对：{header}"
assert len(EXISTING_COLUMNS) == 3 and len(NEW_COLUMNS) == 6, "列数口径变了"
workbook.close()
print(
    "      [OK] 评估表模板已生成，既有三列（"
    + " / ".join(EXISTING_COLUMNS)
    + "）+ 新增六列（"
    + " / ".join(NEW_COLUMNS)
    + "）齐备"
)
PY

# ---------------------------------------------------------------------------
step "跑一轮 fixture 评估（真 socket 替身 + 真 CLI 子进程）"
VERIFY_ROOT="$ROOT" VERIFY_WORKDIR="$WORKDIR" "$PY" - <<'PY' || die "fixture 评估没跑通（上面的输出里有失败原因）"
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ["VERIFY_ROOT"])
WORK = Path(os.environ["VERIFY_WORKDIR"])
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from stub_inference import InferenceStub  # noqa: E402
from stub_research_platform import ResearchPlatformStub  # noqa: E402
from stub_ris import RISStub  # noqa: E402

RUN_ID = "verify-smoke"
RUNS_DIR = WORK / "runs"
CASES_PATH = WORK / "cases.json"

# 三份替身：RIS（权威数据源）、推理服务（被评模型）、科研平台（登记端）。
# 替身只在 tests/ 层，产品代码不 import 它。
#
# RIS 清单给三条 —— 绑定层以 RIS 为真源逐条核检查号，
# 少给一条那一例就会被挂起（不静默丢），所以这里给全。
ris = RISStub(
    items=[
        {
            "accession_no": "DEMO-0001",
            "study_uid": "STUDY-DEMO-001",
            "modality": "DX",
            "study_date": "2026-03-01",
            "report_status": "final",
            "image_ref": "dx://demo/study-001",
        },
        {
            "accession_no": "DEMO-0002",
            "study_uid": "STUDY-DEMO-002",
            "modality": "DX",
            "study_date": "2026-03-01",
            "report_status": "final",
            "image_ref": "dx://demo/study-002",
        },
        {
            "accession_no": "DEMO-0003",
            "study_uid": "STUDY-DEMO-003",
            "modality": "DX",
            "study_date": "2026-03-01",
            "report_status": "final",
            "image_ref": "dx://demo/study-003",
        },
    ]
)
inference = InferenceStub(fail_study_uids={"STUDY-DEMO-003"})
research = ResearchPlatformStub()
for stub in (ris, inference, research):
    stub.start()

try:
    # 三例，覆盖三种结局：命中 / 误报 / 调用失败。
    # DEMO-0002 只给检查号，影像标识由 RIS 按检查号绑定（顺带跑通 RIS 读面）。
    cases = {
        "run_id": RUN_ID,
        "model_id": "demo-model",
        "model_version": "v1.0.0",
        "dataset_ref": "verify-smoke",
        "cases": [
            {
                "accession_no": "DEMO-0001",
                "gold_standard_text": "右下肺见一磨玻璃结节，最大径约8mm",
                "image_ref": "dx://demo/study-001",
                "study_uid": "STUDY-DEMO-001",
            },
            {
                "accession_no": "DEMO-0002",
                "gold_standard_text": "双肺未见明显结节影",
            },
            {
                "accession_no": "DEMO-0003",
                "gold_standard_text": "右下肺见实性结节，最大径约12mm",
                "image_ref": "dx://demo/study-003",
                "study_uid": "STUDY-DEMO-003",
            },
        ],
        "options": {"size_tolerance_mm": 2.0, "concurrency": 1, "resume": False},
    }
    CASES_PATH.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")

    env = os.environ.copy()
    # 十二个必配环境变量：端点地址 + 令牌 + 模型参数（占位值，真跑时换成院内的）。
    env.update(
        {
            "RIS_BASE_URL": "https://ris.hospital.invalid",
            "RIS_API_TOKEN": "verify-ris-token",
            "INFERENCE_BASE_URL": "https://inference.hospital.invalid",
            "INFERENCE_API_TOKEN": "verify-inference-token",
            "RESEARCH_PLATFORM_BASE_URL": "https://research.hospital.invalid",
            "RESEARCH_PLATFORM_TOKEN": "verify-research-token",
            "OPENAI_BASE_URL": "https://model.hospital.invalid",
            "OPENAI_API_KEY": "verify-model-token",
            "OPENAI_MODEL": "verify-model",
            "MODEL_TIMEOUT": "60",
            "MODEL_MAX_RETRIES": "3",
            "MODEL_TEMPERATURE": "0",
            # 替身门：只在这一次子进程里显式打开，四个出站面全走替身。
            "IMAGING_EVAL_FIXTURE": "1",
            "IMAGING_EVAL_FIXTURE_RIS_BASE_URL": ris.base_url,
            "IMAGING_EVAL_FIXTURE_INFERENCE_BASE_URL": inference.base_url,
            "IMAGING_EVAL_FIXTURE_RESEARCH_BASE_URL": research.base_url,
            "IMAGING_EVAL_FIXTURE_MODEL_BASE_URL": research.base_url,
        }
    )

    command = [
        sys.executable,
        "-m",
        "nodule_eval.cli",
        "run",
        "--cases",
        str(CASES_PATH),
        "--model-id",
        "demo-model",
        "--run-id",
        RUN_ID,
        "--runs-dir",
        str(RUNS_DIR),
        "--store",
        str(WORK / "store.sqlite"),
        "--fixture",
    ]
    completed = subprocess.run(
        command, cwd=str(ROOT), env=env, capture_output=True, text=True
    )
    print("      ---- CLI 输出 ----")
    for line in (completed.stdout or "").splitlines():
        print(f"      {line}")
    if completed.returncode != 0:
        print("      ---- stderr ----")
        for line in (completed.stderr or "").splitlines():
            print(f"      {line}")
        raise SystemExit(f"CLI 退出码 {completed.returncode}（期望 0）")

    # ---- 断言产物 ----
    run_dir = RUNS_DIR / RUN_ID
    failures: list[str] = []

    def need(path: Path) -> bool:
        if not path.is_file():
            failures.append(f"缺产物：{path}")
            return False
        return True

    need(run_dir / "case_rows.csv")
    need(run_dir / "tool_calls.jsonl")
    need(run_dir / "summary.json")
    need(run_dir / "missed_list.csv")

    verdicts: list[str] = []
    if (run_dir / "case_rows.csv").is_file():
        import csv

        with open(run_dir / "case_rows.csv", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        verdicts = [row.get("verdict", "") for row in rows]
        if len(rows) != 3:
            failures.append(f"逐例明细应有 3 行，实际 {len(rows)} 行")
        for expected in ("hit", "false_positive", "failed"):
            if expected not in verdicts:
                failures.append(f"判定值缺 {expected}（实际：{verdicts}）")

    if (run_dir / "tool_calls.jsonl").is_file():
        lines = [
            line
            for line in (run_dir / "tool_calls.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(lines) != 3:
            failures.append(f"调用底账应有 3 条，实际 {len(lines)} 条")
        for line in lines:
            record = json.loads(line)
            if not record.get("tool_call_id"):
                failures.append("调用底账有一行没有 tool_call_id")

    summary = {}
    if (run_dir / "summary.json").is_file():
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        counts = summary.get("counts") or {}
        if counts.get("tp") != 1 or counts.get("fp") != 1:
            failures.append(f"原始计数不对（期望 tp=1 fp=1，实际 {counts}）")
        if counts.get("fn") != 0 or counts.get("tn") != 0:
            failures.append(f"误报不该算成漏诊（实际 {counts}）")
        if counts.get("failed") != 1:
            failures.append(f"失败例应单列 1 例（实际 {counts}）")
        if counts.get("counted") != 2:
            failures.append(f"进分母的应是 2 例（失败例不进分母），实际 {counts.get('counted')}")
        if summary.get("verdict_distribution", {}).get("failed") != 1:
            failures.append(f"判定分布里没有 failed 这一栏（实际 {summary.get('verdict_distribution')}）")

    # 替身真的收到了请求（协议面跑通，不是把 HTTP 层 mock 掉）。
    posts = [r for r in inference.requests if r["method"] == "POST"]
    ok_responses = [r for r in inference.responses if r["status"] == 200]
    failed_responses = [r for r in inference.responses if r["status"] == 500]
    # 两例各调 1 次成功 + 失败那例按退避重试 3 次 = 5 次出站请求。
    if len(posts) != 5:
        failures.append(f"推理端点应收 5 次 POST（2 例各 1 次 + 失败例退避重试 3 次），实际 {len(posts)} 次")
    if len(ok_responses) != 2:
        failures.append(f"推理端点应回 2 次 200，实际 {len(ok_responses)} 次")
    if len(failed_responses) != 3:
        failures.append(f"推理端点应回 3 次 500（退避重试到上限），实际 {len(failed_responses)} 次")
    if not any(r["method"] == "GET" for r in ris.requests):
        failures.append("RIS 读面没被调用（检查号应经 RIS 绑定影像标识）")

    if failures:
        for item in failures:
            print(f"      [FAIL] {item}")
        raise SystemExit(1)

    print("      [OK] 三例跑完：命中 / 误报 / 调用失败各一例，失败例不进指标分母")
    print(
        f"      [OK] 推理端点收到 {len(posts)} 次 POST（2 次成功 + 失败例退避重试 3 次）、"
        f"RIS 收到 {sum(1 for r in ris.requests if r['method'] == 'GET')} 次 GET（真 socket、真 Bearer 头）"
    )
    print("      [OK] 产物齐备：case_rows.csv / tool_calls.jsonl / summary.json / missed_list.csv")
finally:
    for stub in (ris, inference, research):
        stub.stop()
PY

# ---------------------------------------------------------------------------
step "doctor 自检（默认路径：真实端点由环境变量给出，doctor 不发起任何出站调用）"
DOCTOR_LOG="$WORKDIR/doctor.log"
set +e
env \
  RIS_BASE_URL="https://ris.hospital.invalid" \
  RIS_API_TOKEN="verify-ris-token" \
  INFERENCE_BASE_URL="https://inference.hospital.invalid" \
  INFERENCE_API_TOKEN="verify-inference-token" \
  RESEARCH_PLATFORM_BASE_URL="https://research.hospital.invalid" \
  RESEARCH_PLATFORM_TOKEN="verify-research-token" \
  OPENAI_BASE_URL="https://model.hospital.invalid" \
  OPENAI_API_KEY="verify-model-token" \
  OPENAI_MODEL="verify-model" \
  MODEL_TIMEOUT="60" \
  MODEL_MAX_RETRIES="3" \
  MODEL_TEMPERATURE="0" \
  "$PY" -m nodule_eval.cli doctor \
    --store "$WORKDIR/store.sqlite" \
    --runs-dir "$WORKDIR/runs" \
  >"$DOCTOR_LOG" 2>&1
DOCTOR_CODE=$?
set -e
sed 's/^/      /' "$DOCTOR_LOG"
[ "$DOCTOR_CODE" -eq 0 ] || die "doctor 退出码 ${DOCTOR_CODE}（期望 0；日志：${DOCTOR_LOG}）"
grep -q "自检通过" "$DOCTOR_LOG" || die "doctor 输出里没有「自检通过」"
ok "doctor 自检通过（配置齐备、模型目录可读、运行库可写）"

# ---------------------------------------------------------------------------
step "断言交付物在位（可直接发布到 GitHub 的最小集合）"
MISSING=""
LEFTOVER=""
for item in \
  README.md \
  LICENSE \
  requirements.txt \
  pyproject.toml \
  .env.example \
  .gitignore \
  qrcode.jpg \
  models \
  prompts \
  scripts \
  src/nodule_eval \
  tests \
  docs/concepts \
  docs/screenshots \
  docs/install.md \
  docs/cli.md \
  docs/macro_install.md \
  ; do
  [ -e "$ROOT/$item" ] || MISSING="$MISSING $item"
done
[ -z "$MISSING" ] || die "交付物缺失：$MISSING"
ok "交付物齐备（README / LICENSE / 依赖 / 模型目录 / 提示词 / 脚本 / 源码 / 测试 / 文档 / 截图）"

SCREENSHOTS="$(find "$ROOT/docs/screenshots" -maxdepth 1 -name '*.png' | wc -l | tr -d ' ')"
[ "$SCREENSHOTS" -ge 1 ] || die "docs/screenshots/ 里没有真实运行截图"
ok "真实运行截图 $SCREENSHOTS 张（docs/screenshots/）"

# 运行时产物不该留在仓库里（这一段是「发布前清理」的自检）。
LEFTOVER=""
for item in runs logs __pycache__ .pytest_cache .mypy_cache .ruff_cache .env; do
  if [ -e "$ROOT/$item" ]; then
    LEFTOVER="$LEFTOVER $item"
  fi
done
if [ -n "$LEFTOVER" ]; then
  warn "发现运行时残留：${LEFTOVER}（已在 .gitignore 内，不会进 GitHub；如需干净目录请手动删除）"
else
  ok "无运行时残留（runs/ logs/ 缓存 .env 均不在项目目录里）"
fi

printf '\n全部通过：依赖就位、评估表可生成、主路径跑通、doctor 自检通过、交付物齐备。\n'
printf '提示：真实院内端点要凭据，按 .env.example 配好后跑 `nodule-eval doctor` 再接活。\n'
exit 0
