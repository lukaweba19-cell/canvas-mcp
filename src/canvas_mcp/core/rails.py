"""Read a Rails project shipped as a course zip, without dumping its source.

Canvas courses on web development hand over a whole Rails app as one zip. A
model cannot read that, and the zip is megabytes of framework boilerplate, so
this module recognises the app and describes it the way a student would read it:
routes, controller actions, models with their associations and validations,
schema tables, views/layouts, migrations and the Rails/database version.

Everything is bounded: at most ``MAX_*`` items per section and
``MAX_SOURCE_BYTES`` per file, and each section is a short line per item. A
partial app is described as partial ("no db/schema.rb in the archive") instead
of pretending the rest exists.
"""

from __future__ import annotations

import re
import zipfile
from typing import Any

MAX_SOURCE_BYTES = 256 * 1024
MAX_ARCHIVE_ENTRIES = 5000
MAX_ROUTE_LINES = 300
MAX_CONTROLLERS = 80
MAX_MODELS = 80
MAX_TABLES = 80
MAX_VIEWS = 80
MAX_MIGRATIONS = 40

_SKIP_DIRS = ("vendor/", "node_modules/", "tmp/", "log/", "public/assets/", "storage/")

_VERB = ("get", "post", "put", "patch", "delete", "match")
_VERB_RE = re.compile(r"^(get|post|put|patch|delete|match)\b\s*(.*)$")
_RESOURCES_RE = re.compile(r"^resources\s+:([a-z_][\w]*)(.*)$")
_RESOURCE_RE = re.compile(r"^resource\s+:([a-z_][\w]*)(.*)$")
_NAMESPACE_RE = re.compile(r"^namespace\s+:([a-z_][\w]*)(.*)$")
_SCOPE_RE = re.compile(r"^scope\b(.*)$")
_TARGET_RE = re.compile(r"([\"'])(?P<controller>[a-z_][\w/]*)#(?P<action>[a-z_]\w*)\1")
_ONLY_RE = re.compile(r"\b(only|except):\s*\[([^\]]*)\]")
_ACTION_RE = re.compile(r"def\s+([a-z_]\w*[?!]?)")
_CLASS_RE = re.compile(r"class\s+([A-Z][\w:]*)\s*(?:<\s*([\w:]+))?")
_ASSOCIATION_RE = re.compile(
    r"^\s*(belongs_to|has_many|has_one|has_and_belongs_to_many)\s+:([a-z_]\w*)"
)
_VALIDATES_RE = re.compile(r"^\s*validate[s]?\s+(.+)$")
_SCHEMA_TABLE_RE = re.compile(r'^\s*create_table\s+[\'"]([a-z_]\w*)[\'"]')
_SCHEMA_COLUMN_RE = re.compile(
    r"^\s*t\.([a-z_]+)\s+[\'\"]([a-z_]\w*)[\'\"]|^\s*t\.(timestamps|references)\b"
)
_SCHEMA_INDEX_RE = re.compile(r"^\s*t\.index\s+(.+)$")
_VIEW_RE = re.compile(
    r"^app/views/(?P<directory>[^/]+)/(?P<template>[^/.]+)\.(?P<rest>.+)$"
)


# ---------------------------------------------------------------------------
# Archive access
# ---------------------------------------------------------------------------


def _is_rails_source(name: str, source: str) -> bool:
    if name.endswith("config/application.rb"):
        return "Rails::Application" in source
    if name.endswith("Gemfile"):
        return bool(re.search(r"gem\s+[\"']rails[\"']", source))
    if name.endswith("Gemfile.lock"):
        return bool(re.search(r"^\s{4}rails \(", source, re.MULTILINE))
    return False


def _read(archive: zipfile.ZipFile, name: str) -> str:
    if archive.getinfo(name).file_size > MAX_SOURCE_BYTES:
        return ""
    try:
        return archive.read(name).decode("utf-8", errors="replace")
    except (KeyError, RuntimeError, zipfile.BadZipFile):
        return ""


def _project_root(names: list[str]) -> str:
    """The prefix inside the zip that holds the app (zips often nest one folder)."""
    candidates = [
        name
        for name in names
        if name.endswith(("Gemfile", "Gemfile.lock", "config/application.rb"))
    ]
    if not candidates:
        return ""
    shortest = min(candidates, key=len)
    for marker in ("Gemfile.lock", "Gemfile", "config/application.rb"):
        if shortest.endswith(marker):
            return shortest[: -len(marker)]
    return ""


def describe_zip(
    archive: zipfile.ZipFile,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Describe a Rails project inside ``archive`` as bounded sections.

    Returns ``(sections, warnings)`` where each section is ``(label, text)``.
    """
    warnings: list[str] = []
    infos = [info for info in archive.infolist() if not info.is_dir()]
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        return [], [
            f"Archive contains {len(infos)} files, over the {MAX_ARCHIVE_ENTRIES} "
            "file limit. Upload the files you need directly."
        ]

    names = [info.filename for info in infos]
    if not any(
        name.endswith(("Gemfile", "Gemfile.lock", "config/application.rb"))
        for name in names
    ):
        source_names = [
            name for name in names if not name.startswith(_SKIP_DIRS)
        ]
        return [], [
            f"This zip is not a readable text format as-is: it holds "
            f"{len(source_names)} file(s) and no Rails project (no Gemfile with "
            "rails and no config/application.rb). Upload the file you need "
            "directly, or a zip of a Rails app."
        ]

    root = _project_root(names)
    files = {
        name[len(root) :] if root and name.startswith(root) else name: name
        for name in names
    }
    if not any(
        _is_rails_source(name, _read(archive, original))
        for name, original in files.items()
    ):
        return [], [
            "This zip contains a Rails-like layout but no Gemfile with rails and "
            "no config/application.rb, so it was not read as a Rails project."
        ]

    def present(relative: str) -> bool:
        return relative in files

    def read(relative: str) -> str:
        original = files.get(relative)
        return _read(archive, original) if original else ""

    controllers = sorted(
        name for name in files if name.startswith("app/controllers/") and name.endswith(".rb")
    )
    models = sorted(name for name in files if name.startswith("app/models/") and name.endswith(".rb"))
    views = sorted(name for name in files if name.startswith("app/views/") and not name.endswith("/"))
    migrations = sorted(name for name in files if name.startswith("db/migrate/") and name.endswith(".rb"))

    version = _rails_version(read("Gemfile.lock"), read("Gemfile"))
    adapter = _database_adapter(read("config/database.yml"))
    app_name = root.rstrip("/").rsplit("/", 1)[-1] or "Rails app (at the archive root)"

    summary = [
        f"App: {app_name}",
        f"Rails: {version or 'not stated in Gemfile.lock'}",
        f"Database adapter: {adapter or 'not stated in config/database.yml'}",
        f"Files: {len(files)}",
    ]
    available = []
    missing = []
    for label, condition in (
        ("config/routes.rb", present("config/routes.rb")),
        ("config/application.rb", present("config/application.rb")),
        ("db/schema.rb", present("db/schema.rb")),
        ("config/database.yml", present("config/database.yml")),
    ):
        (available if condition else missing).append(label)
    summary.append(
        f"Controllers: {len(controllers)} | Models: {len(models)} | "
        f"Views: {len(views)} | Migrations: {len(migrations)}"
    )
    if missing:
        summary.append("Not in the archive: " + ", ".join(missing))
    summary.append(
        "This is a description of the app, not its source. Read a single file "
        "by uploading it on its own."
    )

    sections: list[tuple[str, str]] = [("Rails project", "\n".join(summary))]

    if present("config/routes.rb"):
        sections.append(("Routes", parse_routes(read("config/routes.rb"))))
    if controllers:
        sections.append(("Controllers", describe_controllers(archive, files, controllers)))
    if models:
        sections.append(("Models", describe_models(archive, files, models)))
    if views:
        sections.append(("Views", describe_views(views)))
    if present("db/schema.rb"):
        sections.append(("Schema", describe_schema(read("db/schema.rb"))))
    if migrations:
        sections.append(("Migrations", describe_migrations(migrations)))
    if missing:
        sections.append(
            (
                "Partial app",
                "Only part of the Rails app was uploaded, so these are absent: "
                + ", ".join(missing)
                + ". The sections above describe what is actually here.",
            )
        )
    return sections, warnings


# ---------------------------------------------------------------------------
# routes.rb
# ---------------------------------------------------------------------------


def _plural_param(name: str) -> str:
    """A usable singular for a Rails resource name (covers the common cases)."""
    if name.endswith("ies"):
        return name[:-3] + "y"
    if name.endswith("s"):
        return name[:-1]
    return name


def _standard_routes(
    collection: str, name: str, controller: str, param: str
) -> list[tuple[str, str, str]]:
    return [
        ("GET", collection, f"{controller}#index"),
        ("GET", f"{collection}/new", f"{controller}#new"),
        ("POST", collection, f"{controller}#create"),
        ("GET", f"{collection}/:{param}", f"{controller}#show"),
        ("GET", f"{collection}/:{param}/edit", f"{controller}#edit"),
        ("PATCH", f"{collection}/:{param}", f"{controller}#update"),
        ("PUT", f"{collection}/:{param}", f"{controller}#update"),
        ("DELETE", f"{collection}/:{param}", f"{controller}#destroy"),
    ]


def _action_filter(options: str) -> tuple[set[str] | None, set[str] | None]:
    match = _ONLY_RE.search(options)
    if not match:
        return None, None
    names = {
        item.strip().strip(":").strip("\"'")
        for item in match.group(2).split(",")
        if item.strip()
    }
    return (names, None) if match.group(1) == "only" else (None, names)


def parse_routes(source: str, max_lines: int = MAX_ROUTE_LINES) -> str:
    """Turn routes.rb into ``VERB path -> controller#action`` lines.

    Handles ``resources`` (with nesting, ``only:``/``except:``), singular
    ``resource``, ``namespace``, ``scope``, ``member``/``collection`` blocks,
    direct verb routes and ``mount``.
    """
    routes: list[tuple[str, str, str]] = []
    stack: list[dict[str, Any]] = []

    def path_prefix() -> str:
        return "".join(context.get("path", "") for context in stack)

    def module_prefix() -> str:
        return "".join(context.get("module", "") for context in stack)

    def nearest_resource() -> dict[str, Any] | None:
        for context in reversed(stack):
            if context["kind"] in ("resources", "resource"):
                return context
        return None

    def add(method: str, path: str, target: str) -> None:
        if len(routes) < max_lines:
            routes.append((method, re.sub(r"/{2,}", "/", path) or "/", target))

    for raw in source.splitlines():
        # A '#' inside a quoted target is a controller separator, not a comment.
        line = (raw if _target_re_has_hash(raw) else raw.split("#", 1)[0]).strip()
        if not line:
            continue
        if line == "end" or line.startswith("end ") or line.startswith("end#"):
            if stack:
                stack.pop()
            continue

        resources = _RESOURCES_RE.match(line)
        if resources:
            name = resources.group(1)
            options = resources.group(2)
            parent = nearest_resource()
            base = path_prefix()
            controller = f"{module_prefix()}{name}"
            param = "id" if parent is None else f"{_plural_param(name)}_id"
            collection_path = f"{base}/{name}"
            only, except_ = _action_filter(options)
            for method, path, target in _standard_routes(
                collection_path, name, controller, param
            ):
                action = target.rsplit("#", 1)[1]
                if only is not None and action not in only:
                    continue
                if except_ is not None and action in except_:
                    continue
                add(method, path, target)
            if " do" in options or options.strip().endswith("do"):
                stack.append(
                    {
                        "kind": "resources",
                        # Children use the parent's foreign-key param
                        # (/courses/:course_id/lessons); the resource's own
                        # member routes use :id.
                        "path": f"{collection_path}/:{_plural_param(name)}_id",
                        "own": f"{collection_path}/:{param}",
                        "collection": collection_path,
                        "controller": controller,
                    }
                )
            continue

        resource = _RESOURCE_RE.match(line)
        if resource:
            name = resource.group(1)
            options = resource.group(2)
            base = path_prefix()
            controller = f"{module_prefix()}{name}"
            path = f"{base}/{name}"
            entries = [
                ("GET", path, f"{controller}#show"),
                ("GET", f"{path}/new", f"{controller}#new"),
                ("GET", f"{path}/edit", f"{controller}#edit"),
                ("POST", path, f"{controller}#create"),
                ("PATCH", path, f"{controller}#update"),
                ("PUT", path, f"{controller}#update"),
                ("DELETE", path, f"{controller}#destroy"),
            ]
            only, except_ = _action_filter(options)
            for method, entry_path, target in entries:
                action = target.rsplit("#", 1)[1]
                if only is not None and action not in only:
                    continue
                if except_ is not None and action in except_:
                    continue
                add(method, entry_path, target)
            if options.strip().endswith("do"):
                stack.append(
                    {
                        "kind": "resource",
                        "path": path,
                        "own": path,
                        "collection": path,
                        "controller": controller,
                    }
                )
            continue

        namespace = _NAMESPACE_RE.match(line)
        if namespace:
            name = namespace.group(1)
            stack.append(
                {"kind": "namespace", "path": f"/{name}", "module": f"{name}/"}
            )
            continue

        scope = _SCOPE_RE.match(line)
        if scope:
            options = scope.group(1)
            path = ""
            literal = re.search(r"[\"']([^\"']*)[\"']", options)
            if literal:
                path = literal.group(1)
            else:
                symbol = re.search(r":([a-z_]\w*)", options)
                if symbol:
                    path = f"/{symbol.group(1)}"
            module = re.search(r"module:\s*:([a-z_]\w*)", options)
            stack.append(
                {
                    "kind": "scope",
                    "path": path,
                    "module": f"{module.group(1)}/" if module else "",
                }
            )
            continue

        if re.match(r"^member\b.*do\s*$", line):
            stack.append({"kind": "member", "path": ""})
            continue
        if re.match(r"^collection\b.*do\s*$", line):
            stack.append({"kind": "collection", "path": ""})
            continue

        root = re.match(r"^root\b(.*)$", line)
        if root:
            target = _TARGET_RE.search(root.group(1))
            if target:
                add("GET", "/", f"{target.group('controller')}#{target.group('action')}")
            continue

        mounted = re.match(r"^mount\s+([\w:]+)(?:.*=>\s*[\"']([^\"']+)[\"'])?", line)
        if mounted:
            add("MOUNT", mounted.group(2) or "/", mounted.group(1))
            continue

        verb = _VERB_RE.match(line)
        if verb:
            method = verb.group(1).upper()
            rest = verb.group(2)
            target = _TARGET_RE.search(rest)
            literal = re.match(r"[\"']([^\"']+)[\"']", rest)
            symbol = re.match(r":([a-z_]\w*)", rest)
            resource_context = nearest_resource()
            in_member = any(context["kind"] == "member" for context in stack)
            in_collection = any(context["kind"] == "collection" for context in stack)

            if literal:
                path = literal.group(1)
            elif symbol and resource_context:
                path = symbol.group(1)
            else:
                path = ""

            if in_member and resource_context:
                path = f"{resource_context.get('own', resource_context['path'])}/{path}"
            elif in_collection and resource_context:
                path = f"{resource_context['collection']}/{path}"
            elif not path.startswith("/"):
                path = f"{path_prefix()}/{path}" if path else path_prefix()

            if target:
                controller_action = f"{target.group('controller')}#{target.group('action')}"
            elif (in_member or in_collection) and resource_context:
                controller_action = f"{resource_context['controller']}#{path.rsplit('/', 1)[-1]}"
            else:
                controller_action = "?"
            add(method, path or "/", controller_action)
            continue

        # Any other `something do` block (concerns, constraints) still needs its
        # `end` to pop, or every later route inherits its indentation.
        if re.search(r"\bdo\b\s*(\|[^|]*\|)?\s*$", line):
            stack.append({"kind": "block", "path": ""})

    lines = [f"{method} {path} -> {target}" for method, path, target in routes]
    if not lines:
        return "(no routes found in config/routes.rb)"
    if len(routes) >= max_lines:
        lines.append(f"... stopped after {max_lines} routes")
    return "\n".join(lines)


def _target_re_has_hash(raw: str) -> bool:
    """A ``#`` inside a quoted target is a controller separator, not a comment."""
    return bool(re.search(r"[\"'][a-z_][\w/]*#[a-z_]\w*[\"']", raw))


# ---------------------------------------------------------------------------
# controllers, models, views
# ---------------------------------------------------------------------------


def _controller_name(relative: str, source: str) -> str:
    match = _CLASS_RE.search(source)
    if match:
        return match.group(1)
    stem = relative.removeprefix("app/controllers/").removesuffix(".rb")
    return "".join(part.capitalize() for part in stem.split("/")) + "Controller"


def describe_controllers(
    archive: zipfile.ZipFile, files: dict[str, str], paths: list[str]
) -> str:
    lines: list[str] = []
    for relative in paths[:MAX_CONTROLLERS]:
        source = _read(archive, files[relative])
        name = _controller_name(relative, source)
        actions: list[str] = []
        private = False
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith(("private", "protected")):
                private = True
                continue
            match = _ACTION_RE.match(stripped)
            if match and not private:
                action = match.group(1)
                if action not in actions:
                    actions.append(action)
        detail = ", ".join(actions) if actions else "(no public actions)"
        lines.append(f"{name}: {detail}")
    if len(paths) > MAX_CONTROLLERS:
        lines.append(f"... {len(paths) - MAX_CONTROLLERS} more controllers")
    return "\n".join(lines)


def describe_models(
    archive: zipfile.ZipFile, files: dict[str, str], paths: list[str]
) -> str:
    lines: list[str] = []
    for relative in paths[:MAX_MODELS]:
        source = _read(archive, files[relative])
        match = _CLASS_RE.search(source)
        name = match.group(1) if match else relative.rsplit("/", 1)[-1].removesuffix(".rb").capitalize()
        associations: list[str] = []
        validations: list[str] = []
        for line in source.splitlines():
            association = _ASSOCIATION_RE.match(line)
            if association:
                associations.append(f"{association.group(1)} {association.group(2)}")
                continue
            validation = _VALIDATES_RE.match(line)
            if validation:
                clause = validation.group(1)
                fields = re.findall(r":([a-z_]\w*)", clause)
                options = re.findall(r"([a-z_]+):", clause)
                label = ", ".join(dict.fromkeys(fields))
                if options:
                    label += f" ({', '.join(dict.fromkeys(options))})"
                validations.append(f"validates {label}")
        detail = "; ".join(associations + validations) or "(no associations or validations)"
        lines.append(f"{name}: {detail}")
    if len(paths) > MAX_MODELS:
        lines.append(f"... {len(paths) - MAX_MODELS} more models")
    return "\n".join(lines)


def describe_views(paths: list[str]) -> str:
    grouped: dict[str, list[str]] = {}
    for relative in paths:
        match = _VIEW_RE.match(relative)
        if not match:
            continue
        directory = match.group("directory")
        template = match.group("template")
        grouped.setdefault(directory, [])
        if template not in grouped[directory]:
            grouped[directory].append(template)
    lines = [f"{directory}: {', '.join(sorted(templates))}" for directory, templates in sorted(grouped.items())]
    if len(lines) > MAX_VIEWS:
        lines = lines[:MAX_VIEWS] + [f"... {len(lines) - MAX_VIEWS} more view folders"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# schema.rb, migrations, config
# ---------------------------------------------------------------------------


def describe_schema(source: str, max_tables: int = MAX_TABLES) -> str:
    tables: list[str] = []
    current: str | None = None
    columns: list[str] = []
    indexes: list[str] = []

    def flush() -> None:
        if current is None:
            return
        detail = ", ".join(columns) if columns else "(no columns)"
        if indexes:
            detail += "; indexes: " + "; ".join(indexes)
        tables.append(f"{current}: {detail}")

    for line in source.splitlines():
        table = _SCHEMA_TABLE_RE.match(line)
        if table:
            flush()
            current = table.group(1)
            columns, indexes = [], []
            continue
        if current is None:
            continue
        if line.strip() == "end":
            flush()
            current = None
            continue
        column = _SCHEMA_COLUMN_RE.match(line)
        if column:
            if column.group(3) == "timestamps":
                columns.append("timestamps")
            elif column.group(3) == "references":
                reference = re.search(r":([a-z_]\w*)", line)
                columns.append(f"{reference.group(1) if reference else 'reference'}:references")
            else:
                columns.append(f"{column.group(2)}:{column.group(1)}")
            continue
        index = _SCHEMA_INDEX_RE.match(line)
        if index:
            unique = " unique" if "unique: true" in line else ""
            name = re.search(r'name:\s*["\']([^"\']+)["\']', index.group(1))
            label = name.group(1) if name else index.group(1).strip()
            indexes.append(f"{label}{unique}")
    flush()
    if not tables:
        return "(db/schema.rb has no create_table blocks)"
    if len(tables) > max_tables:
        tables = tables[:max_tables] + [f"... {len(tables) - max_tables} more tables"]
    return "\n".join(tables)


def describe_migrations(paths: list[str]) -> str:
    shown = paths[:MAX_MIGRATIONS]
    lines = [path.removeprefix("db/migrate/") for path in shown]
    if len(paths) > MAX_MIGRATIONS:
        lines.append(f"... {len(paths) - MAX_MIGRATIONS} more migrations")
    lines.insert(0, f"{len(paths)} migration(s):")
    return "\n".join(lines)


def _rails_version(lock: str, gemfile: str) -> str | None:
    match = re.search(r"^\s{4}rails \(([^)]+)\)", lock, re.MULTILINE)
    if match:
        return match.group(1).strip()
    match = re.search(r"gem\s+[\"']rails[\"'],\s*[\"']([^\"']+)[\"']", gemfile)
    return match.group(1).strip() if match else None


def _database_adapter(database_yml: str) -> str | None:
    match = re.search(r"^\s*adapter:\s*([a-z0-9_]+)", database_yml, re.MULTILINE)
    return match.group(1) if match else None
