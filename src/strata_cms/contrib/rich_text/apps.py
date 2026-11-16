"""Django AppConfig registering the core rich-text block."""

from strata_cms.contrib.rich_text.blocks import RICH_TEXT_BLOCK, RICH_TEXT_PLUGIN
from strata_cms.plugin_api.django import StrataPluginConfig
from strata_cms.plugin_api.registry import PluginRegistrar


class RichTextPluginConfig(StrataPluginConfig):
    """Core plugin providing the ``strata.rich_text`` block."""

    name = "strata_cms.contrib.rich_text"
    label = "strata_rich_text"
    strata_plugin = RICH_TEXT_PLUGIN

    def register_strata(self, registrar: PluginRegistrar) -> None:
        """Register the rich-text block through the scoped registrar."""
        registrar.register_block_type(RICH_TEXT_BLOCK)
