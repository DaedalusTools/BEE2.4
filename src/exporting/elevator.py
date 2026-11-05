"""Export selected elevator videos."""
import trio.lowlevel
from srctools import Keyvalues

from transtoken import AppError
from . import ExportData, STEPS, StepResource
from packages import Elevator, Style, TRANS_OBJ_NOT_FOUND
import utils


@STEPS.add_step(prereq=[], results=[StepResource.VCONF_DATA])
async def step_elevator_video(exp_data: ExportData) -> None:
    """Export the chosen video into the configs."""
    await trio.lowlevel.checkpoint()
    elevator: Elevator | None
    sel_id: utils.SpecialID = exp_data.selected(Elevator.export_info)
    if sel_id == utils.ID_NONE:
        elevator = None
    else:
        try:
            elevator = exp_data.packset.obj_by_id(Elevator, sel_id)
        except KeyError:
            raise AppError(TRANS_OBJ_NOT_FOUND.format(object="Elevator", id=sel_id)) from None
    await trio.lowlevel.checkpoint()

    assemble_elevator(exp_data.selected_style, elevator, exp_data.vbsp_conf)


def assemble_elevator(style: Style, elevator: Elevator | None, vbsp_conf: Keyvalues) -> None:
    """Assemble video options without requiring a game installation."""
    if style.has_video:
        if elevator is None:
            # Use a randomised video
            vbsp_conf.set_key(('Elevator', 'type'), 'RAND')
        elif elevator.id == 'VALVE_BLUESCREEN':
            # This video gets a special script and handling
            vbsp_conf.set_key(('Elevator', 'type'), 'BSOD')
        else:
            # Use the particular selected video
            vbsp_conf.set_key(('Elevator', 'type'), 'FORCE')
            vbsp_conf.set_key(('Elevator', 'horiz'), elevator.horiz_video)
            vbsp_conf.set_key(('Elevator', 'vert'), elevator.vert_video)
    else:  # No elevator video for this style
        vbsp_conf.set_key(('Elevator', 'type'), 'NONE')
