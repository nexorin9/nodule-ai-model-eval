# 宏安装说明（Excel VBA / WPS JSA）

> 面向信息科：把三个按钮装进评估表，让放射科**在表里点一下**就能跑一轮评估。
> 全程只有三个按钮：**开始评估** / **确认提交** / **导出结论表**。

---

## 0. 先看一眼：宏到底做了什么

| 按钮 | 干了什么 | 出站请求 |
|------|----------|----------|
| **开始评估** | 读明细区的既有三列 → 写成 `req.json` → 起本机引擎 → 逐例调推理端点 → 把判定、模型返回、失败原因、调用记录 ID 回填到**新增六列**，并把四项指标写进**表头汇总区** | 引擎侧照常调 RIS / 推理端点（宏自己不碰凭据） |
| **确认提交** | 人工看过表之后，把这一轮结论**幂等**写回科研平台评估登记 | 引擎侧一次 `POST .../model-evaluation-runs` |
| **导出结论表** | 导出结论工作簿（xlsx）与漏诊清单（csv） | 无（读本地产物） |

宏**只做三件事**：把表里的行变成请求、把引擎的结果变成表里的判定、把人工确认变成一次写回。

**跑的过程中表在说话**：引擎把进度写进 `runs/<run_id>/engine_status.json`，
宏每半秒读一次，把「已处理 N/总数 · 当前例号 · 心跳」刷到**底部状态栏**——
表格不冻结（Excel 侧用 `DoEvents`，WPS 侧用轮询空转），人随时能切走、截图、取消。
文案由引擎渲染好写进状态文件，宏只搬那一行 —— 两边不各写一份中文说法。

**它绝不碰的东西**：

- **既有三列一个字都不改**（`accession_no` / `gold_standard_text` / `image_ref`）——
  回填只写新增六列，金标准原文逐字保留；
- **不碰凭据** —— RIS / 推理 / 科研平台 / 模型四个出站面的凭据一律从**环境变量**读，
  宏的配置文件里不放任何 token；
- **不自动写回** —— 不点「确认提交」，引擎的写回闸门一次出站调用都不发。

---

## 1. 装之前：三样东西

| 要什么 | 从哪来 | 说明 |
|--------|--------|------|
| Python 3.11+ | 院内信息科自建 | 引擎跑在本机，**数据不出院内** |
| 项目目录 | 本仓库 | 含 `src/` 与 `models/`；引擎从这里启动 |
| 凭据环境变量 | 院内配置 | 12 个变量，见 `.env.example`；**不要写进宏的配置文件** |

先在命令行自检一次（**不发任何出站调用**）：

```bash
cd <项目目录>
python -m nodule_eval.cli doctor
```

doctor 通过（环境变量齐、模型目录可读、存储目录可写）再往下走。

---

## 2. Excel（VBA）安装

### 2.1 转码（**必做**）

代码仓库里的 `.bas` 是 UTF-8，VBA 编辑器按系统 ANSI 代码页读它。
中文 Windows 是 GBK —— 不转码直接导入，中文提示会变成乱码（功能不受影响，但没法给用户看）。

```bash
python scripts/convert_bas_to_ansi.py src/nodule_eval/sheets/xlsm_vba
```

输出 `nodule_eval.ansi.bas` 与 `nodule_eval_support.ansi.bas`
（**不覆盖仓库里的原文件**，原文件始终是 UTF-8 真源）。

### 2.2 导入模块

1. 打开 Excel → `Alt + F11` 打开 VBA 编辑器；
2. 「文件 → 导入文件」→ 依次导入上面两个 `.ansi.bas`；
   （**不要整段粘贴** —— 文件头有 `Attribute VB_Name`，粘贴会编译报错）
3. 「插入 → 模块」不用做 —— 两个文件各自带模块名。

导入后模块列表里应出现两个模块：

| 模块 | 作用 |
|------|------|
| `NoduleEval` | 三个按钮的业务流程（读表 → 请求 → 回填） |
| `NoduleEvalSupport` | 底层动作（UTF-8 读写 / JSON / CSV / 起进程） |

### 2.3 启用宏与信任目录

院内 Excel 默认禁用宏，按下面两步打开（**信任范围只给评估表所在目录**，
不要选「所有位置」）：

1. 「文件 → 选项 → 信任中心 → 信任中心设置 → 宏设置」→
   勾选「**信任对 VBA 工程对象模型的访问**」（宏要读写表格对象模型）；
2. 「信任中心 → 信任中心设置 → 受信任位置 → 添加新位置」→
   把**评估表所在目录**加进去。

> 集团版 Excel 由信息科统一下发「受信任位置」策略时，这一步走策略，
> 不在用户侧做。

### 2.4 建三个按钮

在评估表的**空白区域**（不要压在数据区上）：

1. 「开发工具 → 插入 → 按钮」（**形状按钮，不是 ActiveX 控件**）；
2. 把按钮文字改成中文：

   | 按钮文字 | 指定的宏 |
   |----------|----------|
   | 开始评估 | `StartEvaluation` |
   | 确认提交 | `ConfirmSubmit` |
   | 导出结论表 | `ExportConclusion` |

3. 右键按钮 →「指定宏」→ 选上表对应的过程。

按钮本身**不存任何配置**，配置在环境变量与工作区配置文件里（见第 4 节）。

### 2.5 建工作区与配置文件

工作区默认是**评估表所在目录下的 `.nodule_eval`**，点一次「开始评估」会自动建出来。
里面会有：

```
<nodule_eval 表所在目录>/.nodule_eval/
├── nodule_eval.conf      ← 本机配置（首次运行后手工填）
├── req.json              ← 本轮请求（每次运行覆盖）
├── engine.log            ← 引擎的标准输出与错误输出
└── last_run.txt          ← 最近一轮的 run_id
```

---

## 3. WPS（JSA）安装

### 3.1 放置宏文件

把 `src/nodule_eval/sheets/wps_jsa/nodule_eval.js` 拷到院内 WPS 允许执行脚本的目录
（建议 `<评估表目录>/.nodule_eval/`）。

### 3.2 加载与调试

1. WPS 表格 →「开发工具 → 宏」（或 `Alt + F11`）→ 「编辑宏 / 加载宏」→ 选 `nodule_eval.js`；
2. 在调试器里把三个函数设为入口（`StartEvaluation` / `ConfirmSubmit` / `ExportConclusion`）；
3. 建按钮：WPS 的「插入 → 按钮」或直接用「形状」，再指定上面的宏。

### 3.3 WPS 的两个已知限制

| 限制 | 影响 | 绕法 |
|------|------|------|
| **JSA 读不到进程环境变量** | 宏这一侧的配置读不到 `NODULE_EVAL_*` | 改读工作区的 `nodule_eval.conf`（见第 4 节）；**凭据仍在环境变量里**，由 WPS 进程继承给引擎 |
| **WPS 沙箱保护** | 报「权限不足，无法执行 shell 命令」 | 「设置中心 → 沙箱保护」里关掉，或把 WPS 安装目录与评估表目录加入白名单 |

### 3.4 JSA 的文件读写

JSA 版用 `ADODB.Stream`（Windows 自带）读写 UTF-8 文件，用 `Shell` 起引擎：

- `ADODB.Stream` 写 UTF-8 时会带 BOM —— 引擎读请求 JSON 时**已兼容 BOM**
  （按 `utf-8-sig` 解码），不用手工处理；
- 极端情况下 `ADODB.Stream` 不可用（极少见），此时改用第 5 节的**无宏退路**。

---

## 4. 配置

### 4.1 宏这一侧：`nodule_eval.conf`

放在工作区（`<评估表目录>/.nodule_eval/nodule_eval.conf`），**一行一个 `键=值`**：

```ini
# 本机配置。**这里不放任何凭据**（凭据走环境变量）。
python=C:\Python312\python.exe
home=C:\ProgramData\nodule-ai-model-eval
runs_dir=C:\ProgramData\nodule-ai-model-eval\runs
model_id=demo-model
model_version=1.0.0
dataset_ref=2026Q3-胸片肺结节
size_tolerance_mm=2.0
concurrency=4
# run_id=            # 留空则用最近一轮
```

| 键 | 缺省 | 说明 |
|----|------|------|
| `python` | `python` | Python 解释器路径 |
| `home` | 空 | 项目根目录（含 `src/` 与 `models/`）；给了就以它为工作目录启动引擎 |
| `runs_dir` | `<工作区>/runs` | 产物根目录 |
| `model_id` | **必填** | 被评模型，对应 `models/<model_id>.yaml` |
| `model_version` | 模型目录里声明的版本 | 换版复评时写清楚 |
| `dataset_ref` | `评估表` | 数据集引用，写进结论，便于事后核对 |
| `size_tolerance_mm` | `2.0` | 尺寸容差（毫米） |
| `concurrency` | `4` | 并发数 |
| `run_id` | 空 | 直接指定要提交/导出的那一轮 |

**Excel 版还认环境变量**：`NODULE_EVAL_MODEL_ID` 等同名环境变量**优先于**配置文件。
两处都没有时，Excel 版会退到评估表第 2 行的「被评模型：」。

### 4.2 引擎这一侧：凭据环境变量

12 个变量，全部从环境变量读，缺一个就**指名报缺哪个**（不会回落本地替身）：

| 变量 | 用途 |
|------|------|
| `RIS_BASE_URL` / `RIS_API_TOKEN` | 院内 RIS 读检查清单 |
| `INFERENCE_BASE_URL` / `INFERENCE_API_TOKEN` | 院内肺结节 AI 推理服务 |
| `RESEARCH_PLATFORM_BASE_URL` / `RESEARCH_PLATFORM_TOKEN` | 科研平台评估登记（写回） |
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | 语义归并用的模型端点（OpenAI-compatible 协议） |
| `MODEL_TIMEOUT` / `MODEL_MAX_RETRIES` / `MODEL_TEMPERATURE` | 模型端点的超时、重试、温度 |

在 Excel/WPS 里跑宏时，**环境变量必须对 WPS/Excel 进程可见**：

- 系统级变量（`setx` / 系统属性 → 环境变量）—— 需**重启** Excel / WPS 才生效；
- 或者把它们写进 WPS/Excel 的启动脚本。

---

## 5. 没有宏怎么办（无宏退路）

宏装不上、宏被安全策略拦住、或者只想在服务器上跑批 —— **产品不依赖宏**：

### 5.1 CSV 直跑（最省事）

把评估表另存为 CSV，在命令行跑：

```bash
# 1) 先看这张表被读成了什么（不发任何出站调用）
python -m nodule_eval.sheets.read 评估表.csv

# 2) 跑一轮评估
python -m nodule_eval.cli run --cases 评估表.csv --model-id demo-model --runs-dir runs

# 3) 导出结论表与漏诊清单
python -m nodule_eval.cli export --run-id <run_id>
```

### 5.2 `fill` 回填（把结论写回 xlsm）

```bash
python -m nodule_eval.cli fill --sheet 评估表.xlsm --run-id <run_id>
```

它与宏走**同一份列契约**（`sheets/columns.py`）—— 只追加新增六列，
既有列与金标准原文逐单元格比对，确认零改动；落盘是**原子替换**，
中途失败原表一个字节都不变。

### 5.3 三个按钮的命令行等价物

| 按钮 | 等价命令 |
|------|----------|
| 开始评估 | `python -m nodule_eval.cli run --request <工作区>/req.json --runs-dir <runs_dir>` |
| 确认提交 | `python -m nodule_eval.cli submit --run-id <run_id> --runs-dir <runs_dir> --review` |
| 导出结论表 | `python -m nodule_eval.cli export --run-id <run_id> --runs-dir <runs_dir>` |

---

## 6. 出问题时先看哪里

| 现象 | 大概率原因 | 怎么办 |
|------|------------|--------|
| 点按钮弹出「没有指定被评模型」 | `model_id` 没配 | 配 `nodule_eval.conf` 的 `model_id`，或设 `NODULE_EVAL_MODEL_ID` |
| 状态栏一直停在「正在准备调用推理端点……」 | 引擎没起来（多半是凭据缺了） | 看工作区的 `engine.log`，第一行以 `[` 开头的就是原因（缺哪个变量会指名） |
| 状态栏显示「评估失败：错误码 …」 | 引擎报错了 | 状态栏里带**处置提示**，按提示处置后重跑本轮即可（已跑完的例不会重复调用推理端点） |
| 弹出「评估表缺少产品新增列」 | 用了科室自建的表，没有新增六列 | 用模板生成的评估表，或走 CSV 直跑 |
| 弹出「[CREDENTIAL_MISSING] …」 | 缺环境变量（会指名缺哪个） | 按第 4.2 节补环境变量，**重启** Excel/WPS |
| 引擎跑很久没反应 | 正在逐例调推理端点 | 看 `<工作区>/engine.log`；状态栏也有进度 |
| 提示「评估表第 N 行有内容却没有检查号」 | 明细区有半行数据 | 补检查号或删掉那一行 |
| 中文提示是乱码 | `.bas` 没转码 | 重跑 `convert_bas_to_ansi.py` 再导入 |
| WPS 报「权限不足，无法执行 shell 命令」 | WPS 沙箱保护 | 设置中心 → 沙箱保护 → 关闭 |

`engine.log` 里引擎的错误按 `[错误码] 文案` 打头 —— 照着错误码查
`src/nodule_eval/errors.py` 的错误码表，每条都带中文文案与是否可重试。

---

## 7. 源码在哪

| 文件 | 作用 |
|------|------|
| `src/nodule_eval/sheets/xlsm_vba/nodule_eval.bas` | Excel VBA 三个按钮 |
| `src/nodule_eval/sheets/xlsm_vba/nodule_eval_support.bas` | VBA 底层动作（UTF-8 / JSON / CSV / 起进程） |
| `src/nodule_eval/sheets/wps_jsa/nodule_eval.js` | WPS JSA 三个按钮（同一份契约） |
| `src/nodule_eval/sheets/macro_contract.py` | **交接协议的唯一真源**：文件名、命令行、字段名、回填列 |
| `src/nodule_eval/contracts.py` | 请求 / 响应 JSON 契约（引擎侧） |
| `tests/test_sheet_contract.py` | 三入口契约一致性的自动检查 |

宏与引擎之间只靠**落盘 JSON**交接，不开端口、不引第三方库。
