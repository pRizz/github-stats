#!/usr/bin/python3

import html
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional
from xml.etree import ElementTree as ET


SVG_NS = "http://www.w3.org/2000/svg"
ICON_DIR = Path(__file__).resolve().parent / "assets" / "language-icons" / "devicon"

LANGUAGE_ICON_FILES: Dict[str, str] = {
    "apex": "apex-original.svg",
    "apl": "apl-original.svg",
    "arduino": "arduino-original.svg",
    "astro": "astro-original.svg",
    "awk": "awk-original.svg",
    "ballerina": "ballerina-original.svg",
    "bash": "bash-original.svg",
    "c": "c-original.svg",
    "ceylon": "ceylon-original.svg",
    "clojure": "clojure-original.svg",
    "clojurescript": "clojurescript-original.svg",
    "cmake": "cmake-original.svg",
    "cobol": "cobol-original.svg",
    "coffeescript": "coffeescript-original.svg",
    "crystal": "crystal-original.svg",
    "csharp": "csharp-original.svg",
    "cplusplus": "cplusplus-original.svg",
    "css3": "css3-original.svg",
    "dart": "dart-original.svg",
    "delphi": "delphi-original.svg",
    "docker": "docker-original.svg",
    "elixir": "elixir-original.svg",
    "elm": "elm-original.svg",
    "erlang": "erlang-original.svg",
    "fortran": "fortran-original.svg",
    "fsharp": "fsharp-original.svg",
    "gleam": "gleam-original.svg",
    "go": "go-original.svg",
    "gradle": "gradle-original.svg",
    "graphql": "graphql-plain.svg",
    "groovy": "groovy-original.svg",
    "handlebars": "handlebars-original.svg",
    "haskell": "haskell-original.svg",
    "haxe": "haxe-original.svg",
    "html5": "html5-original.svg",
    "java": "java-original.svg",
    "javascript": "javascript-original.svg",
    "json": "json-original.svg",
    "julia": "julia-original.svg",
    "jupyter": "jupyter-original.svg",
    "kotlin": "kotlin-original.svg",
    "latex": "latex-original.svg",
    "less": "less-plain-wordmark.svg",
    "lua": "lua-original.svg",
    "markdown": "markdown-original.svg",
    "matlab": "matlab-original.svg",
    "microsoftsqlserver": "microsoftsqlserver-original.svg",
    "nim": "nim-original.svg",
    "nixos": "nixos-original.svg",
    "objectivec": "objectivec-plain.svg",
    "ocaml": "ocaml-original.svg",
    "perl": "perl-original.svg",
    "php": "php-original.svg",
    "postgresql": "postgresql-original.svg",
    "powershell": "powershell-original.svg",
    "processing": "processing-original.svg",
    "prolog": "prolog-original.svg",
    "pug": "pug-original.svg",
    "purescript": "purescript-original.svg",
    "python": "python-original.svg",
    "r": "r-original.svg",
    "racket": "racket-original.svg",
    "ruby": "ruby-original.svg",
    "rust": "rust-original.svg",
    "sass": "sass-original.svg",
    "scala": "scala-original.svg",
    "solidity": "solidity-original.svg",
    "stylus": "stylus-original.svg",
    "svelte": "svelte-original.svg",
    "swift": "swift-original.svg",
    "typescript": "typescript-original.svg",
    "vim": "vim-original.svg",
    "visualbasic": "visualbasic-original.svg",
    "vuejs": "vuejs-original.svg",
    "wasm": "wasm-original.svg",
    "xml": "xml-original.svg",
    "yaml": "yaml-original.svg",
    "zig": "zig-original.svg",
}

LANGUAGE_ICON_SLUGS: Dict[str, str] = {
    "apex": "apex",
    "apl": "apl",
    "arduino": "arduino",
    "astro": "astro",
    "awk": "awk",
    "ballerina": "ballerina",
    "bash": "bash",
    "bibtex": "latex",
    "bibtex style": "latex",
    "c": "c",
    "c#": "csharp",
    "c++": "cplusplus",
    "ceylon": "ceylon",
    "clojure": "clojure",
    "clojurescript": "clojurescript",
    "cmake": "cmake",
    "cobol": "cobol",
    "coffeescript": "coffeescript",
    "crystal": "crystal",
    "css": "css3",
    "css3": "css3",
    "dart": "dart",
    "delphi": "delphi",
    "dockerfile": "docker",
    "elixir": "elixir",
    "elm": "elm",
    "erlang": "erlang",
    "f#": "fsharp",
    "fortran": "fortran",
    "gleam": "gleam",
    "go": "go",
    "gradle": "gradle",
    "graphql": "graphql",
    "groovy": "groovy",
    "handlebars": "handlebars",
    "haskell": "haskell",
    "haxe": "haxe",
    "html": "html5",
    "html+ecr": "html5",
    "html+eex": "html5",
    "html+erb": "html5",
    "html+php": "html5",
    "html+razor": "html5",
    "html5": "html5",
    "java": "java",
    "java server pages": "java",
    "javascript": "javascript",
    "json": "json",
    "json with comments": "json",
    "json5": "json",
    "jsx": "javascript",
    "julia": "julia",
    "jupyter notebook": "jupyter",
    "kotlin": "kotlin",
    "latex": "latex",
    "less": "less",
    "lua": "lua",
    "markdown": "markdown",
    "matlab": "matlab",
    "mdx": "markdown",
    "nix": "nixos",
    "nim": "nim",
    "objective-c": "objectivec",
    "object pascal": "delphi",
    "ocaml": "ocaml",
    "perl": "perl",
    "php": "php",
    "plpgsql": "postgresql",
    "powershell": "powershell",
    "processing": "processing",
    "prolog": "prolog",
    "pug": "pug",
    "purescript": "purescript",
    "python": "python",
    "r": "r",
    "racket": "racket",
    "rmarkdown": "markdown",
    "ruby": "ruby",
    "rust": "rust",
    "sass": "sass",
    "scala": "scala",
    "scss": "sass",
    "shell": "bash",
    "solidity": "solidity",
    "stylus": "stylus",
    "svelte": "svelte",
    "swift": "swift",
    "tex": "latex",
    "tsql": "microsoftsqlserver",
    "tsx": "typescript",
    "typescript": "typescript",
    "vba": "visualbasic",
    "vbscript": "visualbasic",
    "vim help file": "vim",
    "vim script": "vim",
    "vim snippet": "vim",
    "visual basic .net": "visualbasic",
    "vue": "vuejs",
    "vue.js": "vuejs",
    "webassembly": "wasm",
    "xml": "xml",
    "xml property list": "xml",
    "xslt": "xml",
    "yaml": "yaml",
    "zig": "zig",
}

ALLOWED_TAGS = {
    "svg",
    "g",
    "path",
    "circle",
    "ellipse",
    "rect",
    "polygon",
    "polyline",
    "line",
    "defs",
    "clipPath",
    "linearGradient",
    "radialGradient",
    "stop",
    "use",
}

ALLOWED_ATTRIBUTES = {
    "baseProfile",
    "clip-path",
    "clip-rule",
    "color",
    "cx",
    "cy",
    "d",
    "data-name",
    "fill",
    "fill-opacity",
    "fill-rule",
    "font-family",
    "font-weight",
    "gradientTransform",
    "gradientUnits",
    "height",
    "href",
    "id",
    "offset",
    "opacity",
    "overflow",
    "points",
    "r",
    "rx",
    "ry",
    "shape-rendering",
    "space",
    "stop-color",
    "stop-opacity",
    "stroke",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-miterlimit",
    "stroke-width",
    "style",
    "transform",
    "version",
    "viewBox",
    "width",
    "x",
    "x1",
    "x2",
    "y",
    "y1",
    "y2",
}


def normalize_language_icon_slug(language: str) -> Optional[str]:
    normalized = str(language).strip().lower()
    return LANGUAGE_ICON_SLUGS.get(normalized)


def render_language_icon(
    language: str,
    color: str = "#8b949e",
    *,
    class_name: str = "language-icon",
    size: int = 16,
    x: Optional[int] = None,
    y: Optional[int] = None,
) -> str:
    slug = normalize_language_icon_slug(language)
    if slug is None:
        return _fallback_icon(color, class_name=class_name, size=size, x=x, y=y)

    maybe_icon = _load_sanitized_icon(slug)
    if maybe_icon is None:
        return _fallback_icon(color, class_name=class_name, size=size, x=x, y=y)

    return _with_render_attributes(
        maybe_icon,
        class_name=class_name,
        size=size,
        x=x,
        y=y,
    )


@lru_cache(maxsize=None)
def _load_sanitized_icon(slug: str) -> Optional[str]:
    filename = LANGUAGE_ICON_FILES.get(slug)
    if filename is None:
        return None

    path = ICON_DIR / filename
    if not path.is_file():
        return None

    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except ET.ParseError as error:
        raise ValueError(f"Invalid SVG icon {filename}") from error

    _sanitize_svg_tree(root, slug)
    ET.register_namespace("", SVG_NS)
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


def _sanitize_svg_tree(root: ET.Element, slug: str) -> None:
    if _local_name(root.tag) != "svg":
        raise ValueError("Icon asset root must be an SVG element")

    id_replacements: Dict[str, str] = {}
    for element in root.iter():
        _validate_element(element)
        element_id = element.attrib.get("id")
        if element_id:
            prefixed_id = f"language-icon-{slug}-{element_id}"
            id_replacements[element_id] = prefixed_id
            element.set("id", prefixed_id)

    if id_replacements:
        for element in root.iter():
            for attr_name, value in list(element.attrib.items()):
                for old_id, prefixed_id in id_replacements.items():
                    value = value.replace(f"url(#{old_id})", f"url(#{prefixed_id})")
                    if value == f"#{old_id}":
                        value = f"#{prefixed_id}"
                element.set(attr_name, value)


def _validate_element(element: ET.Element) -> None:
    if _namespace(element.tag) not in ("", SVG_NS):
        raise ValueError("Icon asset contains a non-SVG element")
    if _local_name(element.tag) not in ALLOWED_TAGS:
        raise ValueError(f"Icon asset contains unsafe SVG tag: {_local_name(element.tag)}")

    for raw_attr_name, value in element.attrib.items():
        attr_name = _local_name(raw_attr_name)
        if attr_name.lower().startswith("on"):
            raise ValueError("Icon asset contains an event handler attribute")
        if attr_name == "src":
            raise ValueError("Icon asset contains an external reference attribute")
        if attr_name == "href" and not value.strip().startswith("#"):
            raise ValueError("Icon asset contains an external reference attribute")
        if attr_name not in ALLOWED_ATTRIBUTES:
            raise ValueError(f"Icon asset contains unsafe SVG attribute: {attr_name}")
        if re.search(r"(?:https?:|data:|javascript:)", value, re.IGNORECASE):
            raise ValueError("Icon asset contains an external reference value")
        if "url(" in value and not re.search(r"url\(\s*#[^)]+\)", value):
            raise ValueError("Icon asset contains a non-local URL reference")


def _with_render_attributes(
    svg: str,
    *,
    class_name: str,
    size: int,
    x: Optional[int],
    y: Optional[int],
) -> str:
    root = ET.fromstring(svg)
    root.set("class", class_name)
    root.set("width", str(size))
    root.set("height", str(size))
    root.set("aria-hidden", "true")
    if x is not None:
        root.set("x", str(x))
    if y is not None:
        root.set("y", str(y))
    ET.register_namespace("", SVG_NS)
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


def _fallback_icon(
    color: str,
    *,
    class_name: str,
    size: int,
    x: Optional[int],
    y: Optional[int],
) -> str:
    location = ""
    if x is not None:
        location += f' x="{x}"'
    if y is not None:
        location += f' y="{y}"'
    safe_color = html.escape(str(color), quote=True)
    safe_class = html.escape(f"{class_name} language-icon-fallback", quote=True)
    return (
        f'<svg xmlns="{SVG_NS}" class="{safe_class}"{location} '
        f'viewBox="0 0 16 16" width="{size}" height="{size}" '
        f'aria-hidden="true" style="fill:{safe_color};">'
        '<path fill-rule="evenodd" d="M8 4a4 4 0 100 8 4 4 0 000-8z" />'
        "</svg>"
    )


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _namespace(name: str) -> str:
    if not name.startswith("{"):
        return ""
    return name[1:].split("}", 1)[0]
