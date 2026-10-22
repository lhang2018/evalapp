"""示例工作流：AI 漫剧能力包（PRD 78 / Architecture 16.3）。

演示 DAG 依赖、并行准备、节点依赖输出引用（"$node_id"）、补偿与预算。
当前节点用内置工具模拟生成环节；接入真实生成服务时替换为服务适配器工具，
只需重新验证受影响节点，不要求重写整个漫剧工作流（Arch 16.4）。
"""

from ..tools.builtin import calculate_tool, current_time_tool, file_write_tool
from ..tools.registry import ToolRegistry
from .models import WorkflowDef, WorkflowNode


def register_comic_drama_tools(registry: ToolRegistry) -> None:
    # 生成服务适配器（Mock）：真实接入时替换为带有任务状态/费用/错误码的服务适配器
    registry.register_mock_tool("mock_script_gen", lambda a: f"第1幕: {a.get('theme','剧本')}", side_effect_level="none")
    registry.register_mock_tool("mock_video_clip", lambda a: f"clip_{a.get('scene', 1)}.mp4", side_effect_level="none")
    registry.register_mock_tool("mock_dubbing", lambda a: "dubbing_track_01.wav", side_effect_level="none")
    registry.register_mock_tool("mock_compose", lambda a: "final_30s.mp4", side_effect_level="none")


def comic_drama_workflow(theme: str, out_path: str = "outputs/final_30s.mp4") -> WorkflowDef:
    """30~60 秒动态漫剧流水线（PRD 78.1 任务流程）。"""
    return WorkflowDef(
        workflow_id="comic_drama_demo", version="1.0.0",
        name="AI 漫剧 30s Demo",
        input_schema={"type": "object"},  # theme is frozen in the constructed node args.
        preconditions=["用户已确认创作主题"],
        nodes=[
            WorkflowNode(node_id="clock", tool="current_time",
                         description="记录开始时间"),
            WorkflowNode(node_id="script", tool="mock_script_gen",
                         args={"theme": theme},
                         description="剧本生成"),
            WorkflowNode(node_id="storyboard", tool="calculate",
                         args={"expression": "6"},   # 6 个镜头
                         description="分镜设计"),
            WorkflowNode(node_id="clip", tool="mock_video_clip",
                         deps=["script", "storyboard"],
                         args={"scene": 1},
                         description="镜头素材生成（依赖剧本与分镜）"),
            WorkflowNode(node_id="dubbing", tool="mock_dubbing",
                         deps=["script"],
                         description="配音生成（依赖剧本）"),
            WorkflowNode(node_id="compose", tool="mock_compose",
                         deps=["clip", "dubbing"],
                         description="音画合成为成片"),
            WorkflowNode(node_id="delivery", tool="file_write",
                         deps=["compose"],
                         requires_confirmation=False,
                         args={"path": "outputs/delivery_note.txt",
                               "content": "$compose"},
                         description="交付产物记录"),
        ],
        acceptance_criteria=[
            {"type": "tool_succeeded", "tool": "mock_compose"},
            {"type": "result_nonempty"},
        ],
        budget={"max_cost": 0.5},
        compensation_actions={},  # A directory listing cannot prove restoration.
        created_from_task=None,
    )
