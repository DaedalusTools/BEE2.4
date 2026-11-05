"""Export glass/grating hole configurations."""
from srctools import Keyvalues
from aioresult import ResultCapture
import trio

from packages import BarrierHole, PackagesSet

from . import STEPS, ExportData, StepResource


@STEPS.add_step(prereq=[], results=[StepResource.VCONF_DATA])
async def step_barrier_hole(exp_data: ExportData) -> None:
    """Export glass/grating hole configurations."""
    conf_block = await assemble_barrier_holes(exp_data.packset)
    if conf_block:
        exp_data.vbsp_conf.append(conf_block)


async def assemble_barrier_holes(packset: PackagesSet) -> Keyvalues:
    """Resolve barrier-hole variants without a game export."""
    variant_conf: ResultCapture[Keyvalues]
    async with trio.open_nursery() as nursery:
        configs = [
            (hole, ResultCapture.start_soon(nursery, hole.variant_conf))
            for hole in packset.all_obj(BarrierHole)
        ]
    conf_block = Keyvalues('BarrierHoles', [])

    for hole, variant_conf in configs:
        kv = variant_conf.result()
        kv.name = 'Variants'

        conf_block.append(Keyvalues(hole.id, [
            Keyvalues('Footprint', hole.footprint_id),
            Keyvalues('error_shape', hole.error_shape),
            kv,
        ]))

    return conf_block
