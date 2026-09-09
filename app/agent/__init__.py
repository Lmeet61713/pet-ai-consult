"""问诊 Agent 包（app.agent）—— 固定状态机驱动的宠物问诊编排层。

【包职责】
本包实现问诊请求的核心编排逻辑：接收 ConsultCommand 后，按预定义的固定
状态机顺序执行输入审核、图片分析、RAG 检索、完整度判断、风险分级、回答
生成、医疗审核、输出审核等阶段，最终组装 ConsultResponse。

设计红线（v5 §9）：不允许模型自由改变执行顺序，所有阶段迁移都在
state_machine.py 的 _ALLOWED 表中声明式穷举。

【模块组成】
- consult_agent.py：ConsultAgent 主控制器（状态机编排、降级/兜底、幂等、存档）
- state.py：ConsultState 状态总线（单次请求生命周期内各阶段共享的"黑板"）
- state_machine.py：AgentState 状态枚举（14 态）+ 允许迁移表 + 迁移校验
- completeness_checker.py：信息完整度判断（决定正常生成 / provisional 追问 / 硬缺失）
- followup_tracker.py：多轮病例事实抽取（第一期覆盖眼部结构化槽位）
- risk_engine.py：风险聚合引擎（急症规则 + 图片质量 + 病例事实，只升不降）

【数据流】
    ConsultCommand
        → ConsultState.from_command()（state.py）
        → ConsultAgent.run()/run_stream()（consult_agent.py）
        → 各阶段产物回写 state
        → ConsultResponse
"""
