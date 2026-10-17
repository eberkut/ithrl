"""Publish only the packaged theme assets used by this site's configuration."""

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import Files, InclusionLevel

THEME_ASSETS = frozenset(
    {
        "css/base.css",
        "css/geist.css",
        "css/pygments/github-dark.css",
        "css/pygments/shadcn-light.css",
        "fonts/Geist.woff2",
        "fonts/GeistMono.woff2",
        "fonts/Inter.woff2",
        "fonts/Inter-Italic.woff2",
        "img/favicon.ico",
        "js/callbacks.js",
        "js/copy-button.js",
        "js/dark.js",
        "js/sidebar.js",
        "js/slider.js",
        "js/toc.js",
        "search/lunr.js",
        "search/main.js",
        "search/worker.js",
    }
)


def on_files(files: Files, *, config: MkDocsConfig) -> Files:
    for file in files:
        if file.src_dir in config.theme.dirs:
            file.inclusion = (
                InclusionLevel.INCLUDED if file.src_uri in THEME_ASSETS else InclusionLevel.EXCLUDED
            )
    return files
