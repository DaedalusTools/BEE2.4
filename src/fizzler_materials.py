"""Headless extraction of pinned BEE2 src/exporting/fizzler.py material generation."""
from pathlib import Path

from srctools import Keyvalues
from srctools.math import FrozenVec, format_float
import trio


# FIXME: what is this file? This seems like a workaround instead of fixing the real issue at its root

FIZZLER_EDGE_MAT = '''\
SolidEnergy
{{
$basetexture "sprites/laserbeam"
$flowmap "effects/fizzler_flow"
$flowbounds "BEE2/fizz/fizz_side"
$flow_noise_texture "effects/fizzler_noise"
$additive 1
$translucent 1
$decal 1
$flow_color "[{}]"
$flow_vortex_color "[{}]"
'''

FIZZLER_EDGE_MAT_PROXY = '''\
$offset "[0 0]"
Proxies
{
FizzlerVortex
{
}
MaterialModify
{
}
}
}
'''


async def generate_fizzler_side_materials(configuration: Keyvalues, material_root: Path) -> set[Path]:
    """Write upstream edge materials below an absolute caller-owned materials root."""
    if not material_root.is_absolute():
        raise ValueError("Generated materials require an absolute caller-owned root.")
    colors: dict[FrozenVec, tuple[float, str]] = {}
    for brush in configuration.find_all("Fizzlers", "Fizzler", "Brush"):
        color = brush["Side_color", ""]
        if color:
            colors[FrozenVec.from_str(color)] = (brush.float("side_alpha", 1.0), brush["side_vortex", color])
    destination = trio.Path(material_root / "bee2" / "fizz_sides")
    if colors:
        await destination.mkdir(parents=True, exist_ok=True)
    resources: set[Path] = set()

    async def write_material(color: FrozenVec, alpha: float, vortex: str) -> None:
        red, green, blue = (round(component * 255) for component in color)
        path = destination / f"side_color_{red:02X}{green:02X}{blue:02X}.vmt"
        async with await path.open("w", newline="\r\n") as output:
            await output.write(FIZZLER_EDGE_MAT.format(color, vortex))
            if alpha != 1:
                await output.write(f"$outputintensity {format_float(alpha)}\n")
            await output.write(FIZZLER_EDGE_MAT_PROXY)
        resources.add(Path(path))

    async with trio.open_nursery() as nursery:
        for color, (alpha, vortex) in colors.items():
            nursery.start_soon(write_material, color, alpha, vortex)
    return resources
