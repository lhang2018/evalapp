"""首批领域专家配置（PRD 9.3）。

新增专家不要求修改统一任务入口（PRD 9.3 末段）。
"""

from .registry import CapabilityEntry, Expert, ExpertRegistry


def builtin_experts() -> ExpertRegistry:
    reg = ExpertRegistry()

    reg.register_expert(Expert(
        expert_id="general", domain_description="通用问答、简单规划、跨领域任务分发",
        capabilities=["qa", "planning_basic", "dispatch"],
        applicable_intents=["general"],
        system_prompt="你是 EvoAgent 通用专家，负责理解目标并安全完成通用任务。",
        budget_class="standard",
        quality_metrics=["answer_relevance", "safety_compliance"],
    ))

    reg.register_expert(Expert(
        expert_id="coding", domain_description="项目分析/代码生成/调试/测试/依赖分析",
        capabilities=["code_gen", "debug", "test", "dependency_analysis"],
        applicable_intents=["coding", "computation"],
        not_applicable=["mobile_device_control"],
        system_prompt="负责代码相关任务：生成、修改、调试、测试。",
        tool_whitelist=["calculate", "file_read", "file_write", "fs_list"],
        memory_scope_key="coding",
        budget_class="generous",
        quality_metrics=["unit_test_pass_rate"],
        dependencies=["file_toolset"],
    ))

    reg.register_expert(Expert(
        expert_id="travel", domain_description="目的地研究/行程规划/预算估算/交通安排",
        capabilities=["itinerary", "budget_estimation", "info_verification"],
        applicable_intents=["travel", "weather"],
        system_prompt="负责旅行规划任务，注意动态信息需要核验。",
        tool_whitelist=["http_get", "current_time", "file_write"],
        memory_scope_key="travel",
    ))

    reg.register_expert(Expert(
        expert_id="comic_drama", domain_description="AI 漫剧：剧本/分镜/素材/视频/配音/合成",
        capabilities=["script", "storyboard", "video_gen", "dubbing", "composition"],
        applicable_intents=["comic_drama"],
        system_prompt="负责 AI 漫剧生成流水线（能力包，不是通用内核的一部分）。",
        workflow_ids=["comic_drama_demo"],
        memory_scope_key="comic_drama",
        budget_class="generous",
    ))

    reg.register_expert(Expert(
        expert_id="mobile_operation", domain_description="App 启动/页面识别/控件操作/状态验证",
        capabilities=["app_launch", "ui_operation", "state_verify"],
        applicable_intents=["mobile_operation", "note_taking"],
        not_applicable=["server_knowledge_qa"],
        system_prompt="负责 Android 设备操作；实际授权以系统为准。",
        tool_whitelist=["current_time", "file_write", "file_read"],
        memory_scope_key="mobile",
    ))

    # 能力注册表示例 + 依赖边
    reg.register_capability(CapabilityEntry(
        capability_id="file_toolset", version="1.0.0", domain="shared",
        description="文件读写工具集（共享内核）",
        tool_ids=["file_read", "file_write", "fs_list"],
    ), depends_on=[])
    reg.register_capability(CapabilityEntry(
        capability_id="coding_tasks", version="1.0.0", domain="coding",
        description="编程领域能力",
        tool_ids=["calculate", "file_write"],
    ), depends_on=["file_toolset"])

    return reg
