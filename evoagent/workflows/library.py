"""工作流资产库（PRD 13.4 / Architecture 16.3）。

内置/受控工作流持久化到 workflow_defs，供查询、复用与版本对比。
注意：函数节点与条件函数不可序列化，从库中加载的定义只能包含工具节点。
"""

from typing import Any, Dict, List, Optional

from .models import WorkflowDef


def save_workflow(db: Any, wf: WorkflowDef, status: str = "published") -> None:
    db.save_workflow_def(wf.workflow_id, wf.version, status, wf.to_dict())


def list_workflows(db: Any) -> List[Dict[str, Any]]:
    return db.list_workflow_defs()


def load_workflow(db: Any, workflow_id: str,
                  version: Optional[str] = None) -> Optional[WorkflowDef]:
    data = db.get_workflow_def(workflow_id, version)
    if not data:
        return None
    return WorkflowDef.from_dict(data)
