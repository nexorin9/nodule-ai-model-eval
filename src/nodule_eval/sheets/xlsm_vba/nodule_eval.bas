Attribute VB_Name = "NoduleEval"
Option Explicit

'==========================================================================
' 肺结节AI模型评估系统 · 评估表内宏（Excel VBA 版）
'
' 三个按钮（按钮用「开发工具 → 插入 → 按钮」创建，按钮文字改成中文，
' 再在「分配宏」里选下面的过程即可；本模块不使用 ActiveX 控件、不使用 UserForm）：
'
'   StartEvaluation    开始评估
'   ConfirmSubmit      确认提交
'   ExportConclusion   导出结论表
'
' 交接协议（与 src/nodule_eval/sheets/macro_contract.py 逐字对应）：
'   <工作区>/req.json                请求 JSON —— 字段同 contracts.py
'   runs/<run_id>/result.json        引擎写出的结果 JSON
'   runs/<run_id>/case_rows.csv      逐例判定明细（回填数据源）
'   runs/<run_id>/tool_calls.jsonl   调用底账（可反查入参与返回）
'   <工作区>/engine.log              引擎的标准输出与错误输出
'   <工作区>/last_run.txt            最近一轮的 run_id
'
' 三个按钮只做三件事：把表里的行变成请求、把引擎的结果变成表里的判定、
' 把人工确认变成一次写回。**既有三列一个字都不改**。
'
' 编码说明：本文件以 UTF-8 保存（便于在代码仓库里看 diff）。VBA 编辑器按
' 系统 ANSI 代码页读 .bas，导入前请先转码：
'   python scripts/convert_bas_to_ansi.py src/nodule_eval/sheets/xlsm_vba
' 转码后中文提示与中文表头别名才认得出（ASCII 字段名不受影响）。
'==========================================================================

' ---- 交接协议常量（改这里等于改协议；改完请同步 macro_contract.py） ----
Private Const REQ_FILENAME As String = "req.json"
Private Const RESULT_FILENAME As String = "result.json"
Private Const CASE_ROWS_FILENAME As String = "case_rows.csv"
Private Const TOOL_CALLS_FILENAME As String = "tool_calls.jsonl"
Private Const ENGINE_LOG_FILENAME As String = "engine.log"
Private Const LAST_RUN_FILENAME As String = "last_run.txt"
' ---- 运行状态文件（引擎写、宏轮询）——与 sheets/status.py 的 STATUS_FILENAME 同名 ----
Private Const STATUS_FILENAME As String = "engine_status.json"
' ---- 状态文件里「渲染好的状态栏文本」这个键 ----
Private Const STATUS_KEY_TEXT As String = "status_text"
Private Const CONFIG_FILENAME As String = "nodule_eval.conf"
Private Const CLI_MODULE As String = "nodule_eval.cli"

' ---- 环境变量 ----
Private Const ENV_WORKSPACE As String = "NODULE_EVAL_WORKSPACE"
Private Const ENV_HOME As String = "NODULE_EVAL_HOME"
Private Const ENV_PYTHON As String = "NODULE_EVAL_PYTHON"
Private Const ENV_RUNS_DIR As String = "NODULE_EVAL_RUNS_DIR"
Private Const ENV_MODEL_ID As String = "NODULE_EVAL_MODEL_ID"
Private Const ENV_MODEL_VERSION As String = "NODULE_EVAL_MODEL_VERSION"
Private Const ENV_DATASET_REF As String = "NODULE_EVAL_DATASET_REF"
Private Const ENV_RUN_ID As String = "NODULE_EVAL_RUN_ID"
Private Const ENV_SIZE_TOLERANCE As String = "NODULE_EVAL_SIZE_TOLERANCE_MM"
Private Const ENV_CONCURRENCY As String = "NODULE_EVAL_CONCURRENCY"

' ---- 既有列（科室维护，宏只读，绝不写） ----
Private Const COL_ACCESSION As String = "accession_no"
Private Const COL_GOLD As String = "gold_standard_text"
Private Const COL_IMAGE As String = "image_ref"

' ---- 产品新增六列（宏只写这六列） ----
Private Const COL_VERDICT As String = "verdict"
Private Const COL_FINDINGS As String = "model_findings"
Private Const COL_FAILURE As String = "failure_reason"
Private Const COL_TOOL_CALL As String = "tool_call_id"
Private Const COL_NEEDS_REVIEW As String = "needs_review"
Private Const COL_NORMALIZE As String = "normalize_source"

' ---- 汇总区固定地址（与 sheets/template.py 的 SUMMARY_CELLS 逐字一致） ----
Private Const SUM_SAMPLE As String = "A6"
Private Const SUM_TP As String = "B6"
Private Const SUM_FP As String = "C6"
Private Const SUM_FN As String = "D6"
Private Const SUM_TN As String = "E6"
Private Const SUM_SENSITIVITY As String = "F6"
Private Const SUM_SPECIFICITY As String = "G6"
Private Const SUM_PPV As String = "H6"
Private Const SUM_NPV As String = "I6"
Private Const SUM_FAILED As String = "A8"
Private Const SUM_DEGRADED As String = "B8"
Private Const SUM_HELD As String = "C8"
Private Const SUM_INCOMPARABLE As String = "D8"
Private Const SUM_MODEL_NORMALIZED As String = "E8"
Private Const SUM_DEGRADE_NOTE As String = "F8"

' ---- 版面常量（与 sheets/template.py 一致） ----
Private Const META_ROW As Long = 2
Private Const HEADER_SEARCH_ROWS As Long = 30
Private Const HEADER_SEARCH_COLS As Long = 30
Private Const RUN_TIMEOUT_SEC As Long = 3600

' ---- 汇总区取不到值时的占位符 ----
Private Const SUMMARY_PLACEHOLDER As String = "—"

' ---- needs_review 列的写法 ----
Private Const NEEDS_REVIEW_YES As String = "是"
Private Const NEEDS_REVIEW_NO As String = "否"

' ---- 表头与数据行之间的列定位结果 ----
Public Type SheetColumns
    HeaderRow As Long
    Accession As Long
    Gold As Long
    Image As Long
    Verdict As Long
    Findings As Long
    Failure As Long
    ToolCall As Long
    NeedsReview As Long
    Normalize As Long
End Type

'==========================================================================
' 按钮 1：开始评估
'==========================================================================
Public Sub StartEvaluation()
    Dim ws As Worksheet
    Dim cols As SheetColumns
    Dim cases As Collection
    Dim runId As String, modelId As String, modelVersion As String, datasetRef As String
    Dim workspace As String, runsDir As String, reqPath As String, resultPath As String
    Dim logPath As String, cmd As String, message As String
    Dim statusPath As String
    Dim pythonExe As String, homeDir As String
    Dim resultText As String
    Dim q As String

    On Error GoTo Failed
    q = Chr$(34)
    Application.ScreenUpdating = False

    Set ws = ResolveDetailSheet(ActiveWorkbook)
    cols = FindColumns(ws)
    Set cases = CollectCases(ws, cols)
    If cases.Count = 0 Then
        ' **空态**：状态栏也写一句 —— 光弹一个对话框，表里看不出「这一轮没跑」。
        WriteStatus ws, "【运行状态】本表未圈定病例：明细区没有可评估的检查号"
        MsgBox "本表未圈定病例：在明细区填写「检查号」与「金标准原文」后再点「开始评估」。", _
               vbInformation, "开始评估"
        GoTo CleanExit
    End If

    workspace = ResolveWorkspace(ws)
    EnsureDir workspace
    runsDir = ConfigValue(ENV_RUNS_DIR, workspace)
    If Len(runsDir) = 0 Then runsDir = workspace & "\runs"

    modelId = ConfigValue(ENV_MODEL_ID, workspace)
    modelVersion = ConfigValue(ENV_MODEL_VERSION, workspace)
    If Len(modelId) = 0 Then
        ReadMetaModel ws, modelId, modelVersion
    End If
    If Len(modelId) = 0 Then
        Err.Raise vbObjectError + 2001, , _
            "没有指定被评模型：请设置环境变量 " & ENV_MODEL_ID & "，或在工作区的 " & _
            CONFIG_FILENAME & " 里写 model_id=（对应 models/<model_id>.yaml）"
    End If
    datasetRef = ConfigValue(ENV_DATASET_REF, workspace)
    If Len(datasetRef) = 0 Then datasetRef = "评估表"

    reqPath = workspace & "\" & REQ_FILENAME
    logPath = workspace & "\" & ENGINE_LOG_FILENAME
    runId = "sheet-" & Format$(Now, "yyyymmdd-hhnnss")
    resultPath = runsDir & "\" & runId & "\" & RESULT_FILENAME
    statusPath = runsDir & "\" & runId & "\" & STATUS_FILENAME

    WriteUtf8File reqPath, BuildRequestJson(runId, modelId, modelVersion, datasetRef, cases, workspace)

    pythonExe = ConfigValue(ENV_PYTHON, workspace)
    If Len(pythonExe) = 0 Then pythonExe = "python"
    homeDir = ConfigValue(ENV_HOME, workspace)
    cmd = BuildEngineCommand(pythonExe, homeDir, _
        "run --request " & q & reqPath & q & " --runs-dir " & q & runsDir & q)

    WriteStatus ws, "【运行状态】评估中：共 " & cases.Count & " 例，正在调用推理端点……"
    ' **边跑边刷状态栏**（加载态）：宏每半秒读一次引擎写的状态文件，
    ' 把「已处理 N/总数 · 当前例号 · 心跳」刷到表底部。表格不冻结
    '（等待循环里有 DoEvents），人随时能切走、取消、截图。
    If Not RunEngineWithProgress(cmd, logPath, statusPath, ws, RUN_TIMEOUT_SEC, message) Then
        Err.Raise vbObjectError + 2002, , message
    End If

    resultText = ReadUtf8File(resultPath)
    WriteSummaryCells ws, resultText
    BackfillRows ws, cols, runsDir & "\" & runId & "\" & CASE_ROWS_FILENAME
    WriteLastRun workspace, runId
    WriteStatus ws, "【运行状态】评估完成：run_id=" & runId & _
        "（调用底账：" & TOOL_CALLS_FILENAME & "）"

    MsgBox "评估完成（run_id = " & runId & "）。" & vbCrLf & _
           "表头汇总区与逐例明细已回填；漏诊清单见漏诊明细区。", _
           vbInformation, "开始评估"
    GoTo CleanExit

Failed:
    WriteStatusSafe ws, "【运行状态】评估失败：" & Err.Description
    MsgBox "评估未完成：" & vbCrLf & Err.Description, vbExclamation, "开始评估"
CleanExit:
    Application.ScreenUpdating = True
End Sub

'==========================================================================
' 按钮 2：确认提交（人工确认后写回科研平台评估登记）
'==========================================================================
Public Sub ConfirmSubmit()
    Dim ws As Worksheet
    Dim workspace As String, runsDir As String, runId As String
    Dim cmd As String, logPath As String, message As String
    Dim pythonExe As String, homeDir As String
    Dim q As String

    On Error GoTo Failed
    q = Chr$(34)

    Set ws = ResolveDetailSheet(ActiveWorkbook)
    workspace = ResolveWorkspace(ws)
    runsDir = ConfigValue(ENV_RUNS_DIR, workspace)
    If Len(runsDir) = 0 Then runsDir = workspace & "\runs"
    runId = ResolveRunId(ws, workspace)
    If Len(runId) = 0 Then
        Err.Raise vbObjectError + 2010, , _
            "找不到本轮的运行ID：请先点「开始评估」，或设置环境变量 " & ENV_RUN_ID
    End If

    If MsgBox("确认把本轮评估结论写回科研平台评估登记？" & vbCrLf & vbCrLf & _
              "run_id = " & runId & vbCrLf & _
              "写回后这一轮的状态变成「已提交」，不再改动。", _
              vbYesNo + vbQuestion, "确认提交") <> vbYes Then
        Exit Sub
    End If

    logPath = workspace & "\" & ENGINE_LOG_FILENAME
    pythonExe = ConfigValue(ENV_PYTHON, workspace)
    If Len(pythonExe) = 0 Then pythonExe = "python"
    homeDir = ConfigValue(ENV_HOME, workspace)
    ' --review：按钮本身即人工确认动作（人在表里逐行看过判定与漏诊区），
    '           引擎据此把这一轮推进到「已人工复核」再写回；不带就写不出去。
    cmd = BuildEngineCommand(pythonExe, homeDir, _
        "submit --run-id " & q & runId & q & " --runs-dir " & q & runsDir & q & _
        " --submitted-by " & q & Application.UserName & q & " --review")

    If Not RunEngineAndWait(cmd, logPath, 300, message) Then
        Err.Raise vbObjectError + 2011, , message
    End If

    MsgBox "已写回科研平台评估登记。" & vbCrLf & _
           TailLines(ReadUtf8File(logPath), 8), vbInformation, "确认提交"
    Exit Sub

Failed:
    MsgBox "写回未完成：" & vbCrLf & Err.Description, vbExclamation, "确认提交"
End Sub

'==========================================================================
' 按钮 3：导出结论表（结论工作簿 + 漏诊清单，不依赖宏也能做）
'==========================================================================
Public Sub ExportConclusion()
    Dim ws As Worksheet
    Dim workspace As String, runsDir As String, runId As String
    Dim cmd As String, logPath As String, message As String
    Dim pythonExe As String, homeDir As String
    Dim q As String

    On Error GoTo Failed
    q = Chr$(34)

    Set ws = ResolveDetailSheet(ActiveWorkbook)
    workspace = ResolveWorkspace(ws)
    runsDir = ConfigValue(ENV_RUNS_DIR, workspace)
    If Len(runsDir) = 0 Then runsDir = workspace & "\runs"
    runId = ResolveRunId(ws, workspace)
    If Len(runId) = 0 Then
        Err.Raise vbObjectError + 2020, , _
            "找不到本轮的运行ID：请先点「开始评估」，或设置环境变量 " & ENV_RUN_ID
    End If

    logPath = workspace & "\" & ENGINE_LOG_FILENAME
    pythonExe = ConfigValue(ENV_PYTHON, workspace)
    If Len(pythonExe) = 0 Then pythonExe = "python"
    homeDir = ConfigValue(ENV_HOME, workspace)
    cmd = BuildEngineCommand(pythonExe, homeDir, _
        "export --run-id " & q & runId & q & " --runs-dir " & q & runsDir & q)

    If Not RunEngineAndWait(cmd, logPath, 300, message) Then
        Err.Raise vbObjectError + 2021, , message
    End If

    MsgBox "结论表已导出：" & vbCrLf & TailLines(ReadUtf8File(logPath), 8), _
           vbInformation, "导出结论表"
    Exit Sub

Failed:
    MsgBox "导出未完成：" & vbCrLf & Err.Description, vbExclamation, "导出结论表"
End Sub

'==========================================================================
' 表定位
'==========================================================================

' ---- 找明细页：「评估表」或「逐例明细」，都没有就取第一页 ----
Private Function ResolveDetailSheet(ByVal wb As Workbook) As Worksheet
    Dim names As Variant, i As Long
    names = Array("评估表", "逐例明细")
    For i = LBound(names) To UBound(names)
        On Error Resume Next
        Set ResolveDetailSheet = wb.Worksheets(CStr(names(i)))
        On Error GoTo 0
        If Not ResolveDetailSheet Is Nothing Then Exit Function
    Next i
    Set ResolveDetailSheet = wb.Worksheets(1)
End Function

' ---- 按表头名定位列（既有三列 + 新增六列） ----
Private Function FindColumns(ByVal ws As Worksheet) As SheetColumns
    Dim cols As SheetColumns
    Dim r As Long, c As Long
    Dim headerText As String
    Dim found As Long, bestRow As Long, bestCount As Long

    cols.Accession = 0
    cols.Gold = 0
    cols.Image = 0
    cols.Verdict = 0
    cols.Findings = 0
    cols.Failure = 0
    cols.ToolCall = 0
    cols.NeedsReview = 0
    cols.Normalize = 0

    For r = 1 To HEADER_SEARCH_ROWS
        found = 0
        For c = 1 To HEADER_SEARCH_COLS
            headerText = NormalizeHeader(CStr(ws.Cells(r, c).Value & ""))
            Select Case headerText
                Case COL_ACCESSION, "检查号": cols.Accession = c: found = found + 1
                Case COL_GOLD, "金标准原文": cols.Gold = c: found = found + 1
                Case COL_IMAGE, "影像引用": cols.Image = c: found = found + 1
                Case COL_VERDICT, "判定": cols.Verdict = c: found = found + 1
                Case COL_FINDINGS, "模型返回": cols.Findings = c: found = found + 1
                Case COL_FAILURE, "失败原因": cols.Failure = c: found = found + 1
                Case COL_TOOL_CALL, "调用记录id": cols.ToolCall = c: found = found + 1
                Case COL_NEEDS_REVIEW, "待人工复核": cols.NeedsReview = c: found = found + 1
                Case COL_NORMALIZE, "归并来源": cols.Normalize = c: found = found + 1
            End Select
        Next c
        If found > bestCount Then
            bestCount = found
            bestRow = r
        End If
    Next r

    If bestRow = 0 Then
        Err.Raise vbObjectError + 2003, , _
            "评估表里找不到表头行（表头须含 accession_no / gold_standard_text / image_ref）"
    End If
    cols.HeaderRow = bestRow

    If cols.Accession = 0 Or cols.Gold = 0 Then
        Err.Raise vbObjectError + 2004, , _
            "评估表缺少既有列 accession_no 或 gold_standard_text（表头在第 " & bestRow & " 行）"
    End If
    If cols.Verdict = 0 Or cols.Findings = 0 Or cols.Failure = 0 Or _
       cols.ToolCall = 0 Or cols.NeedsReview = 0 Or cols.Normalize = 0 Then
        Err.Raise vbObjectError + 2005, , _
            "评估表缺少产品新增列（verdict / model_findings / failure_reason / " & _
            "tool_call_id / needs_review / normalize_source）；请使用模板生成的评估表"
    End If

    FindColumns = cols
End Function

' ---- 表头归一：去空白、去全角空格、转小写 ----
Private Function NormalizeHeader(ByVal text As String) As String
    Dim out As String
    out = LCase$(Trim$(text))
    out = Replace(out, "　", "")
    NormalizeHeader = out
End Function

' ---- 收集病例（逐行读；跨行叙述并作一例） ----
Private Function CollectCases(ByVal ws As Worksheet, ByVal cols As SheetColumns) As Collection
    Dim result As New Collection
    Dim r As Long, lastRow As Long
    Dim acc As String, gold As String, img As String
    Dim prev As Variant

    lastRow = ws.Cells(ws.Rows.Count, cols.Accession).End(xlUp).Row
    For r = cols.HeaderRow + 1 To lastRow
        ' 明细区到此为止：下面的区块标记（漏诊明细 / 运行状态）不是输入
        If IsSectionMarker(ws.Cells(r, 1).Value) Then Exit For

        acc = Trim$(CStr(ws.Cells(r, cols.Accession).Value & ""))
        gold = CStr(ws.Cells(r, cols.Gold).Value & "")
        img = Trim$(CStr(ws.Cells(r, cols.Image).Value & ""))

        If Len(acc) = 0 And Len(Trim$(gold)) = 0 And Len(img) = 0 Then
            ' 模板预留的空白行：跳过但不计数（引擎那边会记账）
        ElseIf Len(acc) = 0 Then
            Err.Raise vbObjectError + 2006, , _
                "评估表第 " & r & " 行有内容却没有检查号，无法评估（已停止）"
        ElseIf Len(Trim$(gold)) = 0 Then
            Err.Raise vbObjectError + 2007, , _
                "评估表第 " & r & " 行的金标准原文为空，无法评估（已停止）"
        Else
            If result.Count > 0 Then
                prev = result(result.Count)
                If CStr(prev(0)) = acc Then
                    ' 跨行叙述：同一检查号写在相邻两行 → 并成一例，段间用换行隔开
                    result.Remove result.Count
                    result.Add Array(acc, CStr(prev(1)) & vbLf & gold, img, prev(3))
                Else
                    result.Add Array(acc, gold, img, r)
                End If
            Else
                result.Add Array(acc, gold, img, r)
            End If
        End If
    Next r

    Set CollectCases = result
End Function

' ---- 这一格是不是区块标记（漏诊明细 / 运行状态） ----
' 用首字符的码位判断（U+3010），不依赖中文字面量 ——
' 宏文件若被以错误编码导入，字面量会乱码，而「这一行不是病例」不能失效。
Private Function IsSectionMarker(ByVal value As Variant) As Boolean
    Dim text As String
    text = Trim$(CStr(value & ""))
    If Len(text) = 0 Then Exit Function
    If AscW(Left$(text, 1)) = &H3010 Then
        IsSectionMarker = True
        Exit Function
    End If
    IsSectionMarker = (InStr(1, text, "漏诊明细", vbBinaryCompare) > 0) Or _
                      (InStr(1, text, "运行状态", vbBinaryCompare) > 0)
End Function

'==========================================================================
' 请求 JSON（字段与 contracts.py 一致）
'==========================================================================
Private Function BuildRequestJson(ByVal runId As String, ByVal modelId As String, _
                                   ByVal modelVersion As String, ByVal datasetRef As String, _
                                   ByVal cases As Collection, ByVal workspace As String) As String
    Dim parts() As String
    Dim i As Long
    Dim out As String

    ReDim parts(0 To cases.Count - 1)
    For i = 1 To cases.Count
        parts(i - 1) = BuildCaseJson(cases(i))
    Next i

    out = "{" & _
        JsonQuote("run_id") & ":" & JsonQuote(runId) & "," & _
        JsonQuote("model_id") & ":" & JsonQuote(modelId) & "," & _
        JsonQuote("model_version") & ":" & JsonQuote(modelVersion) & "," & _
        JsonQuote("dataset_ref") & ":" & JsonQuote(datasetRef) & "," & _
        JsonQuote("cases") & ":[" & Join(parts, ",") & "]," & _
        JsonQuote("options") & ":" & BuildOptionsJson(workspace) & _
        "}"

    BuildRequestJson = out
End Function

Private Function BuildCaseJson(ByVal item As Variant) As String
    BuildCaseJson = "{" & _
        JsonQuote("accession_no") & ":" & JsonQuote(CStr(item(0))) & "," & _
        JsonQuote("gold_standard_text") & ":" & JsonQuote(CStr(item(1))) & "," & _
        JsonQuote("image_ref") & ":" & JsonQuote(CStr(item(2))) & "," & _
        JsonQuote("row_no") & ":" & CStr(CLng(item(3))) & _
        "}"
End Function

Private Function BuildOptionsJson(ByVal workspace As String) As String
    Dim tolerance As Double, concurrency As Long
    tolerance = EnvNumber(ConfigValue(ENV_SIZE_TOLERANCE, workspace), 2#)
    concurrency = EnvNumber(ConfigValue(ENV_CONCURRENCY, workspace), 4#)
    BuildOptionsJson = "{" & _
        JsonQuote("size_tolerance_mm") & ":" & Trim$(Str$(tolerance)) & "," & _
        JsonQuote("concurrency") & ":" & CStr(CLng(concurrency)) & "," & _
        JsonQuote("resume") & ":false" & _
        "}"
End Function

' ---- 配置里的数字（读不出来就用缺省值） ----
Private Function EnvNumber(ByVal raw As String, ByVal fallback As Double) As Double
    Dim v As String
    v = Trim$(raw)
    If Len(v) = 0 Then
        EnvNumber = fallback
    ElseIf IsNumeric(v) Then
        EnvNumber = CDbl(Val(v))
    Else
        EnvNumber = fallback
    End If
End Function

'==========================================================================
' 回填（只写新增六列，既有列一个字都不动）
'==========================================================================
Private Sub BackfillRows(ByVal ws As Worksheet, ByVal cols As SheetColumns, ByVal caseRowsPath As String)
    Dim text As String, lines() As String
    Dim i As Long, fields As Variant
    Dim rowNo As Long
    Dim mapping As Variant, colIndexes As Variant

    If Not FileExistsSafe(caseRowsPath) Then
        Err.Raise vbObjectError + 2008, , "引擎没有写出逐例明细：" & caseRowsPath
    End If

    mapping = BackfillColumns()
    colIndexes = ColumnIndexArray(cols)

    text = ReadUtf8File(caseRowsPath)
    lines = Split(text, vbLf)

    ' 第 0 行是表头；之后每行 = accession_no, verdict, model_findings,
    ' failure_reason, tool_call_id, needs_review, normalize_source, row_no
    For i = 1 To UBound(lines)
        If Len(Trim$(lines(i))) = 0 Then GoTo NextLine
        fields = CsvSplit(Trim$(Replace(lines(i), vbCr, "")))
        If UBound(fields) < 7 Then GoTo NextLine
        rowNo = CLng(Val(CStr(fields(7))))
        If rowNo <= 0 Then GoTo NextLine
        If rowNo < cols.HeaderRow + 1 Then GoTo NextLine

        ws.Cells(rowNo, colIndexes(0)).Value = CStr(fields(1))
        ws.Cells(rowNo, colIndexes(1)).Value = CStr(fields(2))
        ws.Cells(rowNo, colIndexes(2)).Value = CStr(fields(3))
        ws.Cells(rowNo, colIndexes(3)).Value = CStr(fields(4))
        ws.Cells(rowNo, colIndexes(4)).Value = NeedsReviewText(CStr(fields(5)))
        ws.Cells(rowNo, colIndexes(5)).Value = CStr(fields(6))
NextLine:
    Next i
End Sub

' ---- 回填的列（**只有产品新增六列**） ----
Private Function BackfillColumns() As Variant
    BackfillColumns = Array("verdict", "model_findings", "failure_reason", "tool_call_id", "needs_review", "normalize_source")
End Function

' ---- 上面六列在表里的列号（顺序与 BackfillColumns 一致） ----
Private Function ColumnIndexArray(ByVal cols As SheetColumns) As Variant
    ColumnIndexArray = Array(cols.Verdict, cols.Findings, cols.Failure, cols.ToolCall, cols.NeedsReview, cols.Normalize)
End Function

' ---- needs_review：布尔 → 是 / 否 ----
Private Function NeedsReviewText(ByVal raw As String) As String
    If LCase$(Trim$(raw)) = "true" Then
        NeedsReviewText = NEEDS_REVIEW_YES
    Else
        NeedsReviewText = NEEDS_REVIEW_NO
    End If
End Function

'==========================================================================
' 汇总区（固定地址，与模板同一份）
'==========================================================================
Private Sub WriteSummaryCells(ByVal ws As Worksheet, ByVal resultText As String)
    Dim keys As Variant, i As Long
    Dim key As String, address As String, raw As String
    Dim cell As Range

    keys = Array("sample_size", "tp", "fp", "fn", "tn", "sensitivity", "specificity", "ppv", "npv", _
                 "failed_count", "rule_degraded_count", "needs_review_count", _
                 "incomparable_count", "model_normalized_count", "degrade_note")

    For i = LBound(keys) To UBound(keys)
        key = keys(i)
        address = SummaryCellAddress(key)
        If Len(address) = 0 Then GoTo NextKey
        raw = JsonScalar(resultText, SummarySourceField(key))
        Set cell = ws.Range(address)
        If key = "degrade_note" Then
            If Len(raw) = 0 Or raw = "null" Then
                cell.Value = SUMMARY_PLACEHOLDER
            Else
                cell.Value = raw
            End If
        ElseIf Len(raw) = 0 Or raw = "null" Then
            cell.Value = SUMMARY_PLACEHOLDER
        ElseIf IsRatioKey(key) Then
            cell.Value = Val(raw)
        Else
            cell.Value = CLng(Val(raw))
        End If
NextKey:
    Next i
End Sub

' ---- 汇总区各键的单元格地址 ----
Private Function SummaryCellAddress(ByVal key As String) As String
    Select Case key
        Case "sample_size": SummaryCellAddress = SUM_SAMPLE
        Case "tp": SummaryCellAddress = SUM_TP
        Case "fp": SummaryCellAddress = SUM_FP
        Case "fn": SummaryCellAddress = SUM_FN
        Case "tn": SummaryCellAddress = SUM_TN
        Case "sensitivity": SummaryCellAddress = SUM_SENSITIVITY
        Case "specificity": SummaryCellAddress = SUM_SPECIFICITY
        Case "ppv": SummaryCellAddress = SUM_PPV
        Case "npv": SummaryCellAddress = SUM_NPV
        Case "failed_count": SummaryCellAddress = SUM_FAILED
        Case "rule_degraded_count": SummaryCellAddress = SUM_DEGRADED
        Case "needs_review_count": SummaryCellAddress = SUM_HELD
        Case "incomparable_count": SummaryCellAddress = SUM_INCOMPARABLE
        Case "model_normalized_count": SummaryCellAddress = SUM_MODEL_NORMALIZED
        Case "degrade_note": SummaryCellAddress = SUM_DEGRADE_NOTE
        Case Else: SummaryCellAddress = ""
    End Select
End Function

' ---- 汇总区各键取自哪个字段 ----
Private Function SummarySourceField(ByVal key As String) As String
    Select Case key
        Case "sample_size": SummarySourceField = "total_cases"
        Case "tp": SummarySourceField = "tp"
        Case "fp": SummarySourceField = "fp"
        Case "fn": SummarySourceField = "fn"
        Case "tn": SummarySourceField = "tn"
        Case "sensitivity": SummarySourceField = "sensitivity"
        Case "specificity": SummarySourceField = "specificity"
        Case "ppv": SummarySourceField = "ppv"
        Case "npv": SummarySourceField = "npv"
        Case "failed_count": SummarySourceField = "failed_count"
        Case "rule_degraded_count": SummarySourceField = "rule_degraded_count"
        Case "needs_review_count": SummarySourceField = "needs_review_count"
        Case "incomparable_count": SummarySourceField = "incomparable_count"
        Case "model_normalized_count": SummarySourceField = "model_normalized_count"
        Case "degrade_note": SummarySourceField = "degrade_note"
        Case Else: SummarySourceField = ""
    End Select
End Function

' ---- 哪些是比率类指标（写浮点数） ----
Private Function IsRatioKey(ByVal key As String) As Boolean
    Select Case key
        Case "sensitivity", "specificity", "ppv", "npv": IsRatioKey = True
        Case Else: IsRatioKey = False
    End Select
End Function

'==========================================================================
' 运行进度（加载态）：轮询引擎的状态文件刷新状态栏
'==========================================================================

' ---- 起引擎并轮询状态文件（**表格不冻结**） ----
' 每轮循环做三件事：
'   1. DoEvents —— 让 Excel 保持响应（人随时能切走、取消、截图）；
'   2. 读一次状态文件，把状态栏刷成引擎给的那句话（文案在引擎那边，不在宏里）；
'   3. 看日志有没有出错、有没有跑完 —— 与 RunEngineAndWait 同一套判据。
Private Function RunEngineWithProgress(ByVal cmd As String, ByVal logPath As String, _
                                       ByVal statusPath As String, ByVal ws As Worksheet, _
                                       ByVal timeoutSec As Long, ByRef message As String) As Boolean
    Dim started As Double
    Dim logText As String
    Dim errLine As String
    Dim startErr As String

    startErr = StartEngineProcess(cmd, logPath)
    If Len(startErr) > 0 Then
        message = startErr
        RunEngineWithProgress = False
        Exit Function
    End If

    started = Timer
    Do
        DoEvents
        RefreshProgress ws, statusPath
        If FileExistsSafe(logPath) Then
            logText = ReadUtf8File(logPath)
            errLine = FirstErrorLine(logText)
            If Len(errLine) > 0 Then
                message = errLine & vbCrLf & "（引擎日志：" & logPath & "）"
                RunEngineWithProgress = False
                Exit Function
            End If
            If InStr(1, logText, DONE_MARKER, vbBinaryCompare) > 0 Then
                message = ""
                RunEngineWithProgress = True
                Exit Function
            End If
        End If
        If (Timer - started) > timeoutSec Then
            message = "引擎超过 " & timeoutSec & " 秒仍未结束，已中止等待（详见 " & logPath & "）"
            RunEngineWithProgress = False
            Exit Function
        End If
        Wait 1
    Loop
End Function

' ---- 读状态文件，把状态栏刷新成引擎给的那句话 ----
' 状态文件由引擎原子写（临时文件 + 替换），读到的永远是完整的一份。
' 文案不在宏里拼 —— 引擎把渲染好的 status_text 一起写进文件，
' 两边各写一份中文说法，早晚漂。
Private Sub RefreshProgress(ByVal ws As Worksheet, ByVal statusPath As String)
    Dim text As String, line As String
    If Not FileExistsSafe(statusPath) Then Exit Sub
    text = ReadUtf8File(statusPath)
    If Len(text) = 0 Then Exit Sub
    line = JsonScalar(text, STATUS_KEY_TEXT)
    If Len(line) = 0 Then Exit Sub
    WriteStatus ws, line
End Sub

'==========================================================================
' 状态栏 / 工作区 / 命令行
'==========================================================================
Private Sub WriteStatus(ByVal ws As Worksheet, ByVal text As String)
    Dim r As Long, lastRow As Long
    lastRow = ws.Cells(ws.Rows.Count, 1).End(xlUp).Row
    For r = lastRow To 1 Step -1
        If IsSectionMarker(ws.Cells(r, 1).Value) Then
            ws.Cells(r, 1).Value = text
            Exit Sub
        End If
    Next r
End Sub

Private Sub WriteStatusSafe(ByVal ws As Worksheet, ByVal text As String)
    On Error Resume Next
    If Not ws Is Nothing Then WriteStatus ws, text
    On Error GoTo 0
End Sub

' ---- 工作区：环境变量优先；否则用评估表所在目录下的 .nodule_eval ----
Private Function ResolveWorkspace(ByVal ws As Worksheet) As String
    Dim v As String, folder As String
    v = EnvValue(ENV_WORKSPACE, "")
    If Len(v) > 0 Then
        ResolveWorkspace = v
        Exit Function
    End If
    folder = ""
    On Error Resume Next
    folder = ws.Parent.Path
    On Error GoTo 0
    If Len(folder) = 0 Then folder = Environ$("TEMP")
    ResolveWorkspace = folder & "\.nodule_eval"
End Function

' ---- 引擎命令行 ----
Private Function BuildEngineCommand(ByVal pythonExe As String, ByVal homeDir As String, _
                                    ByVal args As String) As String
    Dim cmd As String, q As String
    q = Chr$(34)
    cmd = ""
    If Len(homeDir) > 0 Then
        cmd = "cd /d " & q & homeDir & q & " && set " & q & "PYTHONPATH=" & homeDir & "\src" & q & " && "
    End If
    cmd = cmd & q & pythonExe & q & " -m " & CLI_MODULE & " " & args
    BuildEngineCommand = cmd
End Function

' ---- 记住最近一轮的 run_id（「确认提交」按钮要用） ----
Private Sub WriteLastRun(ByVal workspace As String, ByVal runId As String)
    WriteUtf8File workspace & "\" & LAST_RUN_FILENAME, runId
End Sub

' ---- 本机配置：环境变量优先，其次工作区的 nodule_eval.conf ----
' 配置文件里**不放凭据**（RIS / 推理 / 科研平台 / 模型的凭据一律走环境变量）。
Private Function ConfigValue(ByVal envName As String, ByVal workspace As String) As String
    Dim v As String, key As String
    v = EnvValue(envName, "")
    If Len(v) > 0 Then
        ConfigValue = v
        Exit Function
    End If
    key = LCase$(Replace(envName, "NODULE_EVAL_", ""))
    ConfigValue = ConfValue(workspace, key)
End Function

' ---- 读配置文件里的一项（键值对，一行一个；# 开头是注释） ----
Private Function ConfValue(ByVal workspace As String, ByVal key As String) As String
    Dim path As String, text As String
    Dim lines() As String
    Dim i As Long, p As Long
    Dim line As String

    path = workspace & "\" & CONFIG_FILENAME
    If Not FileExistsSafe(path) Then Exit Function
    text = ReadUtf8File(path)
    lines = Split(text, vbLf)
    For i = LBound(lines) To UBound(lines)
        line = Trim$(Replace(lines(i), vbCr, ""))
        If Len(line) > 0 And Left$(line, 1) <> "#" Then
            p = InStr(1, line, "=", vbBinaryCompare)
            If p > 0 Then
                If Trim$(Left$(line, p - 1)) = key Then
                    ConfValue = Trim$(Mid$(line, p + 1))
                    Exit Function
                End If
            End If
        End If
    Next i
    ConfValue = ""
End Function

' ---- 取本轮 run_id：环境变量 / 配置 → 工作区的 last_run.txt → 表头行 ----
Private Function ResolveRunId(ByVal ws As Worksheet, ByVal workspace As String) As String
    Dim v As String
    v = ConfigValue(ENV_RUN_ID, workspace)
    If Len(v) > 0 Then
        ResolveRunId = v
        Exit Function
    End If
    v = Trim$(ReadUtf8File(workspace & "\" & LAST_RUN_FILENAME))
    If Len(v) > 0 Then
        ResolveRunId = v
        Exit Function
    End If
    ResolveRunId = MetaValue(ws, "运行ID")
End Function

' ---- 从表头第 2 行读「标签：值」 ----
Private Function MetaValue(ByVal ws As Worksheet, ByVal label As String) As String
    Dim text As String, p As Long, q As Long
    text = Trim$(CStr(ws.Cells(META_ROW, 1).Value & ""))
    If Len(text) = 0 Then Exit Function
    p = InStr(1, text, label, vbBinaryCompare)
    If p = 0 Then Exit Function
    text = Mid$(text, p + Len(label))
    Do While Len(text) > 0
        Select Case Left$(text, 1)
            Case "：", ":", " ", "　"
                text = Mid$(text, 2)
            Case Else
                Exit Do
        End Select
    Loop
    q = InStr(1, text, "｜", vbBinaryCompare)
    If q > 0 Then text = Left$(text, q - 1)
    MetaValue = Trim$(text)
End Function

' ---- 从表头第 2 行读被评模型与版本 ----
Private Sub ReadMetaModel(ByVal ws As Worksheet, ByRef modelId As String, ByRef modelVersion As String)
    Dim text As String, p As Long
    text = MetaValue(ws, "被评模型")
    If Len(text) = 0 Then Exit Sub
    p = InStr(1, text, " ", vbBinaryCompare)
    If p > 0 Then
        modelId = Trim$(Left$(text, p - 1))
        modelVersion = Trim$(Mid$(text, p + 1))
    Else
        modelId = Trim$(text)
    End If
End Sub
