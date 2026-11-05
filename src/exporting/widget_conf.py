"""Widget configs can be copied over directly, but need to ensure all defaults are filled in."""
import srctools.logger
import trio.lowlevel
from collections.abc import Mapping

from config.widgets import WidgetConfig
from packages.widgets import ConfigGroup, MultiWidget
from . import STEPS, ExportData, StepResource
import config
import packages

LOGGER = srctools.logger.get_logger(__name__)


@STEPS.add_step(prereq=[], results=[StepResource.CONFIG_DATA])
async def set_apply_widget_defaults(exp_data: ExportData) -> None:
    """Ensure default values are copied across."""
    exp_data.config = await apply_widget_defaults(exp_data.packset, exp_data.config)


async def apply_widget_defaults(
    packset: packages.PackagesSet,
    compiler_config: config.Config,
) -> config.Config:
    """Fill package widget defaults without requiring an installation export."""
    await trio.lowlevel.checkpoint()
    for group in packset.all_obj(ConfigGroup):
        await trio.lowlevel.checkpoint()
        for widget in group.widgets + group.multi_widgets:
            conf_id = widget.conf_id()
            if widget.has_values:
                try:
                    compiler_config.get(WidgetConfig, conf_id)
                    continue
                except KeyError:
                    # Apply defaults.
                    LOGGER.debug('Applying default for {}', conf_id)
                    compiler_config = compiler_config.with_value(widget.create_conf(), conf_id)
            else:
                # Strip item variants if present.
                compiler_config, value = compiler_config.discard(WidgetConfig, conf_id)
                if value is not None:
                    LOGGER.debug('Discarding config for {}', conf_id)
    return compiler_config


def apply_widget_values(
    packset: packages.PackagesSet,
    compiler_config: config.Config,
    values: Mapping[str, str | Mapping[str, str]],
) -> config.Config:
    """Apply opaque compiler strings using the widget holders' load semantics.

    Validate identities and scalar/timer shapes, not UI-specific value coercion.
    No holders, observers or global configuration are changed.
    """
    widgets = {
        widget.conf_id(): widget
        for group in packset.all_obj(ConfigGroup)
        for widget in group.widgets + group.multi_widgets
    }
    for conf_id, value in values.items():
        try:
            widget = widgets[conf_id]
        except KeyError:
            continue
        if not widget.has_values:
            raise ValueError(f'Widget "{conf_id}" selects an item version, not compiler values.')
        if isinstance(widget, MultiWidget):
            previous = compiler_config.get(WidgetConfig, conf_id).values
            effective = dict(widget.create_conf().values)
            for timer in effective:
                if isinstance(previous, str):
                    effective[timer] = previous
                elif timer in previous:
                    effective[timer] = previous[timer]
            if isinstance(value, str):
                effective = dict.fromkeys(effective, value)
            elif isinstance(value, Mapping) and all(
                timer in effective and isinstance(setting, str) for timer, setting in value.items()
            ):
                effective.update(value)
            else:
                raise ValueError(f'Invalid timer values for widget "{conf_id}".')
            value = effective
        elif not isinstance(value, str):
            raise ValueError(f'Widget "{conf_id}" requires a scalar string.')
        compiler_config = compiler_config.with_value(WidgetConfig(value), conf_id)
    return compiler_config
