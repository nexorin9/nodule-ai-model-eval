"""表格侧：评估表模板生成与回填。

本包的模块分三层：

- :mod:`nodule_eval.sheets.columns` —— **列契约的唯一真源**（既有列 + 产品新增六列）。
  模板生成（:mod:`nodule_eval.sheets.template`）与回填（任务 46 的 ``fill.py``）都从这里取列名与顺序，
  两边各写一份就会在「回填写到第几列」上漂移。
- :mod:`nodule_eval.sheets.tokens` —— **视觉 token 的唯一真源**（色板 / 字体 / 字号 / 行高 /
  固定列宽 / 数字格式 + 三档判定底色的条件格式）。模板生成与回填共用同一份，
  改一处两处生效；「低饱和可打印」写成可测函数而不是口头保证。
- :mod:`nodule_eval.sheets.template` —— 用 ``openpyxl`` 生成评估表模板：
  **四个区块**（表头汇总区 / 逐例明细区 / 漏诊明细区 / 底部状态栏）与版面地图
  （:class:`~nodule_eval.sheets.template.TemplateLayout`）。
- :mod:`nodule_eval.sheets.sections` —— **区块标记契约**：明细区之下每块首格写
  ``【漏诊明细】`` / ``【运行状态】``，模板写入方与读取层共用一份常量 ——
  读层见到标记即停，漏诊区里的检查号不会被当成新的病例读进来。
- :mod:`nodule_eval.sheets.macro_contract` —— **宏 ↔ 引擎落盘 JSON 交接协议的唯一真源**
  （任务 45）：文件名、请求 / 响应字段名、**回填只写新增六列**、汇总区 15 个固定地址
  与各自取自哪个字段、三个按钮的命令行参数。Excel VBA 版（``xlsm_vba/*.bas``）与
  WPS JSA 版（``wps_jsa/nodule_eval.js``）两份宏**逐字引用**这里的常量 ——
  两份宏各写一份字面量，就是同一个静默事故的两个入口；
  ``tests/test_sheet_contract.py`` 把两份宏源码与这份契约逐项对照。
- :mod:`nodule_eval.sheets.status` —— **运行状态契约的唯一真源**（任务 46）：
  ``runs/<run_id>/engine_status.json``（引擎原子写、宏轮询读）+ **三态文案**
  （加载态进度 + 当前例号 + 心跳 / 空态「本表未圈定病例」「本次未发现漏判」/
  错误态错误码 + 处置提示）。状态栏文本由引擎渲染好写进文件，**宏只搬那一行** ——
  两边各写一份中文说法，改了引擎那版就会漂。
- :mod:`nodule_eval.sheets.fill` —— **逐例回填**（任务 46）：只写新增六列 +
  汇总区固定地址 + 漏诊区 + 状态栏；**既有三列回填前后逐格比对**，对不上即放弃落盘；
  **原子替换**落盘，中途崩了原表一个字节都不变（不留半截表）。
"""
