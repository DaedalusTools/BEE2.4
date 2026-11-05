"""Generate fizzler side materials."""
from pathlib import Path

from fizzler_materials import generate_fizzler_side_materials

from . import ExportData, STEPS, StepResource


@STEPS.add_step(
    prereq=[StepResource.VCONF_DATA],
    results=[StepResource.RES_SPECIAL],
)
async def generate_fizzler_sides(exp_data: ExportData) -> None:
    """Create the VMTs used for fizzler sides."""
    exp_data.resources.update(await generate_fizzler_side_materials(
        exp_data.vbsp_conf, Path(exp_data.game.abs_path("bee2/materials")),
    ))
