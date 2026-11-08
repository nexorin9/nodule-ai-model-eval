"""出站适配层：RIS 读、推理写、科研平台写。

三个端点的**地址、鉴权头、请求体字段规范与响应字段规范**全部落在本包内；
凭据只从环境变量读（:mod:`nodule_eval.config`），代码里不出现任何默认端点地址。

- :mod:`nodule_eval.adapters.http` —— 统一出站会话层（超时分离、退避重试、错误码映射）
- :mod:`nodule_eval.adapters.inference` —— 厂商肺结节 AI 推理服务
- :mod:`nodule_eval.adapters.ris` —— 院内 RIS 检查清单（任务 7）
- :mod:`nodule_eval.adapters.research_platform` —— 院内科研平台评估登记（任务 35）
"""
