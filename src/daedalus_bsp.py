"""Own BEE2's BSP stages around the existing backend's Valve lighting call."""
from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from srctools.bsp import BSP
    from srctools.filesys import FileSystem, FileSystemChain
    from srctools.game import Game
    from srctools.packlist import PackList


_claim_lock = Lock()
_consumed = False


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
    _active: bool = field(default=True, init=False)
    _attempted: bool = field(default=False, init=False)
    _finished: bool = field(default=False, init=False)

    def finish(self) -> BSP:
        """Reopen after lighting and pack once; never reuse an earlier BSP object."""
        if not self._active or self._attempted:
            raise RuntimeError("The BSP lighting stage is closed or already finalized.")
        self._attempted = True
        import vrad
        result = vrad.finish_bsp(
            self.bsp_path, self._packlist,
            whitelist=self._whitelist, blacklist=self._blacklist, ignore_vpk=False,
        )
        self._finished = True
        return result


@asynccontextmanager
async def lighting_session(
    bsp_path: Path,
    game: Game,
    filesystem: FileSystemChain,
    custom_filesystems: set[FileSystem],
    *,
    required_resources: tuple[str, ...] = (),
) -> AsyncIterator[LightingStage]:
    """Transform/save before yielding; caller runs Valve VRAD then calls finish()."""
    global _consumed
    import daedalus_bee2
    daedalus_bee2.configuration()
    if not bsp_path.is_absolute():
        raise ValueError("The BSP path must be absolute.")
    if not bsp_path.is_file():
        raise FileNotFoundError(bsp_path)
    if any(not any(custom is system for system, _ in filesystem.systems)
           for custom in custom_filesystems):
        raise ValueError("Custom packing filesystems must be mounted by identity.")
    with _claim_lock:
        if _consumed:
            raise RuntimeError("BSP transformation is limited to one job per backend command process.")
        _consumed = True
    import vrad
    from srctools.bsp import BSP
    from chamber_editor.engine_dependency_packlist import EngineDependencyPackList

    bsp = BSP(bsp_path)
    stock_filesystems = {system for system, _ in filesystem.systems} - custom_filesystems
    packlist_factory = partial(EngineDependencyPackList, stock_filesystems=stock_filesystems)
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
            try:
                bsp.save()
                yield stage
                if not stage._finished:
                    raise RuntimeError("Finalize the BSP after Valve lighting before closing this stage.")
            finally:
                stage._active = False
