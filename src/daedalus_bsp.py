"""Own BEE2's BSP stages around the existing backend's Valve lighting call."""
from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from srctools.bsp import BSP
    from srctools.filesys import FileSystem, FileSystemChain
    from srctools.game import Game
    from srctools.packlist import PackList


class MissingCompilerResources(ValueError):
    def __init__(self, filenames: list[str]) -> None:
        self.filenames = tuple(sorted(filenames))
        super().__init__("Required compiler resources are missing: " + ", ".join(self.filenames))


@dataclass
class LightingStage:
    bsp_path: Path
    _packlist: PackList = field(repr=False)
    _whitelist: set[FileSystem] = field(repr=False)
    _blacklist: set[FileSystem] = field(repr=False)

    def finish(self) -> BSP:
        """Reopen after lighting and pack once; never reuse an earlier BSP object."""
        import vrad
        return vrad.finish_bsp(
            self.bsp_path, self._packlist,
            whitelist=self._whitelist, blacklist=self._blacklist, ignore_vpk=False,
        )


@asynccontextmanager
async def lighting_session(
    bsp_path: Path,
    game: Game,
    filesystem: FileSystemChain,
    custom_filesystems: set[FileSystem],
    *,
    packlist_factory: Callable[[FileSystemChain], PackList],
    required_resources: tuple[str, ...] = (),
) -> AsyncIterator[LightingStage]:
    """Transform/save before yielding; caller runs Valve VRAD then calls finish()."""
    import daedalus_bee2
    import vrad
    from srctools.bsp import BSP

    bsp = BSP(bsp_path)
    with daedalus_bee2.compiler_input_errors():
        async with vrad.bsp_transform_session(
            bsp, game, filesystem, packlist_factory=packlist_factory,
        ) as (packlist, stage_filesystem, archive):
            if required_resources:
                import trio
                for filename in required_resources:
                    packlist.pack_file(filename)
                await trio.to_thread.run_sync(packlist.eval_dependencies)
            missing = [
                file.filename for file in packlist
                if not file.optional and file.data is None and file.filename not in stage_filesystem
            ]
            if missing:
                raise MissingCompilerResources(missing)
            blacklist = {system for system, _ in stage_filesystem.systems} - custom_filesystems
            blacklist.add(archive)
            stage = LightingStage(bsp_path, packlist, set(custom_filesystems), blacklist)
            bsp.save()
            yield stage
