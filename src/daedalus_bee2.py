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
from types import MappingProxyType
from typing import TYPE_CHECKING, Awaitable, Callable, Mapping, cast

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
_progress: Callable[[str, int, int], None] | None = None


def configure(config_root: Path, versions: Mapping[str, str]) -> None:
    """Choose this command process's configuration before importing upstream modules."""
    global _configuration
    if _configuration is None:
        _configuration = BackendConfiguration(config_root.resolve(), MappingProxyType(dict(versions)))


def is_configured() -> bool:
    return _configuration is not None


def configuration() -> BackendConfiguration:
    """Return the configuration installed at command startup."""
    return cast(BackendConfiguration, _configuration)


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


@dataclass(frozen=True)
class CleanContentSelection:
    """Resolved definitions, options and artwork; no executable compiler configuration."""
    editors: tuple[editoritems.Item, ...]
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


async def resolve_clean_content(
    packset: packages.PackagesSet,
    versions: Mapping[str, str] | None = None,
    *,
    widget_values: Mapping[str, str | Mapping[str, str]] | None = None,
) -> CleanContentSelection:
    """Resolve catalog content without reading item/style/corridor conditions."""
    import config
    import packages
    from config.signage import Layout
    from exporting.corridors import resolve_corridor_content
    from exporting.items import resolve_item_definitions, TRANS_OLD_STYLE_DEF
    from exporting.player import assemble_player_models
    from exporting.signage import resolve_signage_icons
    from exporting.widget_conf import apply_widget_defaults, apply_widget_values

    style, defaults, palette, selections = _clean_item_choices(packset, versions)
    editors = copy.deepcopy(style.items)
    style_items = {editor.id: editor for editor in editors}
    warnings = []
    for item_id, _ in selections:
        item = packset.obj_by_id(packages.Item, item_id)
        variant, definitions = resolve_item_definitions(item, palette, style.id, defaults[item_id])
        for definition in definitions:
            if definition.id in style_items:
                warnings.append(f"{variant.pak_id}: {TRANS_OLD_STYLE_DEF.format(item=definition.id)}")
                editors.remove(style_items.pop(definition.id))
            editors.append(definition)
    options = await apply_widget_defaults(packset, config.APP.get_full_conf(config.COMPILER))
    if widget_values is not None:
        options = apply_widget_values(packset, options, widget_values)
    options = await assemble_player_models(packset, options)
    corridors, options = await resolve_corridor_content(packset, style, editors, options)
    icons = await resolve_signage_icons(
        packset, style, [(str(timer), sign_id) for timer, sign_id in Layout().signs.items() if sign_id],
    )
    return CleanContentSelection(
        tuple(editors), options, corridors, tuple(warnings), packset, tuple(selections),
        tuple(icons.items()), tuple(copy.deepcopy(style.renderables).items()),
    )


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
    from exporting.items import assemble_items

    style, defaults, palette, selections = _clean_item_choices(packset, versions)
    editors = copy.deepcopy(style.items)
    compiler_config = await style.config()
    warnings = []
    await assemble_items(
        packset, style, palette, defaults, editors, compiler_config,
        lambda package_id, warning: warnings.append(f"{package_id}: {warning}"),
    )
    return ItemExport(tuple(editors), compiler_config, tuple(warnings), tuple(selections))


def _clean_item_choices(packset, versions):
    """Choose definitions once; do not load their lazy compiler configuration."""
    import packages
    from config.item_defaults import ItemDefault

    versions = versions or {}
    items = sorted(packset.all_obj(packages.Item), key=lambda item: item.id)
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
    return style, defaults, palette, selections


@asynccontextmanager
async def package_session(
    package_roots: list[Path],
    *,
    progress: Callable[[str, int, int], None] | None = None,
):
    """Resolve upstream packages; yielded objects are valid only in this scope."""
    global _progress
    import trio
    import config
    import packages
    from app.errors import ErrorUI

    packset = packages.PackagesSet()
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
                await packages._load_packages(packset, package_roots, errors, loose_only=True)
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
