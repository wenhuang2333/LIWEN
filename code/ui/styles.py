import sys

if sys.platform == "win32":
    MONO_FONT_FAMILY = "'Consolas', 'Courier New'"
elif sys.platform == "darwin":
    MONO_FONT_FAMILY = "'SF Mono', 'Menlo'"
else:
    MONO_FONT_FAMILY = "'DejaVu Sans Mono', 'Noto Mono'"


def _blend(hex1, hex2, ratio):
    """Blend hex1 toward hex2 by ratio (0.0 = pure hex1, 1.0 = pure hex2)."""
    hex1, hex2 = hex1.lstrip("#"), hex2.lstrip("#")
    r1, g1, b1 = int(hex1[0:2], 16), int(hex1[2:4], 16), int(hex1[4:6], 16)
    r2, g2, b2 = int(hex2[0:2], 16), int(hex2[2:4], 16), int(hex2[4:6], 16)
    r = int(r1 + (r2 - r1) * ratio)
    g = int(g1 + (g2 - g1) * ratio)
    b = int(b1 + (b2 - b1) * ratio)
    return f"#{r:02x}{g:02x}{b:02x}"


def _lighten(hex_color, factor=0.15):
    """Lighten a hex color by blending with white."""
    return _blend(hex_color, "#ffffff", factor)


def _darken(hex_color, factor=0.15):
    """Darken a hex color by blending with black."""
    return _blend(hex_color, "#000000", factor)


def _alpha(hex_color, opacity):
    """Simulate alpha by blending hex_color onto a dark base (#11111b)."""
    return _blend("#11111b", hex_color, opacity)


def get_main_style(accent="#89b4fa"):
    # ---- Accent variants ----
    hover = _lighten(accent, 0.18)
    pressed = _lighten(accent, 0.32)
    accent_dim = _blend(accent, "#313244", 0.40)   # muted accent for subtle borders

    # ---- Tinted backgrounds (accent + dark base at different depths) ----
    bg_root = _blend("#1e1e2e", accent, 0.03)
    bg_panel = _blend("#181825", accent, 0.05)
    bg_surface = _blend("#252536", accent, 0.07)
    bg_surface_hover = _blend("#313244", accent, 0.10)
    bg_input = _blend("#1e1e2e", accent, 0.04)
    bg_drop = _blend("#181825", accent, 0.05)
    bg_drop_hover = _blend("#181825", accent, 0.10)
    bg_drop_active = _blend("#181825", accent, 0.14)
    bg_bottom = _blend("#181825", accent, 0.04)
    bg_scrollbar = _blend("#181825", accent, 0.03)
    bg_msgbox = _blend("#1e1e2e", accent, 0.03)

    # ---- Borders (tinted at different strengths) ----
    border_subtle = _blend("#313244", accent, 0.10)
    border_normal = _blend("#45475a", accent, 0.15)
    border_focus = accent
    border_danger = "#f38ba8"
    border_bottom = _blend("#313244", accent, 0.12)

    # ---- Text (neutral light palette — always readable on dark) ----
    text_primary = "#e8e8f0"       # main content text
    text_secondary = "#c8ccd4"     # labels, hints
    text_muted = "#9498a4"         # placeholders, less important
    text_on_accent = "#12121c"     # text on accent-color buttons
    text_danger = "#f38ba8"        # danger/remove labels
    text_success = "#a6e3a1"       # success status
    text_warning = "#f9e2af"       # warning status

    # ---- Accent for checked/selected states ----
    accent_surface = _alpha(accent, 0.25)   # accent at ~25% opacity for selected items
    accent_alpha = _alpha(accent, 0.18)      # subtle accent background
    accent_alpha_hover = _alpha(accent, 0.28)  # accent background on hover

    return f"""
    QMainWindow {{
        background-color: {bg_root};
    }}

    /* ================================================================
       Title Bar
       ================================================================ */
    #titleBar {{
        background-color: {bg_panel};
        border-bottom: 1px solid {border_subtle};
    }}

    /* ================================================================
       Content Wrapper (custom border)
       ================================================================ */
    #contentWrapper {{
        background-color: {bg_root};
        border-right: 1px solid {border_normal};
        border-bottom: 1px solid {border_normal};
    }}

    /* ================================================================
       Left Sidebar — main category navigation
       ================================================================ */
    #sideBar {{
        background-color: {bg_panel};
        border-right: 1px solid {border_subtle};
    }}
    #sideBar QPushButton {{
        background: transparent;
        color: {text_muted};
        border: none;
        border-left: 3px solid transparent;
        padding: 16px 20px;
        font-size: 14px;
        font-weight: 500;
        text-align: left;
    }}
    #sideBar QPushButton:hover {{
        color: {text_primary};
        background-color: {bg_surface};
    }}
    #sideBar QPushButton:checked {{
        color: {accent};
        border-left: 3px solid {accent};
        background-color: {accent_alpha};
    }}

    /* ================================================================
       Sub-Tab Bar — secondary navigation within a category
       ================================================================ */
    #subTabBar {{
        background-color: {bg_panel};
        border-bottom: 2px solid {border_subtle};
        padding: 0px;
    }}
    #subTabBar QPushButton {{
        background: transparent;
        color: {text_muted};
        border: none;
        padding: 10px 28px;
        font-size: 14px;
        font-weight: 500;
        border-bottom: 2px solid transparent;
        margin-bottom: -2px;
    }}
    #subTabBar QPushButton:hover {{
        color: {text_primary};
        background-color: {bg_surface};
    }}
    #subTabBar QPushButton:checked {{
        color: {accent};
        border-bottom: 2px solid {accent};
        background-color: {bg_root};
    }}

    /* ================================================================
       Base Widgets — global defaults for all un-contained widgets
       ================================================================ */
    QLabel {{
        color: {text_primary};
        background: transparent;
    }}
    QGroupBox {{
        color: {text_primary};
        font-size: 13px;
        font-weight: 600;
        border: 1px solid {border_subtle};
        border-radius: 8px;
        margin-top: 12px;
        padding-top: 16px;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 6px;
        color: {text_primary};
    }}

    /* ================================================================
       Tab Bar
       ================================================================ */
    #tabBar {{
        background-color: {bg_panel};
        border-bottom: 2px solid {border_subtle};
        padding: 0px;
    }}
    #tabBar QPushButton {{
        background: transparent;
        color: {text_muted};
        border: none;
        padding: 10px 28px;
        font-size: 14px;
        font-weight: 500;
        border-bottom: 2px solid transparent;
        margin-bottom: -2px;
    }}
    #tabBar QPushButton:hover {{
        color: {text_primary};
        background-color: {bg_surface};
    }}
    #tabBar QPushButton:checked {{
        color: {accent};
        border-bottom: 2px solid {accent};
        background-color: {bg_root};
    }}

    /* ================================================================
       Mode Toggle
       ================================================================ */
    #modeToggle QPushButton {{
        background-color: {bg_surface};
        color: {text_muted};
        border: 1px solid {border_normal};
        padding: 8px 24px;
        font-size: 13px;
        font-weight: 500;
    }}
    #modeToggle QPushButton:first-child {{
        border-radius: 6px 0 0 6px;
    }}
    #modeToggle QPushButton:last-child {{
        border-radius: 0 6px 6px 0;
        border-left: none;
    }}
    #modeToggle QPushButton:checked {{
        background-color: {accent};
        color: {text_on_accent};
        border-color: {accent};
    }}
    #modeToggle QPushButton:disabled:checked {{
        background-color: {accent_dim};
        color: {text_primary};
        border-color: {accent_dim};
    }}
    #modeToggle QPushButton:hover:!checked {{
        background-color: {bg_surface_hover};
        color: {text_primary};
    }}

    /* ================================================================
       Page Container
       ================================================================ */
    #pageContainer {{
        background-color: {bg_root};
    }}

    /* ================================================================
       Drop Zone
       ================================================================ */
    #dropZone {{
        background-color: {bg_drop};
        border: 2px dashed {border_normal};
        border-radius: 12px;
    }}
    #dropZone:hover {{
        border-color: {accent};
        background-color: {bg_drop_hover};
    }}
    #dropZone[drag_hover="true"] {{
        border-color: {accent};
        background-color: {bg_drop_active};
        border-style: solid;
    }}
    #dropZoneHint {{
        color: {text_muted};
        font-size: 15px;
    }}

    /* ================================================================
       File / Device List
       ================================================================ */
    QListWidget {{
        background-color: {bg_panel};
        border: 1px solid {border_subtle};
        border-radius: 8px;
        color: {text_primary};
        font-size: 13px;
        padding: 4px;
        outline: none;
    }}
    QListWidget::item {{
        padding: 6px 10px;
        border-radius: 4px;
        color: {text_primary};
    }}
    QListWidget::item:selected {{
        background-color: {accent_surface};
        color: {text_primary};
    }}
    QListWidget::item:hover {{
        background-color: {bg_surface};
    }}

    /* ================================================================
       Settings / Right Panel
       ================================================================ */
    #settingsPanel {{
        background-color: {bg_panel};
        border: 1px solid {border_subtle};
        border-radius: 12px;
        padding: 16px;
    }}
    #settingsPanel QLabel {{
        color: {text_secondary};
        font-size: 13px;
    }}
    #settingsPanel QLineEdit, #settingsPanel QComboBox {{
        background-color: {bg_input};
        border: 1px solid {border_normal};
        border-radius: 6px;
        padding: 8px 12px;
        color: {text_primary};
        font-size: 13px;
        min-height: 20px;
    }}
    #settingsPanel QLineEdit:focus, #settingsPanel QComboBox:focus {{
        border-color: {accent};
    }}
    #settingsPanel QLineEdit:read-only {{
        color: {text_muted};
    }}
    #settingsPanel QComboBox {{
        padding: 6px 10px;
    }}
    #settingsPanel QComboBox::drop-down {{
        border: none;
        width: 24px;
    }}
    #settingsPanel QComboBox QAbstractItemView {{
        background-color: {bg_input};
        border: 1px solid {border_normal};
        border-radius: 4px;
        color: {text_primary};
        selection-background-color: {accent_surface};
    }}
    #settingsPanel QSpinBox {{
        background-color: {bg_input};
        border: 1px solid {border_normal};
        border-radius: 6px;
        padding: 8px 12px;
        color: {text_primary};
        font-size: 13px;
    }}
    #settingsPanel QSpinBox:focus {{
        border-color: {accent};
    }}
    #settingsPanel QCheckBox {{
        color: {text_secondary};
        font-size: 13px;
        spacing: 8px;
    }}
    #settingsPanel QCheckBox::indicator {{
        width: 18px;
        height: 18px;
        border-radius: 4px;
        border: 2px solid {border_normal};
        background-color: {bg_input};
    }}
    #settingsPanel QCheckBox::indicator:checked {{
        background-color: {accent};
        border-color: {accent};
    }}
    #settingsPanel QTextEdit {{
        background-color: {bg_input};
        border: 1px solid {border_normal};
        border-radius: 6px;
        padding: 8px 10px;
        color: {text_primary};
        font-size: 12px;
    }}
    #settingsPanel QTextEdit:focus {{
        border-color: {accent};
    }}

    /* ================================================================
       Buttons
       ================================================================ */
    QPushButton {{
        background-color: {accent};
        color: {text_on_accent};
        border: none;
        border-radius: 6px;
        padding: 8px 20px;
        font-size: 13px;
        font-weight: 600;
        min-height: 20px;
    }}
    QPushButton:hover {{
        background-color: {hover};
    }}
    QPushButton:pressed {{
        background-color: {pressed};
    }}
    QPushButton:disabled {{
        background-color: {bg_surface_hover};
        color: {text_muted};
    }}

    QPushButton#actionBtn {{
        padding: 14px 32px;
        font-size: 15px;
        font-weight: 700;
        border-radius: 8px;
        min-height: 24px;
    }}

    QPushButton#secondaryBtn {{
        background-color: {bg_surface};
        color: {text_primary};
        border: 1px solid {border_normal};
    }}
    QPushButton#secondaryBtn:hover {{
        background-color: {bg_surface_hover};
        color: {text_primary};
    }}

    QPushButton#globalCheckBtn {{
        padding: 16px 40px;
        font-size: 17px;
        font-weight: 800;
        border-radius: 10px;
        min-height: 28px;
        background-color: {accent};
        color: #1e1e2e;
        border: 2px solid {accent};
    }}
    QPushButton#globalCheckBtn:hover {{
        background-color: {hover};
        color: #1e1e2e;
        border: 2px solid {hover};
    }}
    QPushButton#globalCheckBtn:disabled {{
        background-color: {bg_surface};
        color: {text_muted};
        border: 1px solid {border_normal};
    }}

    QPushButton#lanCheckBtn {{
        padding: 14px 32px;
        font-size: 15px;
        font-weight: 700;
        border-radius: 8px;
        min-height: 24px;
        background-color: {accent_alpha};
        color: {accent};
        border: 2px solid {accent};
    }}
    QPushButton#lanCheckBtn:hover {{
        background-color: {accent_alpha_hover};
        color: {text_primary};
    }}
    QPushButton#lanCheckBtn:disabled {{
        background-color: {bg_surface};
        color: {text_muted};
        border: 1px solid {border_normal};
    }}

    QPushButton#deepCheckBtn {{
        padding: 14px 32px;
        font-size: 15px;
        font-weight: 700;
        border-radius: 8px;
        min-height: 24px;
        background-color: #f38ba8;
        color: #1e1e2e;
        border: 2px solid #f38ba8;
    }}
    QPushButton#deepCheckBtn:hover {{
        background-color: #f5a0b8;
        color: #1e1e2e;
        border: 2px solid #f5a0b8;
    }}
    QPushButton#deepCheckBtn:disabled {{
        background-color: {bg_surface};
        color: {text_muted};
        border: 1px solid {border_normal};
    }}

    QPushButton#repairBtn {{
        padding: 12px 28px;
        font-size: 14px;
        font-weight: 700;
        border-radius: 8px;
        min-height: 24px;
        background-color: #f9e2af;
        color: #12121c;
        border: 2px solid #f9e2af;
    }}
    QPushButton#repairBtn:hover {{
        background-color: #fceeca;
        color: #12121c;
        border: 2px solid #fceeca;
    }}
    QPushButton#repairBtn:disabled {{
        background-color: {bg_surface};
        color: {text_muted};
        border: 1px solid {border_normal};
    }}

    QPushButton#repairExternalBtn {{
        padding: 12px 28px;
        font-size: 14px;
        font-weight: 700;
        border-radius: 8px;
        min-height: 24px;
        background-color: #89b4fa;
        color: #12121c;
        border: 2px solid #89b4fa;
    }}
    QPushButton#repairExternalBtn:hover {{
        background-color: #a1c6fb;
        color: #12121c;
        border: 2px solid #a1c6fb;
    }}
    QPushButton#repairExternalBtn:disabled {{
        background-color: {bg_surface};
        color: {text_muted};
        border: 1px solid {border_normal};
    }}

    QPushButton#dangerBtn {{
        background-color: {bg_surface};
        color: {text_danger};
        border: 1px solid {border_danger};
    }}
    QPushButton#dangerBtn:hover {{
        background-color: {text_danger};
        color: {text_on_accent};
    }}

    QPushButton#smallBtn {{
        padding: 4px 12px;
        font-size: 12px;
        min-height: 16px;
    }}

    /* ================================================================
       Section Titles
       ================================================================ */
    #sectionTitle {{
        color: {text_primary};
        font-size: 15px;
        font-weight: 600;
    }}

    /* ================================================================
       Bottom Bar
       ================================================================ */
    #bottomBar {{
        background-color: {bg_bottom};
        border-top: 1px solid {border_bottom};
    }}
    #bottomBar QLabel {{
        color: {text_secondary};
        font-size: 12px;
    }}

    QProgressBar {{
        background-color: {bg_surface};
        border: none;
        border-radius: 4px;
        height: 6px;
        text-align: center;
        font-size: 11px;
        color: {text_primary};
    }}
    QProgressBar::chunk {{
        background-color: {accent};
        border-radius: 4px;
    }}

    /* ================================================================
       Scroll Area
       ================================================================ */
    QScrollArea {{
        background-color: transparent;
        border: none;
    }}
    QScrollArea > QWidget > QWidget {{
        background-color: transparent;
    }}

    /* ================================================================
       Scrollbar
       ================================================================ */
    QScrollBar:vertical {{
        background-color: {bg_scrollbar};
        width: 8px;
        border-radius: 4px;
    }}
    QScrollBar::handle:vertical {{
        background-color: {border_normal};
        border-radius: 4px;
        min-height: 30px;
    }}
    QScrollBar::handle:vertical:hover {{
        background-color: {text_muted};
    }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
        height: 0px;
    }}

    /* ================================================================
       Message Box
       ================================================================ */
    QMessageBox {{
        background-color: {bg_msgbox};
        color: {text_primary};
    }}
    QMessageBox QLabel {{
        color: {text_primary};
    }}
    QMessageBox QPushButton {{
        min-width: 80px;
    }}

    /* ================================================================
       Device List Items
       ================================================================ */
    #deviceList QListWidget::item {{
        padding: 8px 12px;
        border-bottom: 1px solid {border_subtle};
    }}

    /* ================================================================
       My Page Card Accents
       ================================================================ */
    #myCardTitle {{
        color: {text_primary};
        font-size: 22px;
        font-weight: 700;
    }}
    #myCardHint {{
        color: {text_muted};
        font-size: 11px;
    }}
    #pageHint {{
        color: {text_muted};
        font-size: 12px;
    }}
    #statusMuted {{
        color: {text_muted};
        font-size: 11px;
    }}
    #fileSelectedHint {{
        color: {accent};
        font-size: 11px;
    }}
    #pwdHint {{
        color: {text_warning};
        font-size: 11px;
    }}
    """


MAIN_STYLE = get_main_style()
