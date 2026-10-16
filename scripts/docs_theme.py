"""Keep the source allowlist from excluding MkDocs' packaged theme assets."""

from mkdocs.config.defaults import MkDocsConfig
from mkdocs.structure.files import Files, InclusionLevel


def on_files(files: Files, *, config: MkDocsConfig) -> Files:
    for file in files:
        if file.src_dir in config.theme.dirs:
            file.inclusion = InclusionLevel.INCLUDED
    return files
