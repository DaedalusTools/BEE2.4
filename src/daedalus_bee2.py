"""Headless services for the Daedalus BEE2 fork.

Configure once before importing upstream modules. Package sessions are serial,
own their filesystems, and never publish themselves to packages.LOADED.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
import copy
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from threading import Lock
from types import MappingProxyType
from typing import TYPE_CHECKING, Awaitable, Callable, Mapping
from zipfile import ZipFile
import sys

# FIXME: most functions in this file are too long and should be split into helpers

if TYPE_CHECKING:   # FIXME: why do we have these TYPE_CHECKING branches?
    from app import img
    import config
    import corridor
    import editoritems
    import packages
    import user_errors
    from srctools import Keyvalues
    from srctools.filesys import FileSystem
    from srctools.game import Game
    from srctools.vmf import VMF


@dataclass(frozen=True)
class BackendConfiguration:
    config_root: Path
    versions: Mapping[str, str]


_configuration: BackendConfiguration | None = None
_session_active = False
_session_lock = Lock()
_package_set: packages.PackagesSet | None = None
_compiler_consumed = False
_progress: Callable[[str, int, int], None] | None = None
_archives: list[ZipFile] = []


def configure(config_root: Path, versions: Mapping[str, str]) -> None:
    global _configuration
    if not config_root.is_absolute():
        raise ValueError("BEE2 configuration root must be absolute.")
    desired = BackendConfiguration(config_root.resolve(), MappingProxyType(dict(versions)))
    if _configuration is not None:
        if _configuration != desired:
            raise RuntimeError("BEE2 is already configured for another backend root.")
        return
    if "utils" in sys.modules:
        raise RuntimeError("Configure the BEE2 backend before importing upstream modules.")
    _configuration = desired


def is_configured() -> bool:
    return _configuration is not None


def configuration() -> BackendConfiguration:
    if _configuration is None:
        raise RuntimeError("The BEE2 backend has not been configured.")
    return _configuration


def isolate_package_logging[**P, R](operation: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    if not is_configured():
        return operation

    @wraps(operation)
    async def isolated(*args: P.args, **kwargs: P.kwargs) -> R:
        from srctools.logger import CTX_STACK
        # srctools 2.6 stores a mutable list in a ContextVar inherited by child tasks.
        token = CTX_STACK.set(list(CTX_STACK.get(())))
        try:
            return await operation(*args, **kwargs)
        finally:
            CTX_STACK.reset(token)

    return isolated


class ProgressStage:
    def __init__(self, name: str) -> None:
        self.name = name
        self.current = 0
        self.total = 0

    async def set_length(self, total: int) -> None:
        import trio
        self.total = total
        await trio.lowlevel.checkpoint()
        self._emit()

    async def step(self, item: object = None) -> None:
        import trio
        self.current += 1
        await trio.lowlevel.checkpoint()
        self._emit()

    def _emit(self) -> None:
        if _progress is not None:
            _progress(self.name, self.current, self.total)


PACKAGE_PROGRESS = ProgressStage("packages")
OBJECT_PROGRESS = ProgressStage("objects")


@dataclass(frozen=True)
class ItemExport:
    editors: tuple[editoritems.Item, ...]
    compiler_config: Keyvalues
    warnings: tuple[str, ...]
    item_versions: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class CleanProfileConfiguration:
    """In-memory items, corridors and widget defaults; not a full compiler export."""
    editors: tuple[editoritems.Item, ...]
    compiler_config: Keyvalues
    options: config.Config
    corridors: corridor.ExportedConf
    warnings: tuple[str, ...]
    package_set: packages.PackagesSet
    item_versions: tuple[tuple[str, str], ...]
    signage_icons: tuple[tuple[int, img.Handle], ...]
    renderables: tuple[tuple[editoritems.RenderableType, editoritems.Renderable], ...]


class CompilerInputError(ValueError):
    """A BEE2 map/configuration error, not alternate error-map success."""
    def __init__(self, details: user_errors.ErrorInfo) -> None:
        super().__init__(str(details.message))
        self.details = details


@contextmanager
def compiler_input_errors():
    """Translate upstream BaseException diagnostics without swallowing cancellation."""
    import user_errors

    def translate(error: BaseException) -> BaseException:
        if isinstance(error, user_errors.UserError):
            converted = CompilerInputError(error.info)
            converted.__cause__ = error
            return converted
        if isinstance(error, BaseExceptionGroup):
            return error.derive([translate(child) for child in error.exceptions])
        return error

    try:
        yield
    except user_errors.UserError as error:
        raise CompilerInputError(error.info) from error
    except BaseExceptionGroup as error:
        if error.subgroup(user_errors.UserError) is None:
            raise
        raise translate(error) from error


async def preprocess_peti_map(
    packset: packages.PackagesSet,
    profile: CleanProfileConfiguration,
    vmf: VMF,
    game: Game,
    material_root: Path,
    *,
    filesystem: FileSystem,
) -> None:
    """Consume one caller-owned map in this existing backend command process.

    Compiler globals are single-job upstream state. Even failure consumes this
    process's compiler lifetime; package-only sessions remain reusable.
    """
    global _compiler_consumed
    if not _session_active or packset is not _package_set or profile.package_set is not packset:
        raise RuntimeError("Preprocessing requires its owning package session and profile.")
    if not material_root.is_absolute():
        raise ValueError("Generated materials require an absolute Daedalus-owned root.")
    if _compiler_consumed:
        raise RuntimeError("BEE2 preprocessing is limited to one job per backend command process.")
    _compiler_consumed = True
    import packages
    import trio
    import vbsp
    from fizzler_materials import generate_fizzler_side_materials
    from quote_pack import QuoteInfo
    from srctools import Keyvalues

    await trio.lowlevel.checkpoint()
    await generate_fizzler_side_materials(profile.compiler_config, material_root)
    vbsp.conditions.import_conditions()
    packlists = Keyvalues("PackList", [
        Keyvalues(pack.id, [Keyvalues("File", name) for name in pack.files])
        for pack in packset.all_obj(packages.PackList)
    ])
    with compiler_input_errors():
        with template_session(packset):
            indicators, editors, corridors = vbsp.apply_settings(
                profile.compiler_config.copy(), packlists, list(profile.editors),
                profile.corridors, profile.options,
            )
            collisions, map_info, used_instances = vbsp.analyse_peti_map(vmf, editors, corridors)
            await trio.lowlevel.checkpoint()
            await vbsp.transform_peti_map(
                vmf, game, collisions, map_info, used_instances, indicators, QuoteInfo.blank(),
                material_folder=material_root,
                filesystem=filesystem,
            )


@contextmanager
def template_session(packset: packages.PackagesSet):
    """Keep lazy template reads within the owning package-filesystem lifetime."""
    require_package_session(packset)
    from precomp.template_brush import UnparsedTemplate, template_sources

    sources = [
        UnparsedTemplate(
            template_id.upper(), str(packset.packages[reference.package].path),
            reference.path, packset.packages[reference.package].fsys,
        )
        for template_id, reference in packset.templates.items()
    ]
    with template_sources(sources):
        yield


def require_package_session(packset: packages.PackagesSet) -> None:
    if not _session_active or packset is not _package_set:
        raise RuntimeError("This operation requires its owning package session.")


async def assemble_clean_profile_configuration(
    packset: packages.PackagesSet,
    versions: Mapping[str, str] | None = None,
    *,
    widget_values: Mapping[str, str | Mapping[str, str]] | None = None,
) -> CleanProfileConfiguration:
    import config
    import packages
    from exporting.corridors import assemble_corridors
    from exporting.barrier_hole import assemble_barrier_holes
    from exporting.elevator import assemble_elevator
    from exporting.skybox import assemble_skybox
    from exporting.signage import assemble_signage
    from exporting.player import assemble_player_models
    from exporting.stylevar import assemble_stylevars
    from exporting.widget_conf import apply_widget_defaults, apply_widget_values
    from packages.stylevar import BUILTIN_STYLE_VARS
    from config.signage import Layout
    import utils

    items = await assemble_clean_items(packset, versions)
    style = packset.obj_by_id(packages.Style, packages.CLEAN_STYLE)
    editors = list(items.editors)
    options = await apply_widget_defaults(packset, config.APP.get_full_conf(config.COMPILER))
    if widget_values is not None:
        options = apply_widget_values(packset, options, widget_values)
    options = await assemble_player_models(packset, options)
    corridors, options = await assemble_corridors(
        packset, style,
        editors, options, items.compiler_config,
    )
    stylevars = {
        variable.id: variable.default
        for variable in [*packset.all_obj(packages.StyleVar), *BUILTIN_STYLE_VARS]
        if variable.applies_to_style(style)
    }
    items.compiler_config.append(await assemble_stylevars(packset, stylevars, options))
    holes = await assemble_barrier_holes(packset)
    if holes:
        items.compiler_config.append(holes)
    skybox_id = sorted(style.suggested[packages.Skybox])[0]
    if skybox_id != utils.ID_NONE:
        await assemble_skybox(
            packset.obj_by_id(packages.Skybox, skybox_id), items.compiler_config,
        )
    elevator_id = sorted(style.suggested[packages.Elevator])[0]
    assemble_elevator(
        style, None if elevator_id == utils.ID_NONE else packset.obj_by_id(packages.Elevator, elevator_id),
        items.compiler_config,
    )
    signage, signage_icons = await assemble_signage(
        packset, style, [(str(timer), sign_id) for timer, sign_id in Layout().signs.items() if sign_id],
    )
    items.compiler_config.append(signage)
    return CleanProfileConfiguration(
        tuple(editors), items.compiler_config, options, corridors, items.warnings, packset, items.item_versions,
        tuple(signage_icons.items()),
        tuple(copy.deepcopy(style.renderables).items()),
    )


async def assemble_clean_items(
    packset: packages.PackagesSet,
    versions: Mapping[str, str] | None = None,
) -> ItemExport:
    """Assemble item/style definitions, not a complete game/compile export."""
    import packages
    from config.item_defaults import ItemDefault
    from exporting.items import assemble_items

    if not _session_active or packset is not _package_set:
        raise RuntimeError("Item assembly requires its owning package session.")
    versions = versions or {}
    items = sorted(packset.all_obj(packages.Item), key=lambda item: item.id)
    unknown = versions.keys() - {item.id for item in items}
    if unknown:
        raise ValueError("Unknown BEE2 item selection: " + ", ".join(sorted(unknown)))
    defaults = {}
    selections = []
    palette = {}
    position = 0
    for item in items:
        version_id = versions.get(item.id, item.def_ver.id)
        if version_id not in item.versions:
            raise ValueError(f'Unknown BEE2 version "{version_id}" for "{item.id}".')
        defaults[item.id] = ItemDefault(version=version_id)
        selections.append((item.id, version_id))
        positions = {}
        for subtype in item.visual_subtypes:
            positions[subtype] = position % 4, position // 4
            position += 1
        palette[item.id.casefold()] = positions
    style = packset.obj_by_id(packages.Style, packages.CLEAN_STYLE)
    editors = copy.deepcopy(style.items)
    compiler_config = await style.config()
    warnings = []
    await assemble_items(
        packset, style, palette, defaults, editors, compiler_config,
        lambda package_id, warning: warnings.append(f"{package_id}: {warning}"),
    )
    return ItemExport(tuple(editors), compiler_config, tuple(warnings), tuple(selections))


def own_filesystem(filesystem: FileSystem) -> None:
    from srctools.filesys import ZipFileSystem
    if isinstance(filesystem, ZipFileSystem):
        _archives.append(filesystem.zip)


@asynccontextmanager
async def package_session(
    package_roots: list[Path],
    *,
    progress: Callable[[str, int, int], None] | None = None,
):
    """Resolve upstream packages; yielded objects are valid only in this scope."""
    global _session_active, _progress, _package_set
    configuration()
    if not package_roots or any(not root.is_absolute() for root in package_roots):
        raise ValueError("Provide absolute BEE2 package roots.")
    if not _session_lock.acquire(blocking=False):
        raise RuntimeError("Concurrent BEE2 package sessions are not supported.")
    _session_active = True
    try:
        import trio
        import config
        import packages
        from app.errors import ErrorUI

        packset = packages.PackagesSet()
        _package_set = packset
        diagnostics: list[dict[str, object]] = []
        saved = [(spec, spec.get_full_conf()) for spec in (config.APP, config.PALETTE, config.COMPILER)]
        for spec, _ in saved:
            spec.replace_conf(config.Config({}))
        packages.PACK_CONFIG.clear()
        _progress = progress
        for stage in (PACKAGE_PROGRESS, OBJECT_PROGRESS):
            stage.current = stage.total = 0

        async def collect_errors(title, description, errors) -> None:
            diagnostics.extend({"message": str(error.message), "fatal": error.fatal} for error in errors)
            await trio.lowlevel.checkpoint()

        try:
            with ErrorUI.install_handler(collect_errors):
                async with ErrorUI() as errors:
                    await packages._load_packages(packset, package_roots, errors)
                    async with trio.open_nursery() as nursery:
                        nursery.start_soon(packages._load_objects, packset, errors)
                        nursery.start_soon(packages._load_templates, packset)
                if errors.result.failed or not packset.can_export():
                    raise ValueError("BEE2 package resolution failed: " + "; ".join(
                        str(diagnostic["message"]) for diagnostic in diagnostics
                    ))
            yield packset, tuple(diagnostics)
        finally:
            _progress = None
            for spec, original in saved:
                spec.replace_conf(original)
            packages.PACK_CONFIG.clear()
            for archive in _archives:
                archive.close()
            _archives.clear()
    finally:
        _package_set = None
        _session_active = False
        _session_lock.release()
