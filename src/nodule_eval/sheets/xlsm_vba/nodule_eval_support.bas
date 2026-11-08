Attribute VB_Name = "NoduleEvalSupport"
Option Explicit

'==========================================================================
' 肺结节AI模型评估系统 · 宏支撑模块（Excel VBA 版）
'
' 这里只放**与业务无关的底层动作**：
'   1. UTF-8 文件读写（不带 BOM）
'   2. JSON 字符串转义与标量取值
'   3. CSV 单行拆分（带引号）
'   4. 目录创建、环境变量读取
'   5. 起外部进程并等它跑完
'
' 三个按钮的业务流程在 NoduleEval 模块里。
'
' 为什么不引第三方库：院内 PC 常无外网、不能装包，宏必须「拷过去就能跑」。
' 不依赖 ActiveX 控件、不依赖 UserForm、不依赖 FSO / ADODB ——
' 只用 Excel 公共 API 与 Windows 自带能力。
'
' 编码说明：本文件以 UTF-8 保存（便于在代码仓库里看 diff）。
' VBA 编辑器按系统 ANSI 代码页读 .bas，导入前请先转码：
'   python scripts/convert_bas_to_ansi.py src/nodule_eval/sheets/xlsm_vba
' 转码后中文提示与中文表头别名才认得出（ASCII 字段名不受影响）。
'==========================================================================

' ---- 引擎跑完的完成标记（纯 ASCII，宏文件转码错了也判得出来） ----
Public Const DONE_MARKER As String = "__ENGINE_DONE__"


' ---- UTF-8 写文件（不带 BOM） ----
Public Sub WriteUtf8File(ByVal path As String, ByVal text As String)
    Dim n As Long, i As Long, pos As Long, code As Long
    Dim raw() As Byte
    Dim f As Integer

    n = Len(text)
    If n = 0 Then
        f = FreeFile
        Open path For Binary Access Write As #f
        Close #f
        Exit Sub
    End If

    ' 按 UTF-8 逐字符编码；最坏情况一个字符 3 字节
    ReDim raw(0 To n * 3 - 1)
    pos = 0
    For i = 1 To n
        code = AscW(Mid$(text, i, 1))
        If code < 0 Then code = code + 65536
        If code < 128 Then
            raw(pos) = CByte(code)
            pos = pos + 1
        ElseIf code < 2048 Then
            raw(pos) = CByte(192 + (code \ 64))
            raw(pos + 1) = CByte(128 + (code Mod 64))
            pos = pos + 2
        Else
            raw(pos) = CByte(224 + (code \ 4096))
            raw(pos + 1) = CByte(128 + ((code \ 64) Mod 64))
            raw(pos + 2) = CByte(128 + (code Mod 64))
            pos = pos + 3
        End If
    Next i

    ReDim Preserve raw(0 To pos - 1)
    f = FreeFile
    Open path For Binary Access Write As #f
    Put #f, , raw
    Close #f
End Sub

' ---- UTF-8 读文件（去掉 BOM） ----
Public Function ReadUtf8File(ByVal path As String) As String
    Dim fnum As Integer, size As Long, i As Long
    Dim raw() As Byte
    Dim out As String
    Dim run As String
    Dim b As Long, code As Long

    If Len(Dir$(path)) = 0 Then
        ReadUtf8File = ""
        Exit Function
    End If

    size = FileLen(path)
    If size = 0 Then
        ReadUtf8File = ""
        Exit Function
    End If

    ReDim raw(0 To size - 1)
    fnum = FreeFile
    Open path For Binary Access Read As #fnum
    Get #fnum, , raw
    Close #fnum

    ' 去掉 UTF-8 BOM（EF BB BF）
    If size >= 3 Then
        If raw(0) = 239 And raw(1) = 187 And raw(2) = 191 Then
            For i = 3 To size - 1
                raw(i - 3) = raw(i)
            Next i
            size = size - 3
        End If
    End If

    out = ""
    run = ""
    i = 0
    Do While i < size
        b = raw(i)
        If b < 128 Then
            code = b
            i = i + 1
        ElseIf b < 224 Then
            code = ((b - 192) * 64) + (raw(i + 1) - 128)
            i = i + 2
        Else
            code = ((b - 224) * 4096) + ((raw(i + 1) - 128) * 64) + (raw(i + 2) - 128)
            i = i + 3
        End If
        If code < 128 Then
            run = run & Chr$(code)
        Else
            If Len(run) > 0 Then
                out = out & run
                run = ""
            End If
            out = out & ChrW(code)
        End If
    Loop
    If Len(run) > 0 Then out = out & run

    ReadUtf8File = out
End Function

' ---- JSON 字符串转义并加引号 ----
Public Function JsonQuote(ByVal s As String) As String
    Dim i As Long, ch As String, out As String, code As Long
    out = Chr$(34)
    For i = 1 To Len(s)
        ch = Mid$(s, i, 1)
        Select Case ch
            Case Chr$(34)
                out = out & "\"""
            Case "\"
                out = out & "\\"
            Case Chr$(10)
                out = out & "\n"
            Case Chr$(13)
                out = out & "\r"
            Case Chr$(9)
                out = out & "\t"
            Case Else
                code = AscW(ch)
                If code < 0 Then code = code + 65536
                If code < 32 Then
                    out = out & "\u" & Right$("000" & Hex(code), 4)
                Else
                    out = out & ch
                End If
        End Select
    Next i
    JsonQuote = out & Chr$(34)
End Function

' ---- 按键取 JSON 里的标量值 ----
' 返回字符串时**已解码**（\n \r \t 还原成控制字符）；数字 / 布尔原样返回；
' 取不到键返回空串；JSON 的 null 返回 "null"（调用方据此写占位符）。
Public Function JsonScalar(ByVal text As String, ByVal key As String) As String
    Dim needle As String, p As Long, i As Long, ch As String, esc As String
    Dim out As String

    needle = Chr$(34) & key & Chr$(34) & ":"
    p = InStr(1, text, needle, vbBinaryCompare)
    If p = 0 Then
        JsonScalar = ""
        Exit Function
    End If

    i = p + Len(needle)
    Do While i <= Len(text)
        ch = Mid$(text, i, 1)
        If ch <> " " And ch <> vbTab And ch <> vbCr And ch <> vbLf Then Exit Do
        i = i + 1
    Loop
    If i > Len(text) Then
        JsonScalar = ""
        Exit Function
    End If

    ch = Mid$(text, i, 1)
    If ch = Chr$(34) Then
        i = i + 1
        Do While i <= Len(text)
            ch = Mid$(text, i, 1)
            If ch = "\" Then
                esc = Mid$(text, i + 1, 1)
                Select Case esc
                    Case "n": out = out & vbLf
                    Case "r": out = out & vbCr
                    Case "t": out = out & vbTab
                    Case Else: out = out & esc
                End Select
                i = i + 2
            ElseIf ch = Chr$(34) Then
                Exit Do
            Else
                out = out & ch
                i = i + 1
            End If
        Loop
    Else
        Do While i <= Len(text)
            ch = Mid$(text, i, 1)
            If ch = "," Or ch = "}" Or ch = "]" Or ch = vbCr Or ch = vbLf Then Exit Do
            out = out & ch
            i = i + 1
        Loop
        out = Trim$(out)
    End If

    JsonScalar = out
End Function

' ---- CSV 单行拆分（支持双引号包裹与转义的双引号） ----
' 约定：引擎写出的逐例明细里每个字段都是**单行文本**，不含换行。
Public Function CsvSplit(ByVal line As String) As Variant
    Dim fields() As String
    Dim n As Long, i As Long
    Dim cur As String, ch As String
    Dim inQuotes As Boolean

    ReDim fields(0 To 63)
    n = 0
    cur = ""
    inQuotes = False
    i = 1
    Do While i <= Len(line)
        ch = Mid$(line, i, 1)
        If inQuotes Then
            If ch = Chr$(34) Then
                If i < Len(line) And Mid$(line, i + 1, 1) = Chr$(34) Then
                    cur = cur & Chr$(34)
                    i = i + 1
                Else
                    inQuotes = False
                End If
            Else
                cur = cur & ch
            End If
        Else
            If ch = Chr$(34) Then
                inQuotes = True
            ElseIf ch = "," Then
                fields(n) = cur
                cur = ""
                n = n + 1
            Else
                cur = cur & ch
            End If
        End If
        i = i + 1
    Loop
    fields(n) = cur
    n = n + 1
    ReDim Preserve fields(0 To n - 1)
    CsvSplit = fields
End Function

' ---- 建目录（多级，父目录不存在就逐级建） ----
Public Sub EnsureDir(ByVal path As String)
    Dim parent As String
    If Len(Trim$(path)) = 0 Then Exit Sub
    If Len(Dir$(path, vbDirectory)) > 0 Then Exit Sub
    parent = Left$(path, InStrRev(path, "\"))
    If Len(parent) > 3 And Right$(parent, 1) = "\" Then
        EnsureDir parent
    End If
    On Error Resume Next
    MkDir path
    On Error GoTo 0
End Sub

' ---- 读环境变量（空值回落缺省） ----
Public Function EnvValue(ByVal name As String, ByVal fallback As String) As String
    Dim v As String
    v = Environ$(name)
    If Len(Trim$(v)) = 0 Then
        EnvValue = fallback
    Else
        EnvValue = v
    End If
End Function

' ---- 文件是否存在 ----
Public Function FileExistsSafe(ByVal path As String) As Boolean
    If Len(Trim$(path)) = 0 Then Exit Function
    FileExistsSafe = (Len(Dir$(path)) > 0)
End Function

' ---- 起外部进程（**不等待**） ----
' 引擎起来之后由调用方自己轮询状态文件刷新界面（表格不冻结）。
' 返回空串表示已起；返回非空串是失败原因。
'
' 为什么不在这里等：等待逻辑里要**边等边刷状态栏**
' （每半秒读一次 engine_status.json），那件事归主模块（它拿得到工作表）。
Public Function StartEngineProcess(ByVal cmd As String, ByVal logPath As String) As String
    Dim fullCmd As String

    On Error Resume Next
    Kill logPath
    On Error GoTo 0

    ' 引擎正常结束才写完成标记；引擎报错时 exit code 非零，&& 短路，不会写标记
    fullCmd = cmd & " > """ & logPath & """ 2>&1 && echo " & DONE_MARKER & " >> """ & logPath & """"

    On Error Resume Next
    Shell fullCmd, 1
    If Err.Number <> 0 Then
        StartEngineProcess = "起引擎失败：" & Err.Description
        Err.Clear
        Exit Function
    End If
    On Error GoTo 0

    StartEngineProcess = ""
End Function

' ---- 起外部进程并等它跑完 ----
' cmd        完整命令行（不含重定向）
' logPath    标准输出与错误输出的去处
' timeoutSec 超时秒数
' message    失败原因（中文，可直接显示给人看）
'
' 判定成败有两条依据，都不依赖进程句柄：
'   1. 命令行尾部追加的完成标记（DONE_MARKER，纯 ASCII）—— 引擎正常跑完才会写它；
'   2. 日志里出现以 "[" 开头的行 —— 引擎把错误按「[错误码] 文案」打在标准错误上。
' 两条都用 ASCII/结构判断，不依赖中文字面量，宏文件转码错了也判得出来。
Public Function RunEngineAndWait(ByVal cmd As String, ByVal logPath As String, _
                                  ByVal timeoutSec As Long, ByRef message As String) As Boolean
    Dim started As Double
    Dim logText As String
    Dim errLine As String
    Dim fullCmd As String

    On Error Resume Next
    Kill logPath
    On Error GoTo 0

    ' 引擎正常结束才写完成标记；引擎报错时 exit code 非零，&& 短路，不会写标记
    fullCmd = cmd & " > """ & logPath & """ 2>&1 && echo " & DONE_MARKER & " >> """ & logPath & """"
    Shell fullCmd, 1

    started = Timer
    Do
        DoEvents
        If FileExistsSafe(logPath) Then
            logText = ReadUtf8File(logPath)
            errLine = FirstErrorLine(logText)
            If Len(errLine) > 0 Then
                message = errLine & vbCrLf & "（引擎日志：" & logPath & "）"
                RunEngineAndWait = False
                Exit Function
            End If
            If InStr(1, logText, DONE_MARKER, vbBinaryCompare) > 0 Then
                message = ""
                RunEngineAndWait = True
                Exit Function
            End If
        End If
        If (Timer - started) > timeoutSec Then
            message = "引擎超过 " & timeoutSec & " 秒仍未结束，已中止等待（详见 " & logPath & "）"
            RunEngineAndWait = False
            Exit Function
        End If
        Wait 1
    Loop
End Function

' ---- 从日志里找出第一行错误（以 "[" 开头） ----
Public Function FirstErrorLine(ByVal logText As String) As String
    Dim lines() As String
    Dim i As Long
    lines = Split(logText, vbLf)
    For i = LBound(lines) To UBound(lines)
        If Len(Trim$(lines(i))) > 0 Then
            If Left$(Trim$(lines(i)), 1) = "[" Then
                FirstErrorLine = Trim$(lines(i))
                Exit Function
            End If
        End If
    Next i
    FirstErrorLine = ""
End Function

' ---- 从日志里取某个标签后面的值（引擎的中文输出行） ----
Public Function ExtractLogValue(ByVal logText As String, ByVal label As String) As String
    Dim lines() As String
    Dim i As Long, p As Long
    lines = Split(logText, vbLf)
    For i = LBound(lines) To UBound(lines)
        p = InStr(1, lines(i), label, vbBinaryCompare)
        If p > 0 Then
            ExtractLogValue = Trim$(Mid$(lines(i), p + Len(label)))
            Exit Function
        End If
    Next i
    ExtractLogValue = ""
End Function

' ---- 日志尾部若干行（给人看的错误摘要） ----
Public Function TailLines(ByVal logText As String, ByVal count As Long) As String
    Dim lines() As String
    Dim i As Long, from As Long
    lines = Split(logText, vbLf)
    from = UBound(lines) - count + 1
    If from < LBound(lines) Then from = LBound(lines)
    For i = from To UBound(lines)
        If Len(Trim$(lines(i))) > 0 Then
            TailLines = TailLines & Trim$(lines(i)) & vbCrLf
        End If
    Next i
End Function
