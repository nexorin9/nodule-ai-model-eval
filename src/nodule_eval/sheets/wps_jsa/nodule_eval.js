/*
 * ============================================================================
 * 肺结节AI模型评估系统 · 评估表内宏（WPS JSA 版）
 *
 * 三个按钮（与 Excel VBA 版**同一份契约**）：
 *   StartEvaluation    开始评估
 *   ConfirmSubmit      确认提交
 *   ExportConclusion   导出结论表
 *
 * 交接协议（与 src/nodule_eval/sheets/macro_contract.py 逐字对应）：
 *   <工作区>/req.json                请求 JSON —— 字段同 contracts.py
 *   runs/<run_id>/result.json        引擎写出的结果 JSON
 *   runs/<run_id>/case_rows.csv      逐例判定明细（回填数据源）
 *   runs/<run_id>/tool_calls.jsonl   调用底账（可反查入参与返回）
 *   <工作区>/engine.log              引擎的标准输出与错误输出
 *   <工作区>/last_run.txt            最近一轮的 run_id
 *   <工作区>/nodule_eval.conf        本机配置（python 路径 / 项目根 / 被评模型）
 *
 * 只用 WPS JSA 的公共能力与 Windows 自带组件：
 *   - 表格读写：Application / Workbook / Worksheet / Range / Cells
 *   - 起外部进程：Shell（JSA 原生函数）
 *   - 文件读写：ADODB.Stream（Windows 自带，写出 UTF-8）
 * 不使用任何 VBA 专有控件，不使用 ActiveX **控件**。
 *
 * 与 Excel VBA 版的**唯一差别**是配置来源：JSA 读不到进程环境变量，
 * 因此本机配置放在工作区的 nodule_eval.conf 里（键值对，一行一个）。
 * ============================================================================
 */

// ---- 交接协议常量（改这里等于改协议；改完请同步 macro_contract.py） ----
var REQ_FILENAME = "req.json";
var RESULT_FILENAME = "result.json";
var CASE_ROWS_FILENAME = "case_rows.csv";
var TOOL_CALLS_FILENAME = "tool_calls.jsonl";
var ENGINE_LOG_FILENAME = "engine.log";
var LAST_RUN_FILENAME = "last_run.txt";
var CONFIG_FILENAME = "nodule_eval.conf";
var CLI_MODULE = "nodule_eval.cli";

// ---- 运行状态文件（引擎写、宏轮询）——与 sheets/status.py 的 STATUS_FILENAME 同名 ----
var STATUS_FILENAME = "engine_status.json";
// ---- 状态文件里「渲染好的状态栏文本」这个键 ----
var STATUS_KEY_TEXT = "status_text";

// ---- 引擎跑完的完成标记（纯 ASCII，判定不依赖中文字面量） ----
var DONE_MARKER = "__ENGINE_DONE__";

// ---- 既有列（科室维护，宏只读，绝不写） ----
var COL_ACCESSION = "accession_no";
var COL_GOLD = "gold_standard_text";
var COL_IMAGE = "image_ref";

// ---- 产品新增六列（宏只写这六列） ----
var COL_VERDICT = "verdict";
var COL_FINDINGS = "model_findings";
var COL_FAILURE = "failure_reason";
var COL_TOOL_CALL = "tool_call_id";
var COL_NEEDS_REVIEW = "needs_review";
var COL_NORMALIZE = "normalize_source";

// ---- 汇总区固定地址（与 sheets/template.py 的 SUMMARY_CELLS 逐字一致） ----
var SUMMARY_CELL_ADDRESSES = {
    sample_size: "A6",
    tp: "B6",
    fp: "C6",
    fn: "D6",
    tn: "E6",
    sensitivity: "F6",
    specificity: "G6",
    ppv: "H6",
    npv: "I6",
    failed_count: "A8",
    rule_degraded_count: "B8",
    needs_review_count: "C8",
    incomparable_count: "D8",
    model_normalized_count: "E8",
    degrade_note: "F8"
};

// ---- 汇总区各键取自哪个字段 ----
var SUMMARY_SOURCE_FIELD = {
    sample_size: "total_cases",
    tp: "tp",
    fp: "fp",
    fn: "fn",
    tn: "tn",
    sensitivity: "sensitivity",
    specificity: "specificity",
    ppv: "ppv",
    npv: "npv",
    failed_count: "failed_count",
    rule_degraded_count: "rule_degraded_count",
    needs_review_count: "needs_review_count",
    incomparable_count: "incomparable_count",
    model_normalized_count: "model_normalized_count",
    degrade_note: "degrade_note"
};

// ---- 回填的列（**只有产品新增六列**） ----
var BACKFILL_COLUMNS = ["verdict", "model_findings", "failure_reason", "tool_call_id", "needs_review", "normalize_source"];

// ---- 版面常量（与 sheets/template.py 一致） ----
var META_ROW = 2;
var HEADER_SEARCH_ROWS = 30;
var HEADER_SEARCH_COLS = 30;
var RUN_TIMEOUT_MS = 3600 * 1000;
var SUMMARY_PLACEHOLDER = "—";
var NEEDS_REVIEW_YES = "是";
var NEEDS_REVIEW_NO = "否";
var SECTION_MARKER_FIRST_CHAR = 0x3010;

// ============================================================================
// 按钮 1：开始评估
// ============================================================================
function StartEvaluation() {
    var ws = null;
    try {
        ws = resolveDetailSheet();
        var cols = findColumns(ws);
        var cases = collectCases(ws, cols);
        if (cases.length === 0) {
            // **空态**：状态栏也写一句 —— 光弹一个对话框，表里看不出「这一轮没跑」。
            writeStatus(ws, "【运行状态】本表未圈定病例：明细区没有可评估的检查号");
            notifyUser("本表未圈定病例：在明细区填写「检查号」与「金标准原文」后再点「开始评估」。", "开始评估");
            return;
        }

        var modelId = configValue("model_id");
        if (!modelId) {
            throw new Error("没有指定被评模型：请在工作区的 " + CONFIG_FILENAME + " 里写 model_id=（对应 models/<model_id>.yaml）");
        }
        var modelVersion = configValue("model_version");
        var datasetRef = configValue("dataset_ref") || "评估表";

        var workspace = resolveWorkspace(ws);
        ensureDir(workspace);
        var runsDir = configValue("runs_dir") || (workspace + "\\runs");
        var reqPath = workspace + "\\" + REQ_FILENAME;
        var logPath = workspace + "\\" + ENGINE_LOG_FILENAME;
        var runId = "sheet-" + timestamp();

        writeUtf8File(reqPath, buildRequestJson(runId, modelId, modelVersion, datasetRef, cases));

        var cmd = buildEngineCommand(
            "run --request \"" + reqPath + "\" --runs-dir \"" + runsDir + "\""
        );
        writeStatus(ws, "【运行状态】评估中：共 " + cases.length + " 例，正在调用推理端点……");
        // **边跑边刷状态栏**（加载态）：每轮轮询读一次引擎写的状态文件，
        // 把「已处理 N/总数 · 当前例号 · 心跳」刷到表底部（表格不冻结）。
        runEngine(cmd, logPath, RUN_TIMEOUT_MS, runsDir + "\\" + runId + "\\" + STATUS_FILENAME, ws);

        var resultPath = runsDir + "\\" + runId + "\\" + RESULT_FILENAME;
        var resultText = readUtf8File(resultPath);
        writeSummaryCells(ws, resultText);
        backfillRows(ws, cols, runsDir + "\\" + runId + "\\" + CASE_ROWS_FILENAME);
        writeUtf8File(workspace + "\\" + LAST_RUN_FILENAME, runId);
        writeStatus(ws, "【运行状态】评估完成：run_id=" + runId + "（调用底账：" + TOOL_CALLS_FILENAME + "）");

        notifyUser("评估完成（run_id = " + runId + "）。\n表头汇总区与逐例明细已回填；漏诊清单见漏诊明细区。", "开始评估");
    } catch (e) {
        if (ws) { writeStatusSafe(ws, "【运行状态】评估失败：" + errorText(e)); }
        notifyUser("评估未完成：\n" + errorText(e), "开始评估");
    }
}

// ============================================================================
// 按钮 2：确认提交（人工确认后写回科研平台评估登记）
// ============================================================================
function ConfirmSubmit() {
    try {
        var ws = resolveDetailSheet();
        var workspace = resolveWorkspace(ws);
        var runsDir = configValue("runs_dir") || (workspace + "\\runs");
        var runId = configValue("run_id") || readUtf8File(workspace + "\\" + LAST_RUN_FILENAME).replace(/^\s+|\s+$/g, "");
        if (!runId) {
            throw new Error("找不到本轮的运行ID：请先点「开始评估」，或在 " + CONFIG_FILENAME + " 里写 run_id=");
        }
        if (!confirmUser("确认把本轮评估结论写回科研平台评估登记？\n\nrun_id = " + runId +
                         "\n写回后这一轮的状态变成「已提交」，不再改动。")) {
            return;
        }
        // --review：按钮本身即人工确认动作（人在表里逐行看过判定与漏诊区）
        var cmd = buildEngineCommand(
            "submit --run-id \"" + runId + "\" --runs-dir \"" + runsDir +
            "\" --submitted-by \"" + Application.UserName + "\" --review"
        );
        var logPath = workspace + "\\" + ENGINE_LOG_FILENAME;
        runEngine(cmd, logPath, 300 * 1000);
        notifyUser("已写回科研平台评估登记。\n" + tailLines(readUtf8File(logPath), 8), "确认提交");
    } catch (e) {
        notifyUser("写回未完成：\n" + errorText(e), "确认提交");
    }
}

// ============================================================================
// 按钮 3：导出结论表（结论工作簿 + 漏诊清单，不依赖宏也能做）
// ============================================================================
function ExportConclusion() {
    try {
        var ws = resolveDetailSheet();
        var workspace = resolveWorkspace(ws);
        var runsDir = configValue("runs_dir") || (workspace + "\\runs");
        var runId = configValue("run_id") || readUtf8File(workspace + "\\" + LAST_RUN_FILENAME).replace(/^\s+|\s+$/g, "");
        if (!runId) {
            throw new Error("找不到本轮的运行ID：请先点「开始评估」，或在 " + CONFIG_FILENAME + " 里写 run_id=");
        }
        var cmd = buildEngineCommand("export --run-id \"" + runId + "\" --runs-dir \"" + runsDir + "\"");
        var logPath = workspace + "\\" + ENGINE_LOG_FILENAME;
        runEngine(cmd, logPath, 300 * 1000);
        notifyUser("结论表已导出：\n" + tailLines(readUtf8File(logPath), 8), "导出结论表");
    } catch (e) {
        notifyUser("导出未完成：\n" + errorText(e), "导出结论表");
    }
}

// ============================================================================
// 表格定位
// ============================================================================
function resolveDetailSheet() {
    var wb = Application.ActiveWorkbook;
    var names = ["评估表", "逐例明细"];
    for (var i = 0; i < names.length; i++) {
        try {
            var sheet = wb.Worksheets.Item(names[i]);
            if (sheet) { return sheet; }
        } catch (e) { /* 没有这一页，继续找 */ }
    }
    return wb.Worksheets.Item(1);
}

function normalizeHeader(text) {
    return String(text || "").replace(/^[\s　]+|[\s　]+$/g, "").toLowerCase();
}

function findColumns(ws) {
    var cols = {
        headerRow: 0, accession: 0, gold: 0, image: 0,
        verdict: 0, findings: 0, failure: 0, toolCall: 0, needsReview: 0, normalize: 0
    };
    var bestRow = 0, bestCount = 0;
    for (var r = 1; r <= HEADER_SEARCH_ROWS; r++) {
        var found = 0;
        for (var c = 1; c <= HEADER_SEARCH_COLS; c++) {
            var header = normalizeHeader(cellValue(ws, r, c));
            switch (header) {
                case COL_ACCESSION: case "检查号": cols.accession = c; found++; break;
                case COL_GOLD: case "金标准原文": cols.gold = c; found++; break;
                case COL_IMAGE: case "影像引用": cols.image = c; found++; break;
                case COL_VERDICT: case "判定": cols.verdict = c; found++; break;
                case COL_FINDINGS: case "模型返回": cols.findings = c; found++; break;
                case COL_FAILURE: case "失败原因": cols.failure = c; found++; break;
                case COL_TOOL_CALL: case "调用记录id": cols.toolCall = c; found++; break;
                case COL_NEEDS_REVIEW: case "待人工复核": cols.needsReview = c; found++; break;
                case COL_NORMALIZE: case "归并来源": cols.normalize = c; found++; break;
                default: break;
            }
        }
        if (found > bestCount) { bestCount = found; bestRow = r; }
    }
    if (bestRow === 0) {
        throw new Error("评估表里找不到表头行（表头须含 accession_no / gold_standard_text / image_ref）");
    }
    cols.headerRow = bestRow;
    if (!cols.accession || !cols.gold) {
        throw new Error("评估表缺少既有列 accession_no 或 gold_standard_text（表头在第 " + bestRow + " 行）");
    }
    if (!cols.verdict || !cols.findings || !cols.failure ||
        !cols.toolCall || !cols.needsReview || !cols.normalize) {
        throw new Error("评估表缺少产品新增列（verdict / model_findings / failure_reason / " +
                        "tool_call_id / needs_review / normalize_source）；请使用模板生成的评估表");
    }
    return cols;
}

function cellValue(ws, row, col) {
    var v = ws.Cells(row, col).Value;
    if (v === undefined || v === null) { return ""; }
    return v;
}

function isSectionMarker(value) {
    var text = String(value === undefined || value === null ? "" : value).replace(/^[\s　]+|[\s　]+$/g, "");
    if (text === "") { return false; }
    if (text.charCodeAt(0) === SECTION_MARKER_FIRST_CHAR) { return true; }
    return text.indexOf("漏诊明细") >= 0 || text.indexOf("运行状态") >= 0;
}

function collectCases(ws, cols) {
    var result = [];
    var lastRow = ws.Cells(ws.Rows.Count, cols.accession).End(-4162).Row;  // xlUp
    for (var r = cols.headerRow + 1; r <= lastRow; r++) {
        if (isSectionMarker(cellValue(ws, r, 1))) { break; }
        var acc = String(cellValue(ws, r, cols.accession)).replace(/^[\s　]+|[\s　]+$/g, "");
        var gold = String(cellValue(ws, r, cols.gold));
        var img = String(cellValue(ws, r, cols.image)).replace(/^[\s　]+|[\s　]+$/g, "");

        if (acc === "" && gold.replace(/^[\s　]+|[\s　]+$/g, "") === "" && img === "") {
            continue;                                   // 模板预留的空白行
        }
        if (acc === "") {
            throw new Error("评估表第 " + r + " 行有内容却没有检查号，无法评估（已停止）");
        }
        if (gold.replace(/^[\s　]+|[\s　]+$/g, "") === "") {
            throw new Error("评估表第 " + r + " 行的金标准原文为空，无法评估（已停止）");
        }
        if (result.length > 0 && result[result.length - 1].accession_no === acc) {
            // 跨行叙述：同一检查号写在相邻两行 → 并成一例，段间用换行隔开
            result[result.length - 1].gold_standard_text += "\n" + gold;
        } else {
            result.push({
                accession_no: acc,
                gold_standard_text: gold,
                image_ref: img,
                row_no: r
            });
        }
    }
    return result;
}

// ============================================================================
// 请求 JSON（字段与 contracts.py 一致）
// ============================================================================
function jsonQuote(text) {
    var out = '"';
    var s = String(text === undefined || text === null ? "" : text);
    for (var i = 0; i < s.length; i++) {
        var ch = s.charAt(i);
        var code = s.charCodeAt(i);
        if (ch === '"') { out += '\\"'; }
        else if (ch === "\\") { out += "\\\\"; }
        else if (ch === "\n") { out += "\\n"; }
        else if (ch === "\r") { out += "\\r"; }
        else if (ch === "\t") { out += "\\t"; }
        else if (code < 32) { out += "\\u" + ("000" + code.toString(16)).slice(-4); }
        else { out += ch; }
    }
    return out + '"';
}

function buildRequestJson(runId, modelId, modelVersion, datasetRef, cases) {
    var parts = [];
    for (var i = 0; i < cases.length; i++) {
        parts.push("{" +
            jsonQuote("accession_no") + ":" + jsonQuote(cases[i].accession_no) + "," +
            jsonQuote("gold_standard_text") + ":" + jsonQuote(cases[i].gold_standard_text) + "," +
            jsonQuote("image_ref") + ":" + jsonQuote(cases[i].image_ref) + "," +
            jsonQuote("row_no") + ":" + cases[i].row_no +
            "}");
    }
    return "{" +
        jsonQuote("run_id") + ":" + jsonQuote(runId) + "," +
        jsonQuote("model_id") + ":" + jsonQuote(modelId) + "," +
        jsonQuote("model_version") + ":" + jsonQuote(modelVersion) + "," +
        jsonQuote("dataset_ref") + ":" + jsonQuote(datasetRef) + "," +
        jsonQuote("cases") + ":[" + parts.join(",") + "]," +
        jsonQuote("options") + ":" + buildOptionsJson() +
        "}";
}

function buildOptionsJson() {
    var tolerance = parseFloat(configValue("size_tolerance_mm") || "2");
    var concurrency = parseInt(configValue("concurrency") || "4", 10);
    if (isNaN(tolerance)) { tolerance = 2; }
    if (isNaN(concurrency) || concurrency < 1) { concurrency = 4; }
    return "{" +
        jsonQuote("size_tolerance_mm") + ":" + tolerance + "," +
        jsonQuote("concurrency") + ":" + concurrency + "," +
        jsonQuote("resume") + ":false" +
        "}";
}

// ============================================================================
// 回填（只写新增六列，既有列一个字都不动）
// ============================================================================
function backfillRows(ws, cols, caseRowsPath) {
    if (!fileExists(caseRowsPath)) {
        throw new Error("引擎没有写出逐例明细：" + caseRowsPath);
    }
    var lines = readUtf8File(caseRowsPath).split(/\r?\n/);
    var colIndexes = [cols.verdict, cols.findings, cols.failure, cols.toolCall, cols.needsReview, cols.normalize];
    // 第 0 行是表头；之后每行 = accession_no, verdict, model_findings,
    // failure_reason, tool_call_id, needs_review, normalize_source, row_no
    for (var i = 1; i < lines.length; i++) {
        if (lines[i].replace(/^[\s　]+|[\s　]+$/g, "") === "") { continue; }
        var fields = csvSplit(lines[i]);
        if (fields.length < 8) { continue; }
        var rowNo = parseInt(fields[7], 10);
        if (isNaN(rowNo) || rowNo <= 0 || rowNo <= cols.headerRow) { continue; }
        ws.Cells(rowNo, colIndexes[0]).Value = fields[1];
        ws.Cells(rowNo, colIndexes[1]).Value = fields[2];
        ws.Cells(rowNo, colIndexes[2]).Value = fields[3];
        ws.Cells(rowNo, colIndexes[3]).Value = fields[4];
        ws.Cells(rowNo, colIndexes[4]).Value = needsReviewText(fields[5]);
        ws.Cells(rowNo, colIndexes[5]).Value = fields[6];
    }
}

function needsReviewText(raw) {
    return String(raw).replace(/^[\s　]+|[\s　]+$/g, "").toLowerCase() === "true"
        ? NEEDS_REVIEW_YES : NEEDS_REVIEW_NO;
}

function csvSplit(line) {
    var fields = [];
    var cur = "";
    var inQuotes = false;
    for (var i = 0; i < line.length; i++) {
        var ch = line.charAt(i);
        if (inQuotes) {
            if (ch === '"') {
                if (line.charAt(i + 1) === '"') { cur += '"'; i++; }
                else { inQuotes = false; }
            } else { cur += ch; }
        } else if (ch === '"') {
            inQuotes = true;
        } else if (ch === ",") {
            fields.push(cur); cur = "";
        } else {
            cur += ch;
        }
    }
    fields.push(cur);
    return fields;
}

// ============================================================================
// 汇总区
// ============================================================================
function writeSummaryCells(ws, resultText) {
    var keys = ["sample_size", "tp", "fp", "fn", "tn", "sensitivity", "specificity",
                "ppv", "npv", "failed_count", "rule_degraded_count", "needs_review_count",
                "incomparable_count", "model_normalized_count", "degrade_note"];
    for (var i = 0; i < keys.length; i++) {
        var key = keys[i];
        var address = SUMMARY_CELL_ADDRESSES[key];
        if (!address) { continue; }
        var raw = jsonScalar(resultText, SUMMARY_SOURCE_FIELD[key]);
        if (key === "degrade_note") {
            ws.Range(address).Value = (!raw || raw === "null") ? SUMMARY_PLACEHOLDER : raw;
        } else if (!raw || raw === "null") {
            ws.Range(address).Value = SUMMARY_PLACEHOLDER;
        } else if (isRatioKey(key)) {
            ws.Range(address).Value = parseFloat(raw);
        } else {
            ws.Range(address).Value = parseInt(raw, 10);
        }
    }
}

function isRatioKey(key) {
    return key === "sensitivity" || key === "specificity" ||
           key === "ppv" || key === "npv";
}

// ---- 按键取 JSON 里的标量值 ----
function jsonScalar(text, key) {
    if (!text) { return ""; }
    var needle = '"' + key + '":';
    var p = text.indexOf(needle);
    if (p < 0) { return ""; }
    var i = p + needle.length;
    while (i < text.length && " \t\r\n".indexOf(text.charAt(i)) >= 0) { i++; }
    if (i >= text.length) { return ""; }
    var out = "";
    if (text.charAt(i) === '"') {
        i++;
        while (i < text.length) {
            var ch = text.charAt(i);
            if (ch === "\\") {
                var esc = text.charAt(i + 1);
                if (esc === "n") { out += "\n"; }
                else if (esc === "r") { out += "\r"; }
                else if (esc === "t") { out += "\t"; }
                else { out += esc; }
                i += 2;
            } else if (ch === '"') {
                break;
            } else {
                out += ch; i++;
            }
        }
    } else {
        while (i < text.length && ",}]\r\n".indexOf(text.charAt(i)) < 0) {
            out += text.charAt(i); i++;
        }
        out = out.replace(/^[\s　]+|[\s　]+$/g, "");
    }
    return out;
}

// ============================================================================
// 状态栏 / 工作区 / 命令行
// ============================================================================
function writeStatus(ws, text) {
    var lastRow = ws.Cells(ws.Rows.Count, 1).End(-4162).Row;   // xlUp
    for (var r = lastRow; r >= 1; r--) {
        if (isSectionMarker(cellValue(ws, r, 1))) {
            ws.Cells(r, 1).Value = text;
            return;
        }
    }
}

function writeStatusSafe(ws, text) {
    try { writeStatus(ws, text); } catch (e) { /* 状态栏写不进去也不该挡住主流程 */ }
}

function resolveWorkspace(ws) {
    var folder = "";
    try { folder = ws.Parent.Path; } catch (e) { folder = ""; }
    if (!folder) {
        try { folder = Application.ActiveWorkbook.Path; } catch (e2) { folder = ""; }
    }
    if (!folder) { folder = "."; }
    return folder + "\\.nodule_eval";
}

// ---- 本机配置：工作区的 nodule_eval.conf（键值对，一行一个） ----
function configValue(key) {
    var ws = null;
    try { ws = resolveDetailSheet(); } catch (e) { ws = null; }
    var workspace = ws ? resolveWorkspace(ws) : ".\\.nodule_eval";
    var path = workspace + "\\" + CONFIG_FILENAME;
    var text = "";
    try { text = readUtf8File(path); } catch (e) { text = ""; }
    var lines = text.split(/\r?\n/);
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].replace(/^[\s　]+|[\s　]+$/g, "");
        if (line === "" || line.charAt(0) === "#") { continue; }
        var p = line.indexOf("=");
        if (p < 0) { continue; }
        if (line.substring(0, p).replace(/^[\s　]+|[\s　]+$/g, "") === key) {
            return line.substring(p + 1).replace(/^[\s　]+|[\s　]+$/g, "");
        }
    }
    return "";
}

function buildEngineCommand(args) {
    var pythonExe = configValue("python") || "python";
    var home = configValue("home") || "";
    var cmd = "";
    if (home) {
        cmd = "cd /d \"" + home + "\" && set \"PYTHONPATH=" + home + "\\src\" && ";
    }
    return cmd + "\"" + pythonExe + "\" -m " + CLI_MODULE + " " + args;
}

// ---- 起外部进程并等它跑完（**边等边刷状态栏**） ----
// statusPath / ws 给了就每轮轮询读一次状态文件刷新状态栏（加载态）；
// 不给就退化成「只等结果」的旧行为。
function runEngine(cmd, logPath, timeoutMs, statusPath, ws) {
    var full = cmd + " > \"" + logPath + "\" 2>&1 && echo " + DONE_MARKER + " >> \"" + logPath + "\"";
    try { deleteFile(logPath); } catch (e) { /* 文件本来就不在 */ }
    runShell(full);
    var start = nowMs();
    while (nowMs() - start < timeoutMs) {
        if (statusPath && ws) { refreshProgress(ws, statusPath); }
        if (fileExists(logPath)) {
            var text = readUtf8File(logPath);
            var err = firstErrorLine(text);
            if (err) { throw new Error(err + "（引擎日志：" + logPath + "）"); }
            if (text.indexOf(DONE_MARKER) >= 0) { return; }
        }
        pause(300);
    }
    throw new Error("引擎超过 " + Math.round(timeoutMs / 1000) + " 秒仍未结束（详见 " + logPath + "）");
}

// ---- 读状态文件，把状态栏刷成引擎给的那句话 ----
// 文案不在宏里拼：引擎把渲染好的 status_text 一起写进文件，两边不各写一份。
function refreshProgress(ws, statusPath) {
    if (!fileExists(statusPath)) { return; }
    var text = readUtf8File(statusPath);
    if (!text) { return; }
    var line = jsonScalar(text, STATUS_KEY_TEXT);
    if (!line) { return; }
    writeStatus(ws, line);
}

function firstErrorLine(text) {
    var lines = text.split(/\r?\n/);
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].replace(/^[\s　]+|[\s　]+$/g, "");
        if (line !== "" && line.charAt(0) === "[") { return line; }
    }
    return "";
}

function tailLines(text, count) {
    var lines = text.split(/\r?\n/);
    var out = [];
    for (var i = Math.max(0, lines.length - count); i < lines.length; i++) {
        var line = lines[i].replace(/^[\s　]+|[\s　]+$/g, "");
        if (line !== "") { out.push(line); }
    }
    return out.join("\n");
}

function runShell(cmd) {
    // JSA 的 Shell 第二个参数是窗口显示样式（1 = 正常显示）
    try { Shell(cmd, 1); }
    catch (e) { Shell(cmd); }
}

// JSA 没有 sleep / Wait：不暂停会把 CPU 跑满
function pause(ms) {
    var end = nowMs() + ms;
    while (nowMs() < end) { /* 空转等待 */ }
}

function nowMs() {
    return new Date().getTime();
}

function timestamp() {
    var d = new Date();
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    return d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate()) + "-" +
           pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds());
}

// ============================================================================
// 文件读写（Windows 自带 ADODB.Stream；写 UTF-8）
// ============================================================================
function writeUtf8File(path, text) {
    var stream = new ActiveXObject("ADODB.Stream");
    stream.Type = 2;            // adTypeText
    stream.Charset = "utf-8";
    stream.Open();
    stream.WriteText(text);
    stream.SaveToFile(path, 2); // adSaveCreateOverWrite
    stream.Close();
}

function readUtf8File(path) {
    if (!fileExists(path)) { return ""; }
    var stream = new ActiveXObject("ADODB.Stream");
    stream.Type = 1;            // adTypeBinary
    stream.Charset = "utf-8";
    stream.Open();
    stream.LoadFromFile(path);
    var text = stream.ReadText();
    stream.Close();
    return text.replace(/^﻿/, "");
}

function fileExists(path) {
    var stream = new ActiveXObject("ADODB.Stream");
    try {
        stream.Type = 1;
        stream.Open();
        stream.LoadFromFile(path);
        stream.Close();
        return true;
    } catch (e) {
        try { stream.Close(); } catch (e2) { /* 本来就没打开 */ }
        return false;
    }
}

function deleteFile(path) {
    try {
        var stream = new ActiveXObject("ADODB.Stream");
        stream.Type = 1;
        stream.Open();
        stream.LoadFromFile(path);
        stream.Close();
    } catch (e) { /* 文件不在，删不了也无妨 */ }
    runShell('del /q "' + path + '"');
}

function ensureDir(path) {
    runShell('if not exist "' + path + '" md "' + path + '"');
}

// ============================================================================
// 提示
// ============================================================================
function notifyUser(text, title) {
    var message = title ? (title + "\n" + text) : text;
    try {
        if (typeof alert === "function") { alert(message); return; }
    } catch (e) { /* 换下一种提示方式 */ }
    try {
        if (typeof Application !== "undefined" && Application.MsgBox) { Application.MsgBox(message); return; }
    } catch (e2) { /* 换下一种提示方式 */ }
    console.log(message);
}

function confirmUser(message) {
    try {
        if (typeof confirm === "function") { return confirm(message); }
    } catch (e) { /* 换下一种确认方式 */ }
    try {
        if (typeof prompt === "function") {
            return String(prompt(message + "\n（输入「确认」继续）")).replace(/^\s+|\s+$/g, "") === "确认";
        }
    } catch (e2) { /* 换下一种确认方式 */ }
    return true;
}

function errorText(e) {
    if (e === undefined || e === null) { return "未知错误"; }
    if (typeof e === "string") { return e; }
    if (e.message) { return String(e.message); }
    return String(e);
}
