"""
Dashboard theme + font API.

Extracted from the monolithic web_server.py so the theme domain is
isolated, testable, and reloadable.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from hermes_cli.config import cfg_get, load_config, save_config

router = APIRouter()


_BUILTIN_DASHBOARD_THEMES: List[Dict[str, str]] = [
    {"name": "default",       "label": "Hermes Teal",         "description": "Classic dark teal — the canonical Hermes look"},
    {"name": "default-large", "label": "Hermes Teal (Large)", "description": "Hermes Teal with bigger fonts and roomier spacing"},
    {"name": "nous-blue",     "label": "Nous Blue",           "description": "Light mode — vivid Nous-blue accents on cream canvas"},
    {"name": "midnight",      "label": "Midnight",            "description": "Deep blue-violet with cool accents"},
    {"name": "ember",         "label": "Ember",               "description": "Warm crimson and bronze — forge vibes"},
    {"name": "mono",          "label": "Mono",                "description": "Clean grayscale — minimal and focused"},
    {"name": "cyberpunk",     "label": "Cyberpunk",           "description": "Neon green on black — matrix terminal"},
    {"name": "rose",          "label": "Rosé",                "description": "Soft pink and warm ivory — easy on the eyes"},
    {"name": "cairo-nights",  "label": "Cairo Nights",        "description": "Egyptian dark luxury — deep navy, warm gold, and violet glow"},
]


_THEME_DEFAULT_TYPOGRAPHY = {
    "fontSans": "ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    "fontMono": "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, monospace",
    "fontDisplay": "ui-sans-serif, system-ui, sans-serif",
    "fontUrl": "",
    "baseSize": "16px",
    "lineHeight": "1.5",
    "letterSpacing": "0",
}


_THEME_DEFAULT_LAYOUT = {
    "radius": "0.75rem",
    "density": "comfortable",
}


_THEME_OVERRIDE_KEYS = frozenset({
    "primary", "primaryForeground", "background", "foreground", "card",
    "cardForeground", "popover", "popoverForeground", "muted", "mutedForeground",
    "accent", "accentForeground", "border", "input", "ring", "destructive",
    "destructiveForeground", "sidebar", "sidebarForeground", "sidebarPrimary",
    "sidebarPrimaryForeground", "sidebarAccent", "sidebarAccentForeground",
    "sidebarBorder", "sidebarRing",
})


_THEME_NAMED_ASSET_KEYS = frozenset({
    "hero", "logo", "icon", "favicon", "noise", "pattern", "mask",
})


_THEME_CUSTOM_CSS_MAX = 50_000


_THEME_COMPONENT_BUCKETS = frozenset({
    "card", "sidebar", "navItem", "button", "input", "select", "dialog",
    "popover", "table", "badge", "tooltip", "appHeader", "pageHeader",
    "backdrop", "chat", "terminal",
})


def _parse_theme_layer(value: Any, default_hex: str, default_alpha: float = 1.0) -> Optional[Dict[str, Any]]:
    """Normalise a theme layer spec from YAML into `{hex, alpha}` form."""
    if value is None:
        return {"hex": default_hex, "alpha": default_alpha}
    if isinstance(value, str):
        return {"hex": value, "alpha": default_alpha}
    if isinstance(value, dict):
        hex_val = value.get("hex", default_hex)
        alpha_val = value.get("alpha", default_alpha)
        if not isinstance(hex_val, str):
            return None
        try:
            alpha_f = float(alpha_val)
        except (TypeError, ValueError):
            alpha_f = default_alpha
        return {"hex": hex_val, "alpha": alpha_f}
    return None


def _discover_user_themes() -> List[Dict[str, Any]]:
    """Read ``~/.hermes/dashboard-themes/*.yaml`` and normalise each theme."""
    from hermes_cli.config import get_hermes_home
    themes_dir = get_hermes_home() / "dashboard-themes"
    if not themes_dir.is_dir():
        return []
    results: List[Dict[str, Any]] = []
    import yaml
    for path in sorted(themes_dir.glob("*.yaml")):
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name") or path.stem
        normalised = _normalise_theme_definition(name, data)
        results.append(normalised)
    return results


def _normalise_theme_definition(name: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Convert user theme YAML into the frontend's expected shape."""
    label = data.get("label") or name
    description = data.get("description") or ""
    palette_src = data.get("palette", {}) if isinstance(data.get("palette"), dict) else {}
    colors_src = data.get("colors", {}) if isinstance(data.get("colors"), dict) else {}

    def _layer(key: str, default_hex: str, default_alpha: float = 1.0) -> Dict[str, Any]:
        spec = palette_src.get(key, colors_src.get(key))
        parsed = _parse_theme_layer(spec, default_hex, default_alpha)
        return parsed if parsed is not None else {"hex": default_hex, "alpha": default_alpha}

    palette = {
        "background": _layer("background", "#041c1c", 1.0),
        "midground": _layer("midground", "#ffe6cb", 1.0),
        "foreground": _layer("foreground", "#ffffff", 0.0),
        "warmGlow": palette_src.get("warmGlow") or data.get("warmGlow") or "rgba(255, 189, 56, 0.35)",
        "noiseOpacity": 1.0,
    }
    raw_noise = palette_src.get("noiseOpacity", data.get("noiseOpacity"))
    try:
        palette["noiseOpacity"] = float(raw_noise) if raw_noise is not None else 1.0
    except (TypeError, ValueError):
        palette["noiseOpacity"] = 1.0

    typo_src = data.get("typography", {}) if isinstance(data.get("typography"), dict) else {}
    typography = dict(_THEME_DEFAULT_TYPOGRAPHY)
    for key in ("fontSans", "fontMono", "fontDisplay", "fontUrl", "baseSize", "lineHeight", "letterSpacing"):
        val = typo_src.get(key)
        if isinstance(val, str) and val.strip():
            typography[key] = val

    layout_src = data.get("layout", {}) if isinstance(data.get("layout"), dict) else {}
    layout = dict(_THEME_DEFAULT_LAYOUT)
    radius = layout_src.get("radius")
    if isinstance(radius, str) and radius.strip():
        layout["radius"] = radius
    density = layout_src.get("density")
    if isinstance(density, str) and density in {"compact", "comfortable", "spacious"}:
        layout["density"] = density

    overrides_src = data.get("colorOverrides", {})
    color_overrides: Dict[str, str] = {}
    if isinstance(overrides_src, dict):
        for key, val in overrides_src.items():
            if key in _THEME_OVERRIDE_KEYS and isinstance(val, str) and val.strip():
                color_overrides[key] = val

    assets_out: Dict[str, Any] = {}
    assets_src = data.get("assets", {}) if isinstance(data.get("assets"), dict) else {}
    for key in _THEME_NAMED_ASSET_KEYS:
        val = assets_src.get(key)
        if isinstance(val, str) and val.strip():
            assets_out[key] = val
    custom_assets_src = assets_src.get("custom")
    if isinstance(custom_assets_src, dict):
        custom_assets: Dict[str, str] = {}
        for key, val in custom_assets_src.items():
            if (
                isinstance(key, str)
                and key.replace("-", "").replace("_", "").isalnum()
                and isinstance(val, str)
                and val.strip()
            ):
                custom_assets[key] = val
        if custom_assets:
            assets_out["custom"] = custom_assets

    custom_css_val = data.get("customCSS")
    custom_css: Optional[str] = None
    if isinstance(custom_css_val, str) and custom_css_val.strip():
        custom_css = custom_css_val[:_THEME_CUSTOM_CSS_MAX]

    component_styles_src = data.get("componentStyles", {})
    component_styles: Dict[str, Dict[str, str]] = {}
    if isinstance(component_styles_src, dict):
        for bucket, props in component_styles_src.items():
            if bucket not in _THEME_COMPONENT_BUCKETS or not isinstance(props, dict):
                continue
            clean: Dict[str, str] = {}
            for prop, value in props.items():
                if (
                    isinstance(prop, str)
                    and prop.replace("-", "").replace("_", "").isalnum()
                    and isinstance(value, (str, int, float))
                    and str(value).strip()
                ):
                    clean[prop] = str(value)
            if clean:
                component_styles[bucket] = clean

    return {
        "name": name,
        "label": label,
        "description": description,
        "palette": palette,
        "typography": typography,
        "layout": layout,
        "colorOverrides": color_overrides,
        "assets": assets_out,
        "customCSS": custom_css,
        "componentStyles": component_styles,
        "variant": data.get("variant") if isinstance(data.get("variant"), str) else "default",
    }


_FONT_DEFAULT_ID = "theme"
_FONT_CHOICES = frozenset({
    "system-sans", "system-serif", "system-mono",
    "inter", "ibm-plex-sans", "work-sans", "atkinson-hyperlegible", "dm-sans",
    "spectral", "fraunces", "source-serif",
    "jetbrains-mono", "ibm-plex-mono", "space-mono",
})


class ThemeSetBody(BaseModel):
    name: str


class FontSetBody(BaseModel):
    font: str


@router.get("/themes")
async def get_dashboard_themes():
    """Return available themes and the currently active one."""
    config = load_config()
    active = cfg_get(config, "dashboard", "theme", default="default")
    user_themes = _discover_user_themes()
    seen = set()
    themes: List[Dict[str, Any]] = []
    for t in _BUILTIN_DASHBOARD_THEMES:
        seen.add(t["name"])
        themes.append(t)
    for t in user_themes:
        if t["name"] in seen:
            continue
        themes.append({
            "name": t["name"],
            "label": t["label"],
            "description": t["description"],
            "definition": t,
        })
        seen.add(t["name"])
    return {"themes": themes, "active": active}


@router.put("/theme")
async def set_dashboard_theme(body: ThemeSetBody):
    """Set the active dashboard theme (persists to config.yaml)."""
    config = load_config()
    if "dashboard" not in config:
        config["dashboard"] = {}
    config["dashboard"]["theme"] = body.name
    save_config(config)
    return {"ok": True, "theme": body.name}


@router.get("/font")
async def get_dashboard_font():
    """Return the active font override (``"theme"`` = use the theme's font)."""
    config = load_config()
    font = cfg_get(config, "dashboard", "font", default=_FONT_DEFAULT_ID)
    if font not in _FONT_CHOICES:
        font = _FONT_DEFAULT_ID
    return {"font": font}


@router.put("/font")
async def set_dashboard_font(body: FontSetBody):
    """Set the dashboard font override (persists to config.yaml)."""
    font = body.font if body.font in _FONT_CHOICES else _FONT_DEFAULT_ID
    config = load_config()
    if "dashboard" not in config:
        config["dashboard"] = {}
    config["dashboard"]["font"] = font
    save_config(config)
    return {"ok": True, "font": font}
