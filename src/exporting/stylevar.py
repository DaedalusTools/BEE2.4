"""Export stylevar selections."""
from srctools import Keyvalues, bool_as_int
from collections.abc import Mapping
import trio

from config.widgets import WidgetConfig
from packages import ConfigGroup, PackagesSet, StyleVar
import config

from . import STEPS, ExportData, StepResource


@STEPS.add_step(prereq=[], results=[StepResource.VCONF_DATA])
async def step_add_stylevars(exp_data: ExportData) -> None:
    """Export style var selections into the config."""
    exp_data.vbsp_conf.append(await assemble_stylevars(
        exp_data.packset, exp_data.selected(StyleVar.export_info),
        config.APP.get_full_conf(),
    ))


async def assemble_stylevars(
    packset: PackagesSet,
    style_vars: Mapping[str, bool],
    widget_config: config.Config,
) -> Keyvalues:
    """Assemble style flags including legacy widgets without UI or game writes."""
    # Add the StyleVars block, containing each style_var.
    block = Keyvalues('StyleVars', [
        Keyvalues(key, bool_as_int(val))
        for key, val in
        style_vars.items()
    ])
    await trio.lowlevel.checkpoint()
    # Also add all widgets that were stylevars, so previous packages still function.
    for group in packset.all_obj(ConfigGroup):
        await trio.lowlevel.checkpoint()
        for widget in group.widgets:
            if widget.stylevar_id:
                try:
                    value = widget_config.get(WidgetConfig, widget.conf_id()).values
                except KeyError:
                    value = WidgetConfig().values
                if isinstance(value, str):
                    block[widget.stylevar_id] = value

    return block
