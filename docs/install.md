# 院内安装说明（Windows PC）

> **这份说明的用途**：在一台院内 Windows PC 上装好 Python 运行时、配好院内凭据、
> 跑通一次 `doctor` 自检，就能用评估表跑肺结节 AI 模型的检出评估。
>
> 全程约 20 分钟；装完请一定跑一遍 `doctor` —— 它会**指名**告诉你缺哪一项，
> 不用自己猜。

---

## 目录

- [装在哪台机器上](#装在哪台机器上)
- [安装前检查](#安装前检查)
- [步骤 1：装 Python 3.11](#步骤-1装-python-311)
- [步骤 2：放项目目录](#步骤-2放项目目录)
- [步骤 3：装依赖](#步骤-3装依赖)
- [步骤 4：配凭据（.env）](#步骤-4配凭据env)
- [步骤 5：跑 doctor 验收](#步骤-5跑-doctor-验收)
- [步骤 6：宏信任目录](#步骤-6宏信任目录)
- [步骤 7：存储目录与磁盘](#步骤-7存储目录与磁盘)
- [离线安装（内网机器无外网）](#离线安装内网机器无外网)
- [常见报错处置](#常见报错处置)
- [卸载与回滚](#卸载与回滚)
- [相关文档](#相关文档)

---

## 装在哪台机器上

本产品**不部署任何院内系统**，它是一套「表格内插件 + 本机引擎」：
装在**一台**院内 Windows PC 上，评估表和引擎都在这台机器上，状态也落在这台机器。

| 组件 | 装在哪 | 说明 |
|------|--------|------|
| 引擎（本机进程） | 院内一台常开的 Windows PC | Python 3.11 + 项目目录；每轮评估起一个进程，跑完退出 |
| 表格内插件 | 同一台机器上的 Excel 或 WPS | xlsm 加载宏 / WPS JSA 宏；放射科点「开始评估」「确认提交」 |
| 运行状态 | 本机磁盘 | SQLite 运行库，默认 `%LOCALAPPDATA%\nodule-ai-model-eval\` |
| 评估产物 | 本机磁盘 | 默认项目目录下 `runs/<run_id>/`（逐例明细、调用底账、漏诊清单） |
| 院内端点 | 院内网络 | RIS（读）、厂商肺结节 AI 推理服务（写）、科研平台（写）、模型服务（写）—— **由厂商/信息科提供，本产品只对接** |

**不装服务、不开端口、不进容器**：整套东西就是一个 Python 目录 + 一个 Excel 表。
院内防火墙不需要为本产品单独开口子。

---

## 安装前检查

| 检查项 | 要求 | 怎么查 |
|--------|------|--------|
| 操作系统 | Windows 10 / 11，或 Windows Server 2019+ | `winver` |
| Python | 3.11（3.10 也能跑，doctor 会提示） | `py -3.11 --version` |
| 表格软件 | Excel 或 WPS（要跑宏的机器上装） | 打开一张表确认能编辑 |
| 磁盘空间 | ≥ 2 GB（运行库 + 产物；公开数据集缓存另算约 400 MB） | 资源管理器 |
| 院内凭据 | RIS / 推理服务 / 科研平台 / 模型服务各一套 token | 向信息科与厂商索取 |
| 网络 | 本机能访问上述四个端点 | 装完用 `doctor` 与一次真实调用验证 |

> `doctor` **不会**替你去 ping 端点 —— 自检打真实端点会消耗厂商额度，
> 也会把对端日志搞脏。端点通不通，第一次真跑评估时就知道了。

---

## 步骤 1：装 Python 3.11

1. 到 Python 官网下载 **Windows installer (64-bit)** 3.11.x；
2. 安装时**务必勾选 `Add python.exe to PATH`**；
3. 装在 `C:\Python311` 或 `D:\Python311` 这类**短路径**上，别塞到
   `C:\Program Files\...` 深处 —— 引擎要往项目目录写产物，
   装在受保护目录里普通用户写不进去；
4. 装完**新开一个**命令行窗口（PATH 改动对已开的窗口不生效），验证：

```bat
py -3.11 --version
```

看到 `Python 3.11.x` 即可。

---

## 步骤 2：放项目目录

把整个项目目录拷到院内机器，例如 `D:\nodule-ai-model-eval\`。

```
D:\nodule-ai-model-eval\        ← 项目根
├── src\nodule_eval\             ← 引擎源码（Python 包）
├── models\                      ← 被评模型目录（厂商字段映射、授权开关）
├── prompts\                     ← 语义归并提示词（带版本号）
├── scripts\                     ← 取数、模板生成、校验脚本
├── docs\                        ← 本说明、命令行速查、宏安装说明
├── fixtures\                    ← 样例数据
├── requirements.txt
├── .env.example                 ← 凭据样例（复制成 .env 再填）
└── runs\                        ← 产物落在这里（可删，会自动重建）
```

不需要编译，也不需要联网。`data/`、`runs/`、`.pytest_cache/` 这些运行时目录
拷过去后可以直接删掉。

---

## 步骤 3：装依赖

### 3.1 直接装到系统 Python（最省事，宏最好配）

```bat
cd /d D:\nodule-ai-model-eval
py -3.11 -m pip install -r requirements.txt
```

### 3.2 装进虚拟环境（隔离性好，宏里要写绝对路径）

```bat
cd /d D:\nodule-ai-model-eval
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> **虚拟环境的代价**：`.venv` 里写死了绝对路径，**不能整目录拷到别的机器用**；
> 而且宏起进程时必须用 `.venv\Scripts\python.exe` 的**绝对路径**，
> 不能写 `python`。宏源码里怎么填这个路径，见 `docs/macro_install.md`。
> 院内只有这一台机器、且由信息科统一维护时，直接装系统 Python 更省事。

依赖就四个（`requests` / `PyYAML` / `openpyxl` / `python-dotenv`），
`pytest` 是自检用的，院内可以不装。

---

## 步骤 4：配凭据（.env）

凭据**只从环境变量读**。院内最常见的落法是项目根下的 `.env` 文件：

```bat
cd /d D:\nodule-ai-model-eval
copy .env.example .env
notepad .env
```

`.env` 的位置**按项目目录算，不按当前工作目录算** —— 宏是从 Excel 里起进程的，
工作目录往往是表格所在目录，所以把 `.env` 放在项目根下最省事。
也可以用 `NODULE_EVAL_ENV_FILE` 环境变量指向别的路径。

| 变量 | 填什么 | 从哪来 | 能空吗 |
|------|--------|--------|--------|
| `RIS_BASE_URL` | 院内 RIS 地址，如 `https://ris.hospital.internal` | 信息科 | 不能 |
| `RIS_API_TOKEN` | RIS 的 Bearer token | 信息科 | 不能 |
| `INFERENCE_BASE_URL` | 厂商肺结节 AI 推理服务地址 | 厂商 | 不能 |
| `INFERENCE_API_TOKEN` | 推理服务 token | 厂商 | 不能 |
| `RESEARCH_PLATFORM_BASE_URL` | 科研平台地址 | 信息科 | 不能 |
| `RESEARCH_PLATFORM_TOKEN` | 科研平台 token | 信息科 | 不能 |
| `OPENAI_BASE_URL` | 模型服务地址（OpenAI-compatible），如 `https://llm.hospital.internal/v1` | 信息科 | 不能 |
| `OPENAI_API_KEY` | 模型服务 token | 信息科 | 不能 |
| `OPENAI_MODEL` | 模型名，如 `nodule-goldstd-merge` | 信息科 | 不能 |
| `MODEL_TIMEOUT` | 单次调用超时秒数（如 `20`） | 科室定 | 不能 |
| `MODEL_MAX_RETRIES` | 失败重试次数（如 `2`） | 科室定 | 不能 |
| `MODEL_TEMPERATURE` | 采样温度（归并任务建议 `0`） | 科室定 | 不能 |
| `RESEARCH_PLATFORM_MAX_PAYLOAD_BYTES` | 写回提交体上限（字节，默认 1 MiB） | 科研平台侧给 | 能（可选） |

**生效顺序**：系统环境变量 **优先于** `.env` —— `.env` 只补差，不覆盖已有的。
这样「机器级兜底 + 单机临时改」的做法能正常工作。

```bat
:: 临时改一个变量（只对当前窗口有效）
set MODEL_TIMEOUT=30

:: 永久改（新开的窗口才生效）
setx MODEL_TIMEOUT 30
```

> **改完 `.env` 要重开 Excel**：Excel 是从启动那一刻继承环境变量的，
> 已经开着的 Excel 不会看到新值。

> **`.env` 不进版本库**：`.gitignore` 已排除它；`.env.example` 是无真实值的样例。

---

## 步骤 5：跑 doctor 验收

```bat
cd /d D:\nodule-ai-model-eval
set PYTHONPATH=D:\nodule-ai-model-eval\src
py -3.11 -m nodule_eval.cli doctor
```

期望输出（装好时的样子）：

```
[✓] Python 版本：当前 3.11.9（要求 ≥ 3.10）
[✓] 依赖包：requests 2.32.3、yaml 6.0.2、openpyxl 3.1.5
[✓] 环境变量文件（.env）：D:\nodule-ai-model-eval\.env（已生效；系统环境变量优先）
[✓] 环境变量：12 项齐备（RIS / 推理 / 科研平台 / 模型四组凭据）
[✓] 出站目标：四个出站面全部指向环境变量里的真实端点：ris=https://…；inference=https://…；…（doctor 不发起任何出站调用）
[✓] 被评模型目录：已授权 2 个：demo-model、second-model
[✓] 运行库：C:\Users\<用户>\AppData\Local\nodule-ai-model-eval\nodule_eval.sqlite3（已新建，写入探针通过且已回滚，无残迹）
[✓] 产物目录：runs（所在目录 D:\nodule-ai-model-eval 可写）
[!] 公开数据集缓存（JSRT）：未下载（不影响跑评估；仅用于规则/模型归并对照实验）：data\jsrt。下载：python scripts/fetch_jsrt.py
[✓] 院内安装说明：D:\nodule-ai-model-eval\docs\install.md（提示项，不影响继续使用）

自检通过：配置齐备，可以开始评估。
另有 1 项提示（公开数据集缓存（JSRT））—— 不阻断，按需处理。
```

**退出码约定**：`0` = 装好了；`2` = 有阻断项，按上面指名的条目处理。

| 标记 | 含义 | 影响退出码 |
|------|------|-----------|
| `[✓]` | 通过 | — |
| `[✗]` | 阻断项不通过 | **是**（退出码 2） |
| `[!]` | 提示项不通过 | 否 |

`doctor` **不发任何出站调用**，也不打开任何本地替身 —— 它只读配置、
开一次运行库做写入探针（写完回滚，不留残迹）、数一下公开数据集缓存在不在。

给院内脚本读就用 JSON：

```bat
py -3.11 -m nodule_eval.cli doctor --json
```

---

## 步骤 6：宏信任目录

表格内插件（xlsm 加载宏 / WPS JSA 宏）需要把「宏所在的目录」交给 Office 信任，
否则点按钮没反应。**宏源码与三个按钮的逐步操作见 `docs/macro_install.md`**，
这里只给信任目录这一步。

### 6.1 Excel

1. `文件 → 选项 → 信任中心 → 信任中心设置`；
2. `受信任位置 → 添加新位置`，把放宏的目录（例如
   `D:\nodule-ai-model-eval\macro\`）加进去；
3. `受信任位置 → 受信任发布者`（可选）：把院内签发的宏证书加进去，
   这样宏**不用每次点「启用内容」**；
4. `宏设置` 保持「禁用所有宏，并发出通知」即可 —— 打开表时点一下
   「启用内容」，只对这份表生效；
5. `开发工具 → 宏 → Visual Basic → 文件 → 导入文件`，导入 `.bas` 源码，
   再把三个按钮（`开始评估` / `确认提交` / `导出结论表`）挂到工作表上。

### 6.2 WPS

1. `选项 → 自定义功能区 → 勾选「开发工具」`；
2. `开发工具 → JSA 宏`，按提示把 `.js` 放进 JSA 宏目录
   （在 JSA 编辑器里能直接看到实际路径；常见位置在
   `%AppData%\kingsoft\office6\jsaddons\` 下）；
3. WPS 的宏安全策略与 Excel 独立，需要**单独**放行。

### 6.3 组策略禁止宏时（最常见的「点按钮没反应」）

信息科统一禁用宏时，产品仍可用，只是入口换成命令行：

```bat
cd /d D:\nodule-ai-model-eval
set PYTHONPATH=D:\nodule-ai-model-eval\src
py -3.11 -m nodule_eval.cli run --cases D:\评估\胸片评估表.json --model-id demo-model
```

或者走 CSV 直跑 / `fill` 回填。详见 `docs/cli.md`。

> **宏不装也能用**：宏只是入口，引擎本身是命令行程序。
> 评估结论、判定明细、调用底账一个都不少。

---

## 步骤 7：存储目录与磁盘

| 目录 | 装什么 | 默认位置 | 怎么改 |
|------|--------|----------|--------|
| 运行库 | SQLite（run 表、逐例判定行、调用索引、回执台账） | `%LOCALAPPDATA%\nodule-ai-model-eval\` | `--store D:\nodule-ai-model-eval\store.sqlite3` |
| 产物 | 每轮的逐例明细、汇总、漏诊清单、调用底账、状态迁移记录 | `项目目录\runs\<run_id>\` | `--runs-dir D:\nodule-runs\` |
| 公开数据集 | JSRT 247 例（只用于归并对照实验） | `项目目录\data\jsrt\` | `--dataset-dir` |

**磁盘估算**：一轮 200 例的产物约 1–3 MB（含逐例产物与调用底账）。
跑之前看一眼剩余空间就行。

**保留策略**：产物按 `run_id` 一轮一份目录，**不会互相覆盖** ——
同一轮重跑会另起 `version-0002/`。建议定期把结论表与漏诊清单导出归档
（`export` 子命令），产物目录可以按保留期清理。

---

## 离线安装（内网机器无外网）

院内机器通常不通外网。两种做法：

### 做法 A：在有网机器上把依赖打包带进去（推荐）

在一台**装有同版本 Python 3.11** 的机器上：

```bat
cd /d D:\nodule-ai-model-eval
py -3.11 -m pip download -r requirements.txt -d wheels
```

把 `wheels\` 目录拷到院内机器，然后：

```bat
cd /d D:\nodule-ai-model-eval
py -3.11 -m pip install --no-index --find-links=wheels -r requirements.txt
```

> `pip download` 拉的是**当前平台、当前 Python 版本**的包。
> 在 Windows 上下载、在 Linux 上装是不行的 —— 下载机器要与安装机器
> 同平台同版本。

### 做法 B：整目录拷贝

项目本身没有需要编译的东西，**整个目录拷过去就能跑**；只有第三方依赖需要单独装
（做法 A）。**不要拷 `.venv\`** —— 虚拟环境里写死了绝对路径，拷过去必然不能用。

---

## 常见报错处置

| 现象 / 报错 | 原因 | 处置 |
|------------|------|------|
| `[CREDENTIAL_MISSING] 缺 N 项：RIS_API_TOKEN、OPENAI_API_KEY…` | `.env` 没配、位置不对，或 Excel 是改之前打开的 | 确认 `.env` 在项目根；`doctor` 的「环境变量文件（.env）」一项会告诉你它读没读到；**改完重开 Excel** |
| 明明配了变量还报缺 | 装了 `.env` 但没装 `python-dotenv` | `py -3.11 -m pip install python-dotenv`；或改用系统环境变量 |
| `ModuleNotFoundError: No module named 'nodule_eval'` | `PYTHONPATH` 没设 | `setx PYTHONPATH "D:\nodule-ai-model-eval\src"`（新开窗口），或进 `src\` 目录里跑 |
| `[CONTRACT_INVALID] 存储库打不开：…` | 运行库目录不存在或不可写 | 建好目录，或 `--store` 指到可写位置（如 `D:\nodule-ai-model-eval\store.sqlite3`） |
| doctor 报「目录不可写」 | 装在 `C:\Program Files` 之类受保护位置 | 项目挪到 `D:\`；给运行账号写权限 |
| 宏点了没反应 | 宏被禁用 / 信任位置没设 / 宏里 python 路径不对 | 「步骤 6」逐项核；把宏里的解释器路径改成**绝对路径**（含虚拟环境时用 `.venv\Scripts\python.exe`） |
| `[INFERENCE_5XX] 无法连接…` | 端点地址错 / 端口不通 / 防火墙 | 先在同机 `curl` 试一次厂商地址；找厂商确认端口 |
| `[WRITEBACK_NOT_REVIEWED] …` | 还没点「确认提交」就写回 | 产品要求**人工复核后才出站**；先在表里复核逐例判定，再点「确认提交」 |
| `[RESEARCH_PAYLOAD_TOO_LARGE] 提交体 N 字节 > 对端上限 …` | 科研平台的请求体上限更小 | 按提示分批；或设 `RESEARCH_PLATFORM_MAX_PAYLOAD_BYTES` 覆盖默认上限 |
| `[STUDY_UID_MISMATCH] 推理服务未回传检查标识…` | 推理端点与 RIS 的标识对不上 | 找厂商核标识口径；该例已判 failed 且不进指标分母，不会污染结论 |
| 评估表打不开 / 报格式错 | 模板被 Excel 改过（多了列、少了宏） | 用 `py -3.11 scripts\build_xlsm.py` 重新生成模板 |
| 一轮跑得很慢 | 并发数或网络 | `--concurrency 8`；推理端点有并发上限时按厂商建议调 |

---

## 卸载与回滚

1. 先把结论导出去（导出的表格与漏诊清单是给人带走的，产物目录可以删）：

   ```bat
   py -3.11 -m nodule_eval.cli export --run-id <run_id> --out-dir D:\结论归档
   ```

2. 删项目目录 `D:\nodule-ai-model-eval\`；
3. 删运行库 `%LOCALAPPDATA%\nodule-ai-model-eval\`（**这里存着历史评估结论**，
   删之前确认已经导出）；
4. Office 那边把受信任位置与导入的宏撤掉。

---

## 相关文档

| 文档 | 什么时候看 |
|------|-----------|
| `README.md` | 产品是什么、怎么用、结论表长什么样 |
| `docs/cli.md` | 命令行子命令族（后台跑批、无宏退路、排障） |
| `docs/macro_install.md` | 宏源码导入、三个按钮、WPS JSA 配置 |
| `docs/normalizer-comparison.md` | 规则基线与模型归并的对照结果 |
| `.env.example` | 凭据样例（复制成 `.env` 再填） |
