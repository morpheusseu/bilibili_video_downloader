"""Native desktop adaptation of design/apple/DESIGN.md; no web/font dependencies."""

from pathlib import Path
from string import Template

COLORS = {
    "blue": "#0066cc",
    "focus": "#0071e3",
    "ink": "#1d1d1f",
    "muted": "#6e6e73",  # Slightly darker than the reference for small-text contrast.
    "white": "#ffffff",
    "parchment": "#f5f5f7",
    "pearl": "#fafafc",
    "hairline": "#e0e0e0",
    "soft": "#f0f0f0",
    "disabled": "#7a7a7a",
    "pink": "#cf2f73",
    "red": "#c62828",
    "down": Path(__file__).with_name("chevron-down.svg").as_posix(),
    "up": Path(__file__).with_name("chevron-up.svg").as_posix(),
}

STYLE = Template("""
    QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget {
        background: $parchment;
    }
    QWidget { color: $ink; font-size: 14px; font-weight: 400; }
    QDialog#accountDialog { background: $white; }
    QLabel { background: transparent; }
    QLabel#brand { font-size: 17px; font-weight: 600; }
    QLabel#hero { font-size: 34px; font-weight: 600; }
    QLabel#subtitle { font-size: 17px; color: $muted; }
    QLabel#sectionTitle { font-size: 21px; font-weight: 600; }
    QLabel#taskTitle { font-size: 17px; font-weight: 600; }
    QLabel#hint { color: $muted; font-size: 12px; }
    QLabel#finePrint { color: $muted; font-size: 11px; }
    QLabel#identity { color: $ink; font-size: 14px; }
    QWidget#header { background: $white; border-bottom: 1px solid $hairline; }
    QFrame#card { background: $white; border: 1px solid $hairline; border-radius: 18px; }
    QFrame#taskPanel { background: $white; border-top: 1px solid $hairline; }
    QLineEdit, QComboBox, QSpinBox {
        background: $white; border: 1px solid $hairline; border-radius: 8px;
        padding: 9px 12px; min-height: 20px; selection-background-color: $blue;
        selection-color: $white;
    }
    QLineEdit#sourceInput { font-size: 17px; border-radius: 11px; padding: 10px 12px; }
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid $focus; }
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {
        color: $disabled; background: $parchment;
    }
    QComboBox { padding-right: 24px; }
    QComboBox::drop-down { border: none; width: 24px; }
    QComboBox::down-arrow { image: url("$down"); width: 10px; height: 6px; }
    QComboBox QAbstractItemView { background: $white; selection-background-color: $blue;
        selection-color: $white; border: 1px solid $hairline; padding: 4px; }
    QSpinBox { padding-right: 24px; }
    QSpinBox::up-button, QSpinBox::down-button { width: 22px; border: none; }
    QSpinBox::up-arrow { image: url("$up"); width: 8px; height: 5px; }
    QSpinBox::down-arrow { image: url("$down"); width: 8px; height: 5px; }
    QPushButton {
        background: $pearl; border: 1px solid $hairline; border-radius: 8px;
        padding: 8px 14px; min-height: 20px;
    }
    QPushButton:pressed, QPushButton:checked { background: $soft; }
    QPushButton:focus { border: 1px solid $focus; }
    QPushButton#primary, QPushButton#secondary { border-radius: 21px; padding: 10px 20px; }
    QPushButton#primary { background: $blue; color: $white; border: 1px solid $blue; }
    QPushButton#primary:pressed { background: $focus; }
    QPushButton#primary:focus { border: 2px solid $ink; padding: 9px 19px; }
    QPushButton#secondary { color: $blue; border: 1px solid $blue; background: $white; }
    QPushButton#secondary:pressed { background: $parchment; }
    QPushButton#link { color: $blue; background: transparent; border: 1px solid transparent;
        padding: 3px 6px; min-height: 22px; }
    QPushButton#link:focus { border: 1px solid $focus; }
    QPushButton#link:checked { background: $parchment; }
    QPushButton:disabled, QPushButton#primary:disabled, QPushButton#secondary:disabled {
        color: $disabled; background: $parchment; border: 1px solid $hairline;
    }
    QPushButton#accountAvatar { background: $white; color: $pink; border: 1px solid $hairline;
        border-radius: 22px; padding: 0; font-size: 12px; font-weight: 600;
        min-width: 42px; max-width: 42px; min-height: 42px; max-height: 42px; }
    QPushButton#accountAvatar:checked { background: $parchment; }
    QPushButton#accountAvatar:focus { border: 1px solid $focus; }
    QPushButton#favoritesStar { background: transparent; border: 1px solid transparent;
        border-radius: 22px; padding: 0;
        min-width: 42px; max-width: 42px; min-height: 42px; max-height: 42px; }
    QPushButton#favoritesStar:checked { background: $parchment; border: 1px solid $hairline; }
    QPushButton#favoritesStar:focus { border: 1px solid $focus; }
    QPushButton#inboxMail { background: transparent; border: 1px solid transparent;
        border-radius: 22px; padding: 0;
        min-width: 42px; max-width: 42px; min-height: 42px; max-height: 42px; }
    QPushButton#inboxMail:checked { background: $parchment; border: 1px solid $hairline; }
    QPushButton#inboxMail:focus { border: 1px solid $focus; }
    QPushButton#downloadDetails { padding: 0; border: none;
        min-width: 56px; max-width: 56px; min-height: 56px; max-height: 56px; }
    QCheckBox { spacing: 8px; min-height: 26px; }
    QCheckBox::indicator { width: 16px; height: 16px; }
    QCheckBox:disabled, QLabel:disabled { color: $disabled; }
    QTabBar::tab { background: $pearl; color: $muted; border: 1px solid $hairline;
        padding: 9px 12px; min-height: 20px; }
    QTabBar::tab:selected { background: $white; color: $blue; border-bottom: 2px solid $blue; }
    QTabBar::tab:disabled { color: $disabled; }
    QProgressBar { background: $soft; border: none; border-radius: 3px; }
    QProgressBar::chunk { background: $blue; border-radius: 3px; }
    QPlainTextEdit { background: $pearl; border: 1px solid $hairline; border-radius: 8px;
        padding: 8px; font-size: 12px; selection-background-color: $blue; }
    QTreeWidget, QListWidget { background: $pearl; border: 1px solid $hairline; border-radius: 8px;
        padding: 4px; outline: 0; }
    QTreeWidget::item, QListWidget::item { padding: 6px 4px; }
    QTreeWidget::item:selected, QListWidget::item:selected { background: $blue; color: $white; }
    QHeaderView::section { background: $pearl; color: $muted; border: none;
        border-bottom: 1px solid $hairline; padding: 7px; font-size: 12px; }
    QScrollBar:vertical { background: transparent; width: 8px; margin: 4px 0; }
    QScrollBar::handle:vertical { background: $hairline; border-radius: 4px; min-height: 24px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
    QToolTip { color: $ink; background: $white; border: 1px solid $hairline; padding: 6px; }
""").substitute(COLORS)
