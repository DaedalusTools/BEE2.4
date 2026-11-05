"""Export player model configuration."""
import trio

from config.player import ExportPlayer
from exporting import ExportData, STEPS, StepResource
from packages import PackagesSet, PlayerModel
import config


@STEPS.add_step(prereq=[], results=[StepResource.CONFIG_DATA])
async def step_player_model(exp_data: ExportData) -> None:
    """Export player models."""
    exp_data.config = await assemble_player_models(exp_data.packset, exp_data.config)


async def assemble_player_models(packset: PackagesSet, compiler_config: config.Config) -> config.Config:
    """Assemble model options without a game export."""
    await trio.lowlevel.checkpoint()
    conf: dict[str, ExportPlayer] = {}

    for player in packset.all_obj(PlayerModel):
        await trio.lowlevel.checkpoint()
        conf[player.id] = ExportPlayer(
            model=player.model,
            pgun_skin=player.pgun_skin,
            voice_options=player.voice_options
        )
    return compiler_config.with_cls_map(ExportPlayer, conf)
