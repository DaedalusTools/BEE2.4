"""Export corridor configuration."""
from __future__ import annotations

import pickle
import itertools

from srctools import Vec
import srctools.logger
import trio

from config.corridors import Config, Options
from corridor import (
    CorrKind, Corridor, OptionGroup, Direction, GameMode, Attachment, ExportedConf,
    CORRIDOR_COUNTS, ID_TO_CORR,
)
from transtoken import AppError, TransToken
from . import ExportData, STEPS, StepResource
from packages.corridor import CorridorGroup, CorridorUI
import config
import editoritems
import packages


LOGGER = srctools.logger.get_logger(__name__)
ALL_OPTION_KINDS: list[OptionGroup] = [
    (mode, direction)
    for mode in GameMode for direction in Direction
]
TRANS_MISSING_GROUP = TransToken.ui(
    'No corridor definitions found for style "{id}"!'
)


@STEPS.add_step(prereq=[StepResource.EI_ITEMS], results=[StepResource.EI_DATA, StepResource.VCONF_DATA])
async def step_corridor_conf(exp_data: ExportData) -> None:
    """Override editoritems with the new corridor specifier."""
    result, exp_data.config = await assemble_corridors(
        exp_data.packset, exp_data.selected_style, exp_data.all_items,
        exp_data.config, exp_data.vbsp_conf,
    )
    LOGGER.info('Writing corridor configuration...')
    pickle_data = await trio.to_thread.run_sync(pickle.dumps, result, pickle.HIGHEST_PROTOCOL)
    await trio.Path(exp_data.game.abs_path('bin/bee2/corridors.bin')).write_bytes(pickle_data)


async def assemble_corridors(
    packset: packages.PackagesSet,
    selected_style: packages.Style,
    all_items: list[editoritems.Item],
    compiler_config: config.Config,
    vbsp_conf: srctools.Keyvalues,
) -> tuple[ExportedConf, config.Config]:
    """Assemble corridor configuration and editor instances without game writes."""
    result, compiler_config, selected = await _resolve_corridor_content(
        packset, selected_style, all_items, compiler_config,
    )
    for corridor in selected:
        vbsp_conf.extend(await corridor.config())
    return result, compiler_config


async def resolve_corridor_content(
    packset: packages.PackagesSet,
    selected_style: packages.Style,
    all_items: list[editoritems.Item],
    options: config.Config,
) -> tuple[ExportedConf, config.Config]:
    """Resolve definitions and option defaults without reading compiler conditions."""
    result, options, _ = await _resolve_corridor_content(packset, selected_style, all_items, options)
    return result, options


async def _resolve_corridor_content(
    packset: packages.PackagesSet,
    selected_style: packages.Style,
    all_items: list[editoritems.Item],
    compiler_config: config.Config,
) -> tuple[ExportedConf, config.Config, list[CorridorUI]]:
    await trio.lowlevel.checkpoint()
    style_id = selected_style.id
    try:
        group = packset.obj_by_id(CorridorGroup, style_id)
    except KeyError:
        raise AppError(TRANS_MISSING_GROUP.format(
            id=style_id,
        )) from None

    export: dict[CorrKind, list[Corridor]] = {}
    selected = []
    for mode, direction, attach in itertools.product(GameMode, Direction, Attachment):
        conf = config.APP.get_cur_conf(
            Config,
            Config.get_id(style_id, mode, direction, attach),
        )
        try:
            inst_to_corr = {
                corr.instance.casefold(): corr
                for corr in group.corridors[mode, direction, attach]
            }
        except KeyError:
            # None defined for this corridor. This is not an error for vertical ones.
            (LOGGER.warning if attach is Attachment.HORIZONTAL else LOGGER.debug)(
                'No corridors defined for {}:{}_{}_{}',
                style_id, mode.value, direction.value, attach.value,
            )
            export[mode, direction, attach] = []
            continue

        if conf.enabled:
            chosen = [
                corr
                for corr_id, enabled in conf.enabled.items()
                if enabled and (corr := inst_to_corr.get(corr_id.casefold())) is not None
            ]

            if not chosen:
                LOGGER.warning(
                    'No corridors selected for {}:{}_{}_{}',
                    style_id,
                    mode.value, direction.value, attach.value,
                )
                chosen = group.defaults(mode, direction, attach)
        else:
            # Use default setup, don't warn.
            chosen = group.defaults(mode, direction, attach)

        opt_id = Options.get_id(style_id, mode, direction)
        options_changed = False
        try:
            exported_options = dict(compiler_config.get(Options, opt_id).options)
        except KeyError:
            exported_options = {}
            options_changed = True

        for corr in chosen:
            selected.append(corr)
            for option in group.get_options(mode, direction, corr):
                # If an option is not present in the config, add it.
                if option.id not in exported_options:
                    LOGGER.debug('Applying default {}[{!r}] = {!r}', opt_id, option.id, option.default)
                    exported_options[option.id] = option.default
                    options_changed = True

        if options_changed:
            compiler_config = compiler_config.with_value(Options(exported_options), opt_id)

        export[mode, direction, attach] = [corr.strip_ui() for corr in chosen]

    result = ExportedConf(
        corridors=export,
        global_opt_ids={
            kind: frozenset({opt.id for opt in group.global_options.get(kind, ())})
            for kind in ALL_OPTION_KINDS
        },
        options=group.options.copy(),
    )

    # Change out all the instances in items to names following a pattern.
    # This allows the compiler to easily recognise. Also force a few specific options.
    for item in all_items:
        try:
            (mode, direction) = ID_TO_CORR[item.id]
        except KeyError:
            continue
        count = CORRIDOR_COUNTS[mode, direction]
        # For all items these are at the start.
        for i in range(count):
            item.set_inst(i, editoritems.InstCount(editoritems.FSPath(
                f'instances/bee2_corridor/{mode.value}/{direction.value}/corr_{i + 1}.vmf'
            )))
        item.offset = Vec(64, 64, 64)
        # If vertical corridors exist, allow placement there.
        has_vert = False
        if export[mode, direction, Attachment.FLOOR]:
            item.invalid_surf.discard(editoritems.Surface.FLOOR)
            has_vert = True
        if export[mode, direction, Attachment.CEILING]:
            item.invalid_surf.discard(editoritems.Surface.CEIL)
            has_vert = True
        if has_vert:
            # Add a rotation handle.
            item.handle = editoritems.Handle.QUAD
        # Set desired facing to make them face upright, no matter what.
        item.facing = editoritems.DesiredFacing.UP
    return result, compiler_config, selected
