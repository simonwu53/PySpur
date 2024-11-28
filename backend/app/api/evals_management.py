from fastapi import APIRouter, HTTPException, BackgroundTasks, Depends
from sqlalchemy.orm import Session
from pathlib import Path
import yaml
from typing import List, Dict, Any
from pydantic import BaseModel

from ..database import get_db
from ..models.workflow_model import WorkflowModel
from ..evals.evaluator import prepare_and_evaluate_dataset, load_yaml_config
from ..schemas.workflow_schemas import WorkflowDefinitionSchema

router = APIRouter()

EVALS_DIR = Path(__file__).parent.parent / "evals" / "tasks"


class EvalRunRequest(BaseModel):
    eval_name: str
    workflow_id: str
    output_variable: str
    num_samples: int = 10


@router.get("/", description="List all available evals")
def list_evals() -> List[Dict[str, Any]]:
    """
    List all available evals by scanning the tasks directory for YAML files.
    """
    evals = []
    if not EVALS_DIR.exists():
        raise HTTPException(status_code=500, detail="Evals directory not found")
    for eval_file in EVALS_DIR.glob("*.yaml"):
        try:
            eval_content = load_yaml_config(yaml_path=eval_file)
            metadata = eval_content.get("metadata", {})
            evals.append(
                {
                    "name": metadata.get("name", eval_file.stem),
                    "description": metadata.get("description", ""),
                    "type": metadata.get("type", "Unknown"),
                    "num_samples": metadata.get("num_samples", "N/A"),
                    "paper_link": metadata.get("paper_link", ""),
                    "file_name": eval_file.name,
                }
            )
        except Exception as e:
            raise HTTPException(
                status_code=500, detail=f"Error parsing {eval_file.name}: {e}"
            )
    return evals


@router.post(
    "/launch/",
    response_model=Dict[str, Any],
    description="Launch an eval job with detailed validation and workflow integration",
)
async def launch_eval(
    request: EvalRunRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """
    Launch an eval job by triggering the evaluator with the specified eval configuration.
    """
    # Validate workflow ID
    workflow = (
        db.query(WorkflowModel).filter(WorkflowModel.id == request.workflow_id).first()
    )
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    eval_file = EVALS_DIR / f"{request.eval_name}.yaml"
    if not eval_file.exists():
        raise HTTPException(status_code=404, detail="Eval configuration not found")

    try:
        # Load the eval configuration
        eval_config = load_yaml_config(eval_file)

        # Validate the output variable
        workflow_definition = WorkflowDefinitionSchema.model_validate(
            workflow.definition
        )
        all_source_ids = {link.source_id for link in workflow_definition.links}
        all_node_ids = {node.id for node in workflow_definition.nodes}
        leaf_nodes = all_node_ids - all_source_ids

        # Extract all output variables from leaf nodes
        leaf_node_output_variables = {
            f"{node.id}-{output_variable}"
            for node in workflow_definition.nodes
            if node.id in leaf_nodes
            for output_variable in node.config.get("output_variables", [])
        }

        if request.output_variable not in leaf_node_output_variables:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid output variable '{request.output_variable}'. Must be one of: {leaf_node_output_variables}",
            )

        # Run the evaluation with mandatory workflow parameter
        results = await prepare_and_evaluate_dataset(
            eval_config,
            workflow=workflow_definition,  # Now required
            num_samples=request.num_samples,
            output_variable=request.output_variable,
        )

        return {
            "status": "success",
            "results": results,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error launching eval: {e}")
