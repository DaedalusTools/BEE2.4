"""Export the selected skybox/fog configuration."""
from __future__ import annotations
from srctools import Keyvalues, bool_as_int

from transtoken import AppError
from . import ExportData, STEPS, StepResource
from packages import Skybox, TRANS_OBJ_NOT_FOUND
import utils


@STEPS.add_step(prereq=[], results=[StepResource.VCONF_DATA])
async def step_skybox(exp_data: ExportData) -> None:
    """Export the selected skybox."""
    sel_id = exp_data.selected(Skybox.export_info)
    if sel_id == utils.ID_NONE:
        return  # No skybox..

    try:
        skybox = exp_data.packset.obj_by_id(Skybox, sel_id)
    except KeyError:
        raise AppError(TRANS_OBJ_NOT_FOUND.format(object="Skybox", id=sel_id)) from None

    await assemble_skybox(skybox, exp_data.vbsp_conf)


async def assemble_skybox(skybox: Skybox, vbsp_conf: Keyvalues) -> None:
    """Assemble skybox and fog options without installation writes."""
    vbsp_conf.set_key(('Options', 'skybox'), skybox.material)
    vbsp_conf.set_key(('Options', 'sky_draw_first'), bool_as_int(skybox.draw_first))
    vbsp_conf.extend(await skybox.config())
    # Styles or other items shouldn't be able to set fog settings..
    if 'fog' in vbsp_conf:
        del vbsp_conf['fog']

    fog_opts = skybox.fog_opts.copy()
    fog_opts.name = 'Fog'

    vbsp_conf.append(fog_opts)
