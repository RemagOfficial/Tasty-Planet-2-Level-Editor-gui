"""
Tasty Planet 2 level editor.

Data layer: the correct community parser (read_level / write_level). The file is
per-layer; each layer holds walls (collisions for player and entities), paths (splines for entities to follow), 
entities and decorations (the background). Positions are delta-encoded on disk; this editor keeps
absolute positions in the level dict and lets write_level re-encode on save (byte-exact).

Decoration decode notes (verified using japansword1a.bin):
  * a decoration of type 0 reuses the PREVIOUS decoration's tile (run-length);
    type 1 carries a cell index into tileTypes, type 2 an inline name.
  * the decoration 'size' field is a ROTATION stored in centidegrees (-raw/100 = 90).
"""
import sys, os, re, copy, math, glob, json, collections
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QGraphicsScene, QGraphicsView, QGraphicsPixmapItem,
    QGraphicsPathItem, QGraphicsRectItem, QGraphicsEllipseItem, QDockWidget, QWidget,
    QVBoxLayout, QFormLayout, QHBoxLayout, QLabel, QPushButton, QDoubleSpinBox, QSpinBox, QCheckBox,
    QListWidget, QListWidgetItem, QFileDialog, QColorDialog, QLineEdit, QStyle, QFrame,
    QDialog, QGridLayout, QComboBox, QScrollArea, QToolButton, QGraphicsItemGroup,
    QTableWidget, QTableWidgetItem, QHeaderView, QMenu, QMessageBox)
from PySide6.QtGui import (
    QPixmap, QImage, QColor, QPainter, QPen, QAction, QPainterPath, QFont, QPolygonF,
    QKeySequence, QIcon, QBrush)
from PySide6.QtCore import Qt, QRectF, QPointF, Signal, QObject, QTimer, QSize

from read_level import read_level
from write_level import write_level
import tp2multi as TM
from tp2assets import Assets

ENTITY_K = 100.0      # entity dict position -> level units
DECO_K   = 1.0        # decoration dict position -> level units
DIM_BASE = 16000.0    # decoration dimensions raw -> scale 1.0
TURN     = 65536.0    # raw -> 360 deg
HIST_MAX = 40
DEFAULT_GFX = r"C:\Program Files (x86)\Steam\steamapps\common\Tasty Planet Back for Seconds\assets\graphics"
CONFIG_FILE = "editor_config.json"

# shared field specs (key, label, choices|None, tooltip)
# choices: list of (stored_value, display_text); None => free-text line edit.
TF_CHOICES = [('false', 'false'), ('true', 'true')]
VICTORY_CHOICES = [('0', '0 — reach a size'), ('1', '1 — collect objects'), ('2', '2 — no size meter')]

STAGE_SPECS = [   # per-<level> attributes (excludes name, that's handled separately)
    ('triggerarea',    "Goal area — max size to reach", None,
        "Goo area that ends/transitions this stage (victory type 0). Bigger = grow larger to win."),
    ('meterperpix',    "Zoom (meterperpix)", None,
        "World meters per on-screen pixel — sets how large every size reads."),
    ('posx',           "Stage pos X", None, "Stage position in the concentric multilevel layout."),
    ('posy',           "Stage pos Y", None, "Stage position in the concentric multilevel layout."),
    ('gootostart',     "Goo carries to start", TF_CHOICES,
        "If true, the goo keeps its current size when entering this stage."),
    ('triggerspecial', "Trigger special", None, "Special-trigger index (-1 = none)."),
]
ML_SPECS = [      # multilevel-wide attributes
    ('victorytype',        "Victory type", VICTORY_CHOICES,
        "How you win: 0 reach a size · 1 collect N special entities · 2 hides the size meter."),
    ('numspecialentities', "Special entities to collect", None,
        "How many special entities to win (used with victory type 1)."),
    ('timelimit',          "Time limit (sec)", None, "Seconds allowed to complete the level."),
    ('goldtime',           "Gold time (sec)", None, "Finish within this many seconds to earn gold."),
    ('silvertime',         "Silver time (sec)", None, "Finish within this many seconds to earn silver."),
    ('bronzetime',         "Bronze time (sec)", None, "Finish within this many seconds to earn bronze."),
    ('levelmusicscript',   "Music script", None, "Overrides the music track for this level."),
    ('tipscriptfunction',  "Tip script", None, "Shows a tip at the start of the level."),
    ('comicstartfunction', "Start comic", None, "Plays a comic at the start of the level."),
    ('comicendfunction',   "End comic", None, "Plays a comic at the end of the level."),
    ('smallfailstring',    "Fail message", None, "Text shown on failure."),
]

# create levels from scratch
def blank_level_dict():
    """A valid, empty level: 5 named layers, no tiles/walls/paths/entities/decorations."""
    return {'dummy': 0, 'tileTypeCount': 0, 'tileTypes': [], 'layerCount': 5,
            'layers': [{'walls': [], 'paths': [], 'entities': [], 'decorations': []} for _ in range(5)]}

def level_shell_xml(edges=(-1600.0, -1200.0, 1600.0, 1200.0), growthrate=0.25,
                    goostart_area=2900, goo_xy=(0.0, 0.0)):
    """The .xml shell paired with a level .bin (edges, layers, goo start)."""
    el, et, er, eb = edges; gx, gy = goo_xy
    return (
        f'<level sidescroll="false" waterlevel="0" '
        f'edgeleft="{el:.6f}" edgetop="{et:.6f}" edgeright="{er:.6f}" edgebottom="{eb:.6f}" '
        f'growthrate="{float(growthrate):.6f}">\n'
        '    <elementengine>\n'
        '        <layer name="Background" sorteddraw="true" drawabove="false" parx="1.000000" pary="1.000000" />\n'
        '        <layer name="Background Front" sorteddraw="true" drawabove="false" parx="1.000000" pary="1.000000" />\n'
        '        <layer name="Main Bottom" sorteddraw="true" drawabove="false" parx="1.000000" pary="1.000000" />\n'
        '        <layer name="Main" sorteddraw="true" drawabove="false" parx="1.000000" pary="1.000000">\n'
        f'            <goostart x="{gx:.6f}" y="{gy:.6f}" area="{int(goostart_area)}" '
        'singleplayer="true" multiplayer1="true" multiplayer2="false" priority="1080" />\n'
        '        </layer>\n'
        '        <layer name="Unnamed Layer" sorteddraw="true" drawabove="false" parx="1.000000" pary="1.000000" />\n'
        '    </elementengine>\n'
        '</level>\n')

def write_blank_level(bin_path, **shell_kw):
    """Write a new blank .bin and its paired .xml shell. Returns (bin_path, xml_path)."""
    write_level(blank_level_dict(), bin_path)
    xml_path = os.path.splitext(bin_path)[0] + ".xml"
    with open(xml_path, 'w', encoding='utf-8', newline='') as f:
        f.write(level_shell_xml(**shell_kw))
    return bin_path, xml_path

def default_stage(name):
    return TM.Stage({'name': name, 'meterperpix': '1.000000e-003', 'posx': '0.000000e+000',
                     'posy': '0.000000e+000', 'triggerarea': '20000', 'triggerspecial': '-1',
                     'gootostart': 'false'})

def default_path_follow(name):
    """Path-follow block matching lab2a's frogs (standard follow params)."""
    return {'v0': 0.55, 'v1': 0.55, 'flag0': 0, 'v2': 0.5, 'v3': 2.0,
            'path_name': name, 'flag1': 0, 'mode': 1, 'v4': 1.0}

# Path-follow parameters.
# Tested using various existing levels in the game and the parameters those entities use.
# Across every follower: v0=v1=0.55, flag0=0, and v3 == 4*v2 hold; v2/mode/flag1/v4 vary by type.
PF_FIELDS = ('v0', 'v1', 'flag0', 'v2', 'v3', 'flag1', 'mode', 'v4')
GENERIC_PATH_FOLLOW = {'v0': 0.55, 'v1': 0.55, 'flag0': 0, 'v2': 0.5, 'v3': 2.0, 'flag1': 0, 'mode': 1, 'v4': 1.0}
TYPE_PATH_FOLLOW = {
    'frog_lab1':            {'v0': 0.55, 'v1': 0.55, 'flag0': 0, 'v2': 1.2,  'v3': 4.8, 'flag1': 0, 'mode': 1, 'v4': 1.0},
    'rat_lab1':             {'v0': 0.55, 'v1': 0.55, 'flag0': 0, 'v2': 1.2,  'v3': 4.8, 'flag1': 1, 'mode': 1, 'v4': 1.0},
    'rat_lab2':             {'v0': 0.55, 'v1': 0.55, 'flag0': 0, 'v2': 0.8,  'v3': 3.2, 'flag1': 0, 'mode': 2, 'v4': 0.5},
    'simon_lab1':           {'v0': 0.55, 'v1': 0.55, 'flag0': 0, 'v2': 1.2,  'v3': 4.8, 'flag1': 1, 'mode': 1, 'v4': 0.7},
    'scientist_young_lab2': {'v0': 0.55, 'v1': 0.55, 'flag0': 0, 'v2': 1.6,  'v3': 6.4, 'flag1': 0, 'mode': 1, 'v4': 1.0},
    'scientist_old_lab2':   {'v0': 0.55, 'v1': 0.55, 'flag0': 0, 'v2': 1.6,  'v3': 6.4, 'flag1': 0, 'mode': 1, 'v4': 1.0},
    'watusi_egypt1':        {'v0': 0.55, 'v1': 0.55, 'flag0': 0, 'v2': 0.4,  'v3': 1.6, 'flag1': 1, 'mode': 2, 'v4': 1.0},
    'alphadon_dino1':       {'v0': 0.55, 'v1': 0.55, 'flag0': 0, 'v2': 0.5,  'v3': 2.0, 'flag1': 0, 'mode': 1, 'v4': 0.5},
    'agomphus_dino2':       {'v0': 0.55, 'v1': 0.55, 'flag0': 0, 'v2': 0.25, 'v3': 1.0, 'flag1': 0, 'mode': 1, 'v4': 1.0},
}

def make_path_follow(name, etype=None, level=None):
    """Build a path_follow block for an entity. Params are resolved best-first:
    (1) copy an existing same-type follower already in this level, (2) a per-type table
    based on the existing levels, (3) a generic fallback."""
    params = None
    if level:
        for L in level.get('layers', []):
            for e in L.get('entities', []):
                if (e.get('type') == etype and e.get('has_path_follow') == 1 and 'path_follow' in e):
                    pf = e['path_follow']; params = {k: pf[k] for k in PF_FIELDS if k in pf}; break
            if params: break
    if params is None:
        params = dict(TYPE_PATH_FOLLOW.get(etype, GENERIC_PATH_FOLLOW))
    params = dict(params); params['path_name'] = name
    return params

# Emitter block: 11 doubles v0..v10, plus reserved+end_marker ints (stored only on
# layers >= 3). based on existing levels: v0=v1=1.0, v2=v7=v8=0 always; v9<v10 (a min/max pair);
# end_marker 1..20; v4/v5/v6 are 0 or 0.05. Meanings below are assumed, take with a grain of salt.
GENERIC_EMITTER = {'v0': 1.0, 'v1': 1.0, 'v2': 0.0, 'v3': 0.0, 'v4': 0.0, 'v5': 0.0, 'v6': 0.0,
                   'v7': 0.0, 'v8': 0.0, 'v9': 5.0, 'v10': 10.0, 'reserved': 0, 'end_marker': 5}
TYPE_EMITTER = {   # overlaid on top of GENERIC_EMITTER
    'frog_lab1':      {'v9': 1.0,  'v10': 2.0,  'end_marker': 1},
    'rat_lab2':       {'v3': 200.0, 'v9': 3.0, 'v10': 5.0, 'end_marker': 16},
    'simon_lab1':     {'v9': 5.0,  'v10': 10.0, 'end_marker': 1},
    'alphadon_dino1': {'v4': 0.05, 'v5': 0.05, 'v6': 0.05, 'v9': 5.0,  'v10': 8.0,  'end_marker': 20},
    'agomphus_dino2': {'v9': 10.0, 'v10': 20.0, 'end_marker': 10},
    'watusi_egypt1':  {'v4': 0.05, 'v5': 0.05, 'v6': 0.05, 'v9': 10.0, 'v10': 12.0, 'end_marker': 10},
}

def make_emitter(etype=None, level=None):
    """Build an emitter block. Prefer copying an existing same-type emitter in this level,
    else a per-type default, else a generic spawner."""
    if level:
        for L in level.get('layers', []):
            for e in L.get('entities', []):
                if e.get('type') == etype and e.get('has_emitter') == 1 and 'emitter' in e:
                    return dict(e['emitter'])
    em = dict(GENERIC_EMITTER); em.update(TYPE_EMITTER.get(etype, {}))
    return em

def recompute_path(path):
    """Recompute smooth tangents (p1 = -p2), position (bbox center of anchors) and extent from the
    p0 anchor points, matching the shape of shipped paths so the game follows it smoothly."""
    sps = path.get('spline_points', [])
    n = len(sps)
    if n == 0: return
    xs = [sp['p0'][0] for sp in sps]; ys = [sp['p0'][1] for sp in sps]
    for i, sp in enumerate(sps):
        prev = sps[i - 1]['p0'] if i > 0 else sp['p0']
        nxt = sps[i + 1]['p0'] if i < n - 1 else sp['p0']
        tx = (nxt[0] - prev[0]) * 0.25; ty = (nxt[1] - prev[1]) * 0.25
        sp['p2'] = [tx, ty]; sp['p1'] = [-tx, -ty]
    minx, maxx = min(xs), max(xs); miny, maxy = min(ys), max(ys)
    path['position'] = [(minx + maxx) / 2, (miny + maxy) / 2]
    path['extent_x_guess'] = maxx - minx; path['extent_y_guess'] = maxy - miny


# (field key, label, is_int, tooltip) — labels are best-guesses.
EMITTER_FIELDS = [
    ('end_marker', "count to spawn (?)", True,  "Guess: how many spawn before stopping (1..20 in game). Stored only on layers ≥ 3."),
    ('v9',  "interval min (?)",  False, "Guess: shortest gap between spawns."),
    ('v10', "interval max (?)",  False, "Guess: longest gap between spawns. Always ≥ interval min in shipped levels."),
    ('v4',  "spread A (?)",      False, "Small jitter; 0 or 0.05 in shipped levels."),
    ('v5',  "spread B (?)",      False, "Small jitter; 0 or 0.05 in shipped levels."),
    ('v6',  "spread C (?)",      False, "Small jitter; 0 or 0.05 in shipped levels."),
    ('v3',  "range? (usually 0)",False, "Usually 0; one rat group used 200."),
    ('v0',  "v0 (always 1.0)",   False, "Always 1.0 in shipped levels — likely an enable/scale flag."),
    ('v1',  "v1 (always 1.0)",   False, "Always 1.0 in shipped levels."),
    ('v2',  "v2 (always 0)",     False, "Always 0 in shipped levels."),
    ('v7',  "v7 (always 0)",     False, "Always 0 in shipped levels."),
    ('v8',  "v8 (always 0)",     False, "Always 0 in shipped levels."),
    ('reserved', "reserved (0)", True,  "Always 0 in shipped levels."),
]

def new_multilevel_model():
    ml = TM.MultiLevel()
    ml.attrs = {'timelimit': '0', 'victorytype': '0', 'numspecialentities': '0',
                'goldtime': '0', 'silvertime': '0', 'bronzetime': '0', 'smallfailstring': '',
                'tipscriptfunction': '', 'comicstartfunction': '', 'comicendfunction': '',
                'levelmusicscript': 'levelmusic_default'}
    ml.stages = []
    return ml


BG="#1b1d22"; PANEL="#24272e"; PANEL2="#2c3038"; TEXT="#e6e8ec"; SUBTLE="#9aa0aa"
ACCENT="#5b8cff"; HOVER="#7aa2ff"; SELECT="#ffd166"; PATHC="#33d6c8"; WALLC="#ff9f43"
QSS = f"""
* {{ font-family: "Segoe UI","Inter",system-ui; font-size: 10pt; color: {TEXT}; }}
QMainWindow, QWidget {{ background: {BG}; }}
QDockWidget::title {{ background: {PANEL2}; padding: 8px 12px; border-top-left-radius: 10px;
    border-top-right-radius: 10px; font-weight: 600; }}
QDockWidget > QWidget {{ background: {PANEL}; }}
QGraphicsView {{ background: {BG}; border: none; }}
QLabel {{ background: transparent; }}
QPushButton {{ background: {PANEL2}; border: 1px solid #3a3f49; border-radius: 9px; padding: 7px 12px; }}
QPushButton:hover {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QPushButton:pressed {{ background: {HOVER}; }}
QPushButton:checked {{ background: {ACCENT}; border-color: {ACCENT}; color: white; }}
QLineEdit, QSpinBox, QDoubleSpinBox {{ background: {PANEL2}; border: 1px solid #3a3f49;
    border-radius: 8px; padding: 5px 8px; selection-background-color: {ACCENT}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {ACCENT}; }}
QListWidget {{ background: {PANEL2}; border: 1px solid #3a3f49; border-radius: 10px; padding: 4px; outline: none; }}
QListWidget::item {{ padding: 7px 9px; border-radius: 7px; }}
QListWidget::item:hover {{ background: #333845; }}
QListWidget::item:selected {{ background: {ACCENT}; color: white; }}
QCheckBox {{ spacing: 8px; padding: 3px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1px solid #4a505c; background: {PANEL2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QMenuBar {{ background: {BG}; }}
QMenuBar::item:selected {{ background: {PANEL2}; border-radius: 6px; }}
QMenu {{ background: {PANEL}; border: 1px solid #3a3f49; border-radius: 8px; padding: 5px; }}
QMenu::item {{ padding: 6px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: {ACCENT}; color: white; }}
QStatusBar {{ background: {PANEL}; color: {SUBTLE}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #3a3f49; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""


def pil_to_qpixmap(im):
    im = im.convert("RGBA")
    qimg = QImage(im.tobytes("raw", "RGBA"), im.width, im.height, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimg.copy())


class Bus(QObject):
    selected = Signal(object)


def entity_render_scale(mass, w, h):
    # Tested on lab2a, so this block references assets used there.
    """Area-based scale (rendered area = mass) with an elongation boost shaped like a hump:
    compact sprites (candy, oscilloscope) use the plain area model; the boost ramps up to BMAX
    at the caliper's aspect (~3.2), then decays for more-elongated sprites (test tube, eyedropper)
    so they don't run away. Tunable knobs: A_LO/A_PEAK/BMAX/DECAY."""
    w = max(w, 1); h = max(h, 1)
    asp = max(w, h) / min(w, h)
    A_LO, A_PEAK, BMAX, DECAY = 2.1, 3.2, 1.8, 0.38
    if asp <= A_LO:
        boost = 1.0
    elif asp <= A_PEAK:
        boost = 1.0 + (asp - A_LO) / (A_PEAK - A_LO) * (BMAX - 1.0)
    else:
        boost = max(1.0, BMAX - DECAY * (asp - A_PEAK))
    return math.sqrt(max(mass, 1.0) / (w * h)) * boost


class SpriteItem(QGraphicsPixmapItem):
    """Sprite with rounded hover/selection highlight and commit-on-move."""
    def __init__(self, kind, ref, layer_idx, win):
        super().__init__()
        self.kind = kind; self.ref = ref; self.layer_idx = layer_idx; self.win = win
        self._hover = False; self._press = None
        self.setAcceptHoverEvents(True)
        self.setTransformationMode(Qt.SmoothTransformation)

    def hoverEnterEvent(self, e): self._hover = True; self.update()
    def hoverLeaveEvent(self, e): self._hover = False; self.update()

    def mousePressEvent(self, e):
        self._press = self.pos(); super().mousePressEvent(e)
    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e)
        if self._press is not None and self.pos() != self._press:
            self.win.commit()
        self._press = None

    def paint(self, painter, option, widget=None):
        option.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, option, widget)
        if self._hover or self.isSelected():
            c = QColor(SELECT) if self.isSelected() else QColor(HOVER)
            pen = QPen(c); pen.setCosmetic(True); pen.setWidthF(2.0)
            painter.setPen(pen); painter.setBrush(Qt.NoBrush)
            r = self.boundingRect(); rad = max(min(r.width(), r.height()) * 0.08, 1.0)
            painter.drawRoundedRect(r, rad, rad)


class EntityItem(SpriteItem):
    def __init__(self, ent, layer_idx, win):
        super().__init__('entity', ent, layer_idx, win)
        self.setFlags(QGraphicsPixmapItem.ItemIsMovable | QGraphicsPixmapItem.ItemIsSelectable |
                      QGraphicsPixmapItem.ItemSendsGeometryChanges)
        self.frame = 0
        self.fcount = win.assets.frame_count(ent['type'])
        self.px, self.py = win.assets.pivot(ent['type'])
        self.rebuild(); self.place()

    def rgba(self): return tuple(self.ref['color']['rgba'])

    def rebuild(self):
        spr = self.win.sprite_pixmap(self.ref['type'], self.rgba(), self.frame)
        self._resolved = spr is not None
        if spr is None:
            pm = QPixmap(40, 40); pm.fill(Qt.transparent)
            p = QPainter(pm); p.setRenderHint(QPainter.Antialiasing)
            p.setBrush(QColor(*self.rgba()[:3])); p.setPen(QPen(QColor(255, 0, 255), 2))
            p.drawEllipse(3, 3, 34, 34); p.end(); self.setPixmap(pm); self.w = self.h = 40
        else:
            self.setPixmap(spr); self.w = spr.width(); self.h = spr.height()
        self.setOffset(-self.px * self.w, -self.py * self.h)

    def place(self):
        if self._resolved:
            scale = entity_render_scale(self.ref.get('mass', 1.0), self.w, self.h)
        else:
            scale = 1.0
        self.setScale(scale)
        self.setRotation(-self.ref['rotation']['raw'] / 100.0)   # rotation = centidegrees
        self.setZValue(1000 + self.layer_idx * 1000 + self.ref.get('priority', 0) * 0.001)
        self.setPos(self.ref['position'][0] * ENTITY_K, self.ref['position'][1] * ENTITY_K)

    def itemChange(self, change, value):
        if change == QGraphicsPixmapItem.ItemPositionHasChanged and self.scene():
            self.ref['position'] = [value.x() / ENTITY_K, value.y() / ENTITY_K]
            if self.isSelected(): self.win.inspector.refresh_pos(self)
        return super().itemChange(change, value)


class DecoItem(SpriteItem):
    def __init__(self, deco, layer_idx, win, name, editable):
        super().__init__('deco', deco, layer_idx, win)
        self._name = name
        spr = win.sprite_pixmap(name, tuple(deco.get('color_rgba', [255, 255, 255, 255])), 0) if name else None
        self._resolved = spr is not None
        if spr is None:
            self.w = self.h = 1; return
        self.setPixmap(spr); self.w = spr.width(); self.h = spr.height()
        self.setOffset(-self.w / 2, -self.h / 2)
        self.setZValue(self.layer_idx * 1000 + deco.get('priority', {}).get('total', 0) * 0.001)
        self.apply()
        self.set_editable(editable)

    def apply(self):
        d = self.ref
        dims = d.get('dimensions', {}).get('raw', [DIM_BASE, DIM_BASE])
        self.setScale(max((dims[0] or DIM_BASE) / DIM_BASE, 0.001))
        self.setRotation(-d.get('size', {}).get('raw', 0) / 100.0)   # 'size' = centidegrees
        self.setPos(d['position'][0] * DECO_K, d['position'][1] * DECO_K)

    def rebuild(self):
        if not self._resolved: return
        spr = self.win.sprite_pixmap(self._name, tuple(self.ref.get('color_rgba', [255, 255, 255, 255])), 0)
        if spr is None: return
        self.setPixmap(spr); self.w = spr.width(); self.h = spr.height()
        self.setOffset(-self.w / 2, -self.h / 2)

    def set_editable(self, on):
        self.setFlag(QGraphicsPixmapItem.ItemIsMovable, on)
        self.setFlag(QGraphicsPixmapItem.ItemIsSelectable, on)
        self.setFlag(QGraphicsPixmapItem.ItemSendsGeometryChanges, on)
        self.setAcceptedMouseButtons(Qt.AllButtons if on else Qt.NoButton)
        self.setAcceptHoverEvents(on)

    def itemChange(self, change, value):
        if change == QGraphicsPixmapItem.ItemPositionHasChanged and self.scene():
            self.ref['position'] = [value.x() / DECO_K, value.y() / DECO_K]
            if self.isSelected(): self.win.inspector.refresh_pos(self)
        return super().itemChange(change, value)


class PathItem(QGraphicsPathItem):
    def __init__(self, path, layer_idx, win=None):
        super().__init__(); self.ref = path; self.layer_idx = layer_idx; self.win = win
        pen = QPen(QColor(PATHC)); pen.setCosmetic(True); pen.setWidthF(2.0); self.setPen(pen)
        self.setZValue(5000 + layer_idx * 1000); self.setToolTip(f"path: {path.get('path_name')} (double-click to add a point)")
        self.refresh()
    def refresh(self):
        pts = [QPointF(sp['p0'][0], sp['p0'][1]) for sp in self.ref.get('spline_points', [])]
        pp = QPainterPath()
        if pts:
            pp.moveTo(pts[0])
            for q in pts[1:]: pp.lineTo(q)
        self.setPath(pp)
    def mouseDoubleClickEvent(self, e):
        if self.win is not None:
            self.win._path_add_point_at(self.ref, e.scenePos().x(), e.scenePos().y()); e.accept()
        else:
            super().mouseDoubleClickEvent(e)


class WaypointItem(QGraphicsEllipseItem):
    R = 7
    def __init__(self, path, idx, path_item, win):
        super().__init__(-self.R, -self.R, 2*self.R, 2*self.R)
        self.ref = path; self.idx = idx; self.path_item = path_item; self.win = win
        self.setBrush(QBrush(QColor(PATHC)))
        pen = QPen(QColor("white")); pen.setCosmetic(True); pen.setWidthF(1.5); self.setPen(pen)
        self.setZValue(6000 + path_item.layer_idx * 1000)
        # manual drag (ItemIsMovable + ItemIgnoresTransformations is broken in Qt)
        self.setFlag(QGraphicsEllipseItem.ItemIgnoresTransformations)
        self.setToolTip("drag to move · double-click to remove")
        self.setPos(path['spline_points'][idx]['p0'][0], path['spline_points'][idx]['p0'][1])
        self._drag = False; self._moved = False
    def mousePressEvent(self, e):
        self._drag = True; self._moved = False; e.accept()
    def mouseMoveEvent(self, e):
        if self._drag:
            p = e.scenePos(); self.setPos(p); self._moved = True
            self.ref['spline_points'][self.idx]['p0'] = [p.x(), p.y()]
            self.win._recompute_path(self.ref); self.path_item.refresh()
            self.win._last_path = self.ref; e.accept()
        else:
            super().mouseMoveEvent(e)
    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            if self._moved: self.win.commit()
            e.accept()
        else:
            super().mouseReleaseEvent(e)
    def mouseDoubleClickEvent(self, e):
        self.win._path_remove_point(self.ref, self.idx); e.accept()


class BoundaryHandle(QGraphicsRectItem):
    """A draggable corner of the level boundary. Corner ids: 0=TL 1=TR 2=BR 3=BL.
    Dragging a corner moves the two edges it touches; the boundary stays a rectangle."""
    R = 6
    def __init__(self, corner, win):
        super().__init__(-self.R, -self.R, 2*self.R, 2*self.R)
        self.corner = corner; self.win = win
        self.setBrush(QBrush(QColor(SELECT)))
        pen = QPen(QColor("white")); pen.setCosmetic(True); pen.setWidthF(1.5); self.setPen(pen)
        self.setZValue(20000)
        # NOTE: ItemIsMovable + ItemIgnoresTransformations is broken in Qt (the item won't follow
        # the cursor), so we drive the drag by hand from the mouse events instead.
        self.setFlag(QGraphicsRectItem.ItemIgnoresTransformations)
        self.setCursor(Qt.SizeAllCursor); self.setToolTip("drag to resize the level boundary")
        self._drag = False; self._moved = False
    def mousePressEvent(self, e):
        self._drag = True; self._moved = False; e.accept()
    def mouseMoveEvent(self, e):
        if self._drag:
            p = e.scenePos(); self.setPos(p); self._moved = True
            self.win._edge_from_handle(self.corner, p.x(), p.y()); e.accept()
        else:
            super().mouseMoveEvent(e)
    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            if self._moved: self.win._persist_edges()
            e.accept()
        else:
            super().mouseReleaseEvent(e)



class WallItem(QGraphicsPathItem):
    """A collision wall — an arbitrary polygon. Stored normalized (x spans ±0.5, uniform scale by
    `width`; `length` = world height). Movable; per-vertex editable when wall editing is on.
    Double-click an edge to add a point; double-click a vertex handle to remove it."""
    def __init__(self, wall, layer_idx, win, editable):
        super().__init__()
        self.ref = wall; self.layer_idx = layer_idx; self.win = win; self.editable = editable
        if editable:
            self.setFlags(QGraphicsPathItem.ItemIsMovable | QGraphicsPathItem.ItemIsSelectable |
                          QGraphicsPathItem.ItemSendsGeometryChanges)
        else:
            self.setAcceptedMouseButtons(Qt.NoButton)      # clicks pass through to objects beneath
        self.setZValue(4000 + layer_idx * 1000)
        self.rebuild()

    def _shape0(self):
        for sh in self.ref.get('shapes', []):
            if 'vertices' in sh.get('data', {}): return sh
        return None

    def world_verts(self):
        sh = self._shape0()
        if not sh: return []
        w = self.ref.get('width', 1.0) or 1.0
        cx = self.ref.get('pos_x', 0.0); cy = self.ref.get('pos_y', 0.0)
        return [[cx + nx * w, cy + ny * w] for nx, ny in sh['data']['vertices']]

    def set_world_verts(self, wv):
        sh = self._shape0()
        if not sh or len(wv) < 3: return
        xs = [p[0] for p in wv]; ys = [p[1] for p in wv]
        minx, maxx = min(xs), max(xs); miny, maxy = min(ys), max(ys)
        width = max(maxx - minx, 1.0); cx = (minx + maxx) / 2; cy = (miny + maxy) / 2
        sh['data']['vertices'] = [[(x - cx) / width, (y - cy) / width] for x, y in wv]
        self.ref['pos_x'] = cx; self.ref['pos_y'] = cy
        self.ref['width'] = width; self.ref['length'] = maxy - miny
        self.rebuild()

    def move_vertex(self, i, wx, wy):
        wv = self.world_verts()
        if 0 <= i < len(wv): wv[i] = [wx, wy]; self.set_world_verts(wv)

    def remove_vertex(self, i):
        wv = self.world_verts()
        if len(wv) > 3 and 0 <= i < len(wv):
            del wv[i]; self.set_world_verts(wv); return True
        return False

    def add_vertex_near(self, x, y):
        wv = self.world_verts()
        if len(wv) < 2: return
        best, bd = 0, 1e30
        for i in range(len(wv)):
            a = wv[i]; b = wv[(i + 1) % len(wv)]
            d = _point_seg_dist(x, y, a, b)
            if d < bd: bd, best = d, i
        wv.insert(best + 1, [x, y]); self.set_world_verts(wv)

    def rebuild(self):
        w = self.ref.get('width', 1.0) or 1.0
        pp = QPainterPath()
        for sh in self.ref.get('shapes', []):
            verts = sh.get('data', {}).get('vertices')
            if verts and len(verts) >= 2:
                pp.addPolygon(QPolygonF([QPointF(vx * w, vy * w) for vx, vy in verts])); pp.closeSubpath()
        self.setPath(pp)
        block = self.ref.get('wall_type_name', 'everything')
        col = QColor(PATHC) if block == 'goo_only' else QColor(WALLC)
        pen = QPen(col); pen.setCosmetic(True); pen.setWidthF(2.0); self.setPen(pen)
        fill = QColor(col); fill.setAlpha(55); self.setBrush(fill)
        self.win._wall_block = True
        self.setPos(self.ref.get('pos_x', 0.0), self.ref.get('pos_y', 0.0))
        self.win._wall_block = False

    def itemChange(self, change, value):
        if (change == QGraphicsPathItem.ItemPositionHasChanged and self.scene()
                and not getattr(self.win, '_wall_block', False)):
            self.ref['pos_x'] = value.x(); self.ref['pos_y'] = value.y()
            self.win._update_wall_handles()
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, e):
        super().mouseReleaseEvent(e); self.win.commit()

    def mouseDoubleClickEvent(self, e):
        if self.editable:
            self.add_vertex_near(e.scenePos().x(), e.scenePos().y())
            self.win._rebuild_wall_handles(); self.win.commit()
            self.win.statusBar().showMessage("added a point to the wall — drag it; double-click a point to remove")
        else:
            super().mouseDoubleClickEvent(e)


def _point_seg_dist(px, py, a, b):
    ax, ay = a; bx, by = b; dx = bx - ax; dy = by - ay
    if dx == 0 and dy == 0: return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


class WallVertexHandle(QGraphicsRectItem):
    """A draggable polygon vertex of the selected wall. Double-click removes it (min 3)."""
    R = 5
    def __init__(self, index, wall_item, win):
        super().__init__(-self.R, -self.R, 2 * self.R, 2 * self.R)
        self.index = index; self.wall = wall_item; self.win = win
        self.setBrush(QBrush(QColor(SELECT)))
        pen = QPen(QColor("white")); pen.setCosmetic(True); pen.setWidthF(1.5); self.setPen(pen)
        self.setZValue(21000)
        self.setFlag(QGraphicsRectItem.ItemIgnoresTransformations)   # constant on-screen size
        self.setCursor(Qt.SizeAllCursor); self.setToolTip("drag to reshape · double-click to remove")
        self._drag = False; self._moved = False
    def mousePressEvent(self, e):
        self._drag = True; self._moved = False; e.accept()
    def mouseMoveEvent(self, e):
        if self._drag:
            p = e.scenePos(); self.setPos(p); self._moved = True
            self.wall.move_vertex(self.index, p.x(), p.y()); e.accept()
        else:
            super().mouseMoveEvent(e)
    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            if self._moved: self.win.commit()
            e.accept()
        else:
            super().mouseReleaseEvent(e)
    def mouseDoubleClickEvent(self, e):
        if self.wall.remove_vertex(self.index):
            self.win._rebuild_wall_handles(); self.win.commit()
            self.win.statusBar().showMessage("removed a wall point"); e.accept()


class Canvas(QGraphicsView):
    def __init__(self, scene, win):
        super().__init__(scene)
        self.win = win
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setBackgroundBrush(QColor(BG))
        self._panning = False
        self._pan_last = None
        self._drag_before_pan = QGraphicsView.RubberBandDrag

    def mousePressEvent(self, e):
        if e.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_last = e.pos()
            self._drag_before_pan = self.dragMode()
            self.setDragMode(QGraphicsView.NoDrag)
            self.setCursor(Qt.ClosedHandCursor)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._panning and self._pan_last is not None:
            d = e.pos() - self._pan_last
            self._pan_last = e.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - d.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - d.y())
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self._pan_last = None
            self.setDragMode(self._drag_before_pan)
            self.setCursor(Qt.ArrowCursor)
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def contextMenuEvent(self, e):
        self.win.open_canvas_context_menu(e.pos(), e.globalPos())
        e.accept()

    def wheelEvent(self, e):
        s = 1.15 if e.angleDelta().y() > 0 else 1/1.15
        self.scale(s, s)


class LayersDock(QDockWidget):
    TYPES = ['decorations', 'entities', 'paths', 'walls']
    def __init__(self, win):
        super().__init__("Layers", win); self.win = win
        w = QWidget(); col = QVBoxLayout(w); col.setSpacing(10)
        col.addWidget(QLabel("Layer visibility"))
        self.layer_box = QVBoxLayout(); lw = QWidget(); lw.setLayout(self.layer_box); col.addWidget(lw)
        ln = QFrame(); ln.setFrameShape(QFrame.HLine); ln.setStyleSheet("color:#3a3f49"); col.addWidget(ln)
        col.addWidget(QLabel("Object types"))
        self.type_checks = {}
        for t in self.TYPES:
            c = QCheckBox(t.capitalize()); c.setChecked(True); c.toggled.connect(self.win.apply_visibility)
            self.type_checks[t] = c; col.addWidget(c)
        self.dim_bg = QCheckBox("Dim background layers"); self.dim_bg.toggled.connect(self.win.apply_visibility); col.addWidget(self.dim_bg)
        ln2 = QFrame(); ln2.setFrameShape(QFrame.HLine); ln2.setStyleSheet("color:#3a3f49"); col.addWidget(ln2)
        self.bg_edit = QPushButton("Edit background"); self.bg_edit.setCheckable(True)
        self.bg_edit.setToolTip("Allow selecting/moving decorations (off by default to avoid accidents)")
        self.bg_edit.toggled.connect(self.win.set_bg_editable); col.addWidget(self.bg_edit)
        col.addStretch(1); self.setWidget(w); self.layer_checks = []
    def rebuild(self, names):
        for c in self.layer_checks: c.setParent(None)
        self.layer_checks = []
        for i, nm in enumerate(names):
            c = QCheckBox(f"{i}  ·  {nm}"); c.setChecked(True); c.toggled.connect(self.win.apply_visibility)
            self.layer_box.addWidget(c); self.layer_checks.append(c)


class Inspector(QDockWidget):
    def __init__(self, win):
        super().__init__("Inspector", win); self.win = win; self.item = None; self._block = False
        w = QWidget(); form = QFormLayout(w); form.setSpacing(9)
        self.title = QLabel("Select an object"); self.title.setStyleSheet(f"font-weight:600;color:{ACCENT}")
        self.sx = QDoubleSpinBox(); self.sx.setRange(-1e6, 1e6); self.sx.setDecimals(1)
        self.sy = QDoubleSpinBox(); self.sy.setRange(-1e6, 1e6); self.sy.setDecimals(1)
        self.rot = QSpinBox(); self.rot.setRange(-180, 180); self.rot.setSuffix("°")
        self.size_spin = QDoubleSpinBox(); self.size_spin.setRange(1, 5e6); self.size_spin.setDecimals(0)
        self.size_label = QLabel("mass (size)")
        self.eat = QLineEdit(); self.eat.setPlaceholderText("—")
        self.swatch = QPushButton("color"); self.swatch.clicked.connect(self._pick)
        self.kind = QComboBox(); self.kind.addItems(["entity (eaten)", "decoration (background)"])
        self.kind.currentIndexChanged.connect(self._kind_changed)
        self.layer = QComboBox(); self.layer.currentIndexChanged.connect(self._layer_changed)
        self.path = QComboBox(); self.path.currentIndexChanged.connect(self._path_changed)
        self.path.setToolTip("Assign this entity to a path in its layer: it spawns at the path and follows it.")
        self.persist = QCheckBox("persists to next stage")
        self.persist.setToolTip("Whether this entity carries over to the next multilevel stage (field_158). "
                                "New entities now inherit the level's setting; copied entities keep theirs.")
        self.persist.stateChanged.connect(self._persist_toggled)
        self.emit = QCheckBox("emits copies (spawner)")
        self.emit.setToolTip("Periodically spawn copies of this entity (has_emitter). Shipped emitters all sit on "
                             "Main / Main Bottom layers — keep spawners there.")
        self.emit.stateChanged.connect(self._emit_toggled)
        self.emit_count = QSpinBox(); self.emit_count.setRange(1, 999)
        self.emit_count.setToolTip("How many spawn before the emitter stops (end_marker).")
        self.emit_min = QDoubleSpinBox(); self.emit_min.setRange(0.0, 999.0); self.emit_min.setDecimals(1)
        self.emit_max = QDoubleSpinBox(); self.emit_max.setRange(0.0, 999.0); self.emit_max.setDecimals(1)
        self.emit_min.setToolTip("Shortest gap between spawns, seconds (v9).")
        self.emit_max.setToolTip("Longest gap between spawns, seconds (v10).")
        for sb in (self.emit_count, self.emit_min, self.emit_max): sb.valueChanged.connect(self._emit_changed)
        ivl = QHBoxLayout(); ivl.setContentsMargins(0, 0, 0, 0)
        ivl.addWidget(self.emit_min); ivl.addWidget(QLabel("–")); ivl.addWidget(self.emit_max)
        form.addRow(self.title); form.addRow("X", self.sx); form.addRow("Y", self.sy)
        form.addRow("rotation", self.rot); form.addRow(self.size_label, self.size_spin)
        form.addRow("eat size (cm)", self.eat)
        form.addRow("color", self.swatch); form.addRow("type", self.kind); form.addRow("layer", self.layer)
        form.addRow("follow path", self.path)
        form.addRow("multilevel", self.persist)
        form.addRow(self.emit)
        form.addRow("↳ count", self.emit_count)
        form.addRow("↳ interval s", ivl)
        _sc = QScrollArea(); _sc.setWidgetResizable(True); _sc.setWidget(w); self.setWidget(_sc)
        for sb in (self.sx, self.sy, self.rot, self.size_spin):
            sb.valueChanged.connect(self._edit); sb.editingFinished.connect(self.win.commit)
        self.size_spin.valueChanged.connect(self._refresh_eat)
        self.eat.editingFinished.connect(self._eat_to_mass)

    def _refill_layers(self, current_idx):
        self._block = True
        self.layer.clear()
        self.layer.addItems(getattr(self.win, 'layer_names', []) or
                            [f"Layer {i}" for i in range(len(self.win.level.get('layers', [])))])
        if 0 <= current_idx < self.layer.count(): self.layer.setCurrentIndex(current_idx)
        self.layer.setEnabled(True)
        self._block = False

    def _refresh_eat(self, *_):
        # eat size (cm) = object's own diameter = 2*sqrt(mass/pi)*meterperpix*100
        mpp = getattr(self.win, '_mpp', None)
        if isinstance(self.item, EntityItem) and mpp:
            mass = max(self.size_spin.value(), 1.0)
            cm = 2 * math.sqrt(mass / math.pi) * mpp * 100
            self._block_eat = True; self.eat.setText(f"{cm:.2f}"); self.eat.setEnabled(True); self._block_eat = False
        else:
            self._block_eat = True
            self.eat.setText(""); self.eat.setEnabled(False)
            self.eat.setToolTip("" if mpp else "needs a linked multilevel (meterperpix) to show cm")
            self._block_eat = False

    def _eat_to_mass(self):
        if getattr(self, '_block_eat', False) or not isinstance(self.item, EntityItem): return
        mpp = getattr(self.win, '_mpp', None)
        try: cm = float(self.eat.text())
        except (TypeError, ValueError): return
        if not mpp or cm <= 0: return
        mass = math.pi * (cm / (200 * mpp)) ** 2
        self.size_spin.setValue(mass)          # triggers _edit (updates mass + re-place) + refresh
        self.win.commit()

    def _layer_paths(self):
        """Path dicts in the selected item's layer (entities can only follow paths in their own layer)."""
        it = self.item
        if it is None or self.win.level is None: return []
        layers = self.win.level.get('layers', [])
        if not (0 <= it.layer_idx < len(layers)): return []
        return layers[it.layer_idx].get('paths', [])

    def _refresh_paths(self):
        self._block_path = True
        self.path.clear()
        if not isinstance(self.item, EntityItem):
            self.path.setEnabled(False); self.path.addItem("— entities only —", None)
            self._block_path = False; return
        paths = [p for p in self._layer_paths() if p.get('path_name')]
        self.path.addItem("(none)", None)
        for p in paths:
            self.path.addItem(p['path_name'], p['path_name'])
        e = self.item.ref
        cur = e.get('path_follow', {}).get('path_name') if e.get('has_path_follow') == 1 else None
        idx = next((i for i in range(self.path.count()) if self.path.itemData(i) == cur), 0)
        self.path.setCurrentIndex(idx)
        self.path.setEnabled(True)
        if not paths:
            self.path.setEnabled(False); self.path.setToolTip("This layer has no paths to follow.")
        else:
            self.path.setToolTip("Assign this entity to a path: it snaps to the path and follows it.")
        self._block_path = False

    def _path_changed(self, idx):
        if getattr(self, '_block_path', False) or self._block or not isinstance(self.item, EntityItem): return
        e = self.item.ref
        name = self.path.itemData(idx)
        if not name:
            e['has_path_follow'] = 0; e.pop('path_follow', None)
            self.win.statusBar().showMessage("path follow removed")
        else:
            e['has_path_follow'] = 1
            e['path_follow'] = make_path_follow(name, e.get('type'), self.win.level)
            p = next((p for p in self._layer_paths() if p.get('path_name') == name), None)
            if p:                                  # spawn the entity on the path's start point
                sp = p.get('spline_points')
                start = (sp[0]['p0'] if sp else p.get('position', [0, 0]))
                e['position'] = [start[0] / ENTITY_K, start[1] / ENTITY_K]
                self.item.place(); self.refresh_pos(self.item)
            self.win.statusBar().showMessage(f"following path '{name}'")
        self.win.commit()

    def _persist_toggled(self, *_):
        if self._block or not isinstance(self.item, EntityItem): return
        self.item.ref['field_158'] = 1 if self.persist.isChecked() else 0
        self.win.commit()

    def _emit_toggled(self, *_):
        if self._block or not isinstance(self.item, EntityItem): return
        e = self.item.ref
        if self.emit.isChecked():
            e['has_emitter'] = 1
            if 'emitter' not in e: e['emitter'] = make_emitter(e.get('type'), self.win.level)
        else:
            e['has_emitter'] = 0; e.pop('emitter', None)
        self._block = True; self._fill_emit(e); self._block = False
        self.win.commit()

    def _emit_changed(self, *_):
        if self._block or not isinstance(self.item, EntityItem): return
        e = self.item.ref
        if e.get('has_emitter') == 1 and 'emitter' in e:
            em = e['emitter']
            em['end_marker'] = int(self.emit_count.value())
            em['v9'] = float(self.emit_min.value()); em['v10'] = float(self.emit_max.value())
            self.win.commit()

    def _fill_emit(self, e):
        on = e.get('has_emitter') == 1
        self.emit.setEnabled(True); self.emit.setChecked(on)
        em = e.get('emitter', {}) if on else {}
        self.emit_count.setValue(int(em.get('end_marker', 5)))
        self.emit_min.setValue(float(em.get('v9', 5.0))); self.emit_max.setValue(float(em.get('v10', 10.0)))
        for sb in (self.emit_count, self.emit_min, self.emit_max): sb.setEnabled(on)

    def _layer_changed(self, idx):
        if self._block or self.item is None or idx < 0: return
        self.win.move_selection_to_layer(idx)        # applies to every selected object, not just this one

    def _kind_changed(self, idx):
        if self._block or self.item is None: return
        self.win.convert_object(self.item, 'entity' if idx == 0 else 'deco')

    @staticmethod
    def _wrap(deg):
        d = int(round(deg)); return (d + 180) % 360 - 180

    def bind(self, item):
        self.item = item; self._block = True
        if isinstance(item, EntityItem):
            e = item.ref; self.title.setText(e['type'])
            self.sx.setValue(e['position'][0] * ENTITY_K); self.sy.setValue(e['position'][1] * ENTITY_K)
            self.rot.setValue(self._wrap(-e['rotation']['raw'] / 100.0))
            self.size_label.setText("mass (size)")
            self.size_spin.setDecimals(0); self.size_spin.setRange(1, 5e6); self.size_spin.setValue(e.get('mass', 1.0))
            self._set_swatch(e['color']['rgba'])
            self.kind.setEnabled(True); self.kind.setCurrentIndex(0)
            self._refresh_eat(); self._refresh_paths()
            self.persist.setEnabled(True); self.persist.setChecked(e.get('field_158', 0) != 0)
            self._fill_emit(e)
            self._block = False; self._refill_layers(item.layer_idx)
        elif isinstance(item, DecoItem) and getattr(item, '_resolved', False):
            d = item.ref; self.title.setText((item._name or "decoration"))
            self.sx.setValue(d['position'][0] * DECO_K); self.sy.setValue(d['position'][1] * DECO_K)
            self.rot.setValue(self._wrap(-d.get('size', {}).get('raw', 0) / 100.0))
            self.size_label.setText("size ×")
            dims = d.get('dimensions', {}).get('raw', [DIM_BASE, DIM_BASE])
            self.size_spin.setDecimals(2); self.size_spin.setRange(0.05, 40.0)
            self.size_spin.setValue((dims[0] or DIM_BASE) / DIM_BASE)
            self._set_swatch(d.get('color_rgba', [255, 255, 255, 255]))
            self.kind.setEnabled(True); self.kind.setCurrentIndex(1)
            self._refresh_eat(); self._refresh_paths()
            self.persist.setEnabled(False); self.persist.setChecked(False)
            self.emit.setEnabled(False); self.emit.setChecked(False)
            for sb in (self.emit_count, self.emit_min, self.emit_max): sb.setEnabled(False)
            self._block = False; self._refill_layers(item.layer_idx)
        else:
            self.title.setText("Select an object")
            self._block = True; self.layer.clear(); self.layer.setEnabled(False); self.kind.setEnabled(False)
            self.eat.setText(""); self.eat.setEnabled(False)
            self.path.clear(); self.path.setEnabled(False)
            self.persist.setEnabled(False); self.persist.setChecked(False)
            self.emit.setEnabled(False); self.emit.setChecked(False)
            for sb in (self.emit_count, self.emit_min, self.emit_max): sb.setEnabled(False)
        self._block = False

    def refresh_pos(self, item):
        if item is not self.item: return
        self._block = True
        K = ENTITY_K if isinstance(item, EntityItem) else DECO_K
        self.sx.setValue(item.ref['position'][0] * K); self.sy.setValue(item.ref['position'][1] * K)
        self._block = False

    def _set_swatch(self, rgba):
        self.swatch.setStyleSheet(f"background: rgba({rgba[0]},{rgba[1]},{rgba[2]},{rgba[3]/255:.2f}); border-radius:9px; padding:7px;")

    def _edit(self, *_):
        if self._block: return
        it = self.item
        if isinstance(it, EntityItem):
            e = it.ref
            e['position'] = [self.sx.value() / ENTITY_K, self.sy.value() / ENTITY_K]
            e['rotation']['raw'] = int(round(-self.rot.value() * 100)); e['mass'] = self.size_spin.value()
            it.place()
        elif isinstance(it, DecoItem) and it._resolved:
            d = it.ref
            d['position'] = [self.sx.value() / DECO_K, self.sy.value() / DECO_K]
            d.setdefault('size', {})['raw'] = int(round(-self.rot.value() * 100))
            old = d.get('dimensions', {}).get('raw', [DIM_BASE, DIM_BASE])
            aspect = (old[1] / old[0]) if old[0] else 1.0
            nx = int(round(self.size_spin.value() * DIM_BASE))
            d.setdefault('dimensions', {})['raw'] = [nx, int(round(nx * aspect))]
            it.apply()

    def _pick(self):
        it = self.item
        if isinstance(it, EntityItem):
            cur = it.ref['color']['rgba']
            c = QColorDialog.getColor(QColor(*cur[:3]), self, "Tint")
            if not c.isValid(): return
            it.ref['color']['rgba'] = [c.red(), c.green(), c.blue(), cur[3]]
            self._set_swatch(it.ref['color']['rgba']); it.rebuild(); it.place(); self.win.commit()
        elif isinstance(it, DecoItem) and it._resolved:
            cur = it.ref.get('color_rgba', [255, 255, 255, 255])
            c = QColorDialog.getColor(QColor(*cur[:3]), self, "Tint")
            if not c.isValid(): return
            it.ref['color_rgba'] = [c.red(), c.green(), c.blue(), cur[3]]
            self._set_swatch(it.ref['color_rgba']); it.rebuild(); it.apply(); self.win.commit()


class MultiLevelDock(QDockWidget):
    """Build/edit a multilevel: an ordered, add/remove/reorder array of stage-levels."""
    def __init__(self, win):
        super().__init__("Multilevel builder", win); self.win = win
        self.ml = None; self.ml_path = None; self.cur = None
        self._restoring_selection = False
        w = QWidget(); col = QVBoxLayout(w); col.setSpacing(8)
        col.addWidget(QLabel("Stages  (the <level> array — top plays first)"))
        self.list = QListWidget()
        self.list.setMinimumHeight(130)   # enough room for about 3 visible stage rows
        self.list.currentRowChanged.connect(self._select)
        self.list.itemDoubleClicked.connect(lambda *_: self._open())
        col.addWidget(self.list)
        # add / remove / reorder
        rowb = QHBoxLayout()
        for txt, fn, tip in (("+ blank", self._add_blank, "Create a new empty .bin/.xml and add it as a stage"),
                             ("+ existing", self._add_existing, "Add an existing .bin as a stage"),
                             ("−", self._remove, "Remove the selected stage from the array"),
                             ("↑", lambda: self._move(-1), "Move stage earlier"),
                             ("↓", lambda: self._move(1), "Move stage later")):
            b = QPushButton(txt); b.setToolTip(tip); b.clicked.connect(fn); rowb.addWidget(b)
        col.addLayout(rowb)
        self.open_btn = QPushButton("Open selected stage in editor"); self.open_btn.clicked.connect(self._open)
        col.addWidget(self.open_btn)

        # per-stage params
        col.addWidget(self._hline()); col.addWidget(self._h("Selected stage"))
        self.sform = QFormLayout(); sc = QWidget(); sc.setLayout(self.sform); col.addWidget(sc)
        # multilevel-wide rules
        col.addWidget(self._hline()); col.addWidget(self._h("Multilevel rules"))
        self.mform = QFormLayout(); mc = QWidget(); mc.setLayout(self.mform); col.addWidget(mc)
        # save
        col.addWidget(self._hline())
        srow = QHBoxLayout()
        b1 = QPushButton("Save"); b1.clicked.connect(self._save); srow.addWidget(b1)
        b2 = QPushButton("Save As…"); b2.clicked.connect(self._save_as); srow.addWidget(b2)
        col.addLayout(srow)
        col.addStretch(1)

        # Keep the full dock scrollable when space is tight, while still allowing splitter resizing.
        _sc = QScrollArea()
        _sc.setWidgetResizable(True)
        _sc.setWidget(w)
        self.setWidget(_sc)
        self.stage_edits = {}; self.ml_edits = {}

    @staticmethod
    def _h(t):
        l = QLabel(t); l.setStyleSheet("font-weight:bold;margin-top:6px;"); return l
    @staticmethod
    def _hline():
        f = QFrame(); f.setFrameShape(QFrame.HLine); f.setStyleSheet(f"color:{PANEL2};"); return f

    def _mkw(self, value, choices):
        cur = "" if value is None else str(value)
        if choices is not None:
            w = QComboBox()
            for val, disp in choices: w.addItem(disp, val)
            idx = next((i for i, (val, _) in enumerate(choices) if val == cur), -1)
            if idx < 0: w.addItem(cur, cur); idx = w.count() - 1
            w.setCurrentIndex(idx)
        else:
            w = QLineEdit(cur)
        return w
    @staticmethod
    def _wval(w):
        if isinstance(w, QComboBox):
            d = w.currentData(); return (d if d is not None else w.currentText()).strip()
        return w.text().strip()

    # ---- load / list ----
    def load(self, ml, path):
        self.ml, self.ml_path, self.cur = ml, path, None
        self._build_ml_form()
        self._rebuild_list(select=0 if ml.stages else None)
        self.show()

    def _rebuild_list(self, select=None):
        self.list.blockSignals(True); self.list.clear()
        m0 = float(self.ml.stages[0]['meterperpix']) if self.ml.stages else 1.0
        for i, s in enumerate(self.ml.stages):
            try: zoom = f"×{float(s.get('meterperpix', m0))/m0:.2f} zoom"
            except (TypeError, ValueError, ZeroDivisionError): zoom = ""
            it = QListWidgetItem(f"{i+1}.  {s.get('name','?')}   ·   {zoom}")
            self.list.addItem(it)
        self.list.blockSignals(False)
        if select is not None and 0 <= select < self.list.count():
            self.list.setCurrentRow(select)
        elif not self.ml.stages:
            self.cur = None; self._build_stage_form()

    # ---- per-stage form ----
    def _select(self, idx):
        if self._restoring_selection:
            return
        prev = self.cur
        self._commit_stage()                       # save edits from the previously-selected stage
        self.cur = idx if (self.ml and 0 <= idx < len(self.ml.stages)) else None
        self._build_stage_form()
        if self.cur is None or not self.ml_path:
            return
        target = self.ml.stages[self.cur].get('name')
        current = os.path.splitext(os.path.basename(self.win.path))[0] if self.win.path else None
        if target == current:
            return
        if not self.win.load_stage(target, self.ml_path):
            self._restoring_selection = True
            self.list.blockSignals(True)
            self.list.setCurrentRow(prev if prev is not None else -1)
            self.list.blockSignals(False)
            self._restoring_selection = False
            self.cur = prev if (self.ml and prev is not None and 0 <= prev < len(self.ml.stages)) else None
            self._build_stage_form()

    def _build_stage_form(self):
        while self.sform.rowCount(): self.sform.removeRow(0)
        self.stage_edits = {}
        if self.cur is None: return
        st = self.ml.stages[self.cur]
        w = self._mkw(st.get('name'), None); self.stage_edits['name'] = w
        w.setToolTip("Base filename of this stage's .bin (links the level into the multilevel).")
        self.sform.addRow("name (.bin)", w)
        for key, label, choices, tip in STAGE_SPECS:
            w = self._mkw(st.get(key), choices); w.setToolTip(tip)
            self.stage_edits[key] = w; self.sform.addRow(label, w)

    def _commit_stage(self):
        if self.cur is None or not self.stage_edits: return
        if not (self.ml and 0 <= self.cur < len(self.ml.stages)): return
        st = self.ml.stages[self.cur]
        for k, w in self.stage_edits.items():
            st[k] = self._wval(w)

    # ---- multilevel-wide form ----
    def _build_ml_form(self):
        while self.mform.rowCount(): self.mform.removeRow(0)
        self.ml_edits = {}
        for key, label, choices, tip in ML_SPECS:
            if key in self.ml.attrs:
                w = self._mkw(self.ml.attrs[key], choices); w.setToolTip(tip)
                self.ml_edits[key] = w; self.mform.addRow(label, w)

    def _commit_ml(self):
        for k, w in self.ml_edits.items():
            self.ml.attrs[k] = self._wval(w)

    # ---- array operations ----
    def _add_blank(self):
        if self.ml is None: self.win.statusBar().showMessage("open or start a multilevel first"); return
        start = self.win._levels_dir()
        fn, _ = QFileDialog.getSaveFileName(self, "New blank level .bin", start, "Level (*.bin)")
        if not fn: return
        if not fn.lower().endswith(".bin"): fn += ".bin"
        try:
            write_blank_level(fn)
        except Exception as ex:
            self.win.statusBar().showMessage(f"could not create level: {ex}"); return
        name = os.path.splitext(os.path.basename(fn))[0]
        self._commit_stage(); self.ml.stages.append(default_stage(name))
        self._rebuild_list(select=len(self.ml.stages) - 1)
        self.win.statusBar().showMessage(f"created {name}.bin + .xml and added as stage {len(self.ml.stages)}")

    def _add_existing(self):
        if self.ml is None: self.win.statusBar().showMessage("open or start a multilevel first"); return
        start = self.win._levels_dir()
        fn, _ = QFileDialog.getOpenFileName(self, "Add existing level .bin", start, "Level (*.bin)")
        if not fn: return
        name = os.path.splitext(os.path.basename(fn))[0]
        self._commit_stage(); self.ml.stages.append(default_stage(name))
        self._rebuild_list(select=len(self.ml.stages) - 1)

    def _remove(self):
        if self.cur is None: return
        i = self.cur
        self.stage_edits = {}                       # drop edits for the row being deleted
        del self.ml.stages[i]; self.cur = None
        self._rebuild_list(select=min(i, len(self.ml.stages) - 1) if self.ml.stages else None)

    def _move(self, delta):
        if self.cur is None: return
        self._commit_stage()
        i = self.cur; j = i + delta
        if not (0 <= j < len(self.ml.stages)): return
        self.ml.stages[i], self.ml.stages[j] = self.ml.stages[j], self.ml.stages[i]
        self.cur = None; self._rebuild_list(select=j)

    def _open(self):
        if self.cur is None or not self.ml_path:
            self.win.statusBar().showMessage("save the multilevel first, then open a stage"); return
        self._commit_stage()
        self.win.load_stage(self.ml.stages[self.cur]['name'], self.ml_path)

    # ---- save ----
    def _flush(self):
        self._commit_stage(); self._commit_ml()

    def _save(self):
        if not self.ml: return
        if not self.ml_path: return self._save_as()
        self._flush()
        try: self.ml.save(self.ml_path)
        except Exception as ex: self.win.statusBar().showMessage(f"save failed: {ex}"); return
        self.win.statusBar().showMessage(f"saved {os.path.basename(self.ml_path)}  ({len(self.ml.stages)} stages)")
        self.win._push_recent('recent_multilevels', self.ml_path)

    def _save_as(self):
        if not self.ml: return
        self._flush()
        start = self.ml_path or self.win._multilevels_dir()
        fn, _ = QFileDialog.getSaveFileName(self, "Save multilevel as", start, "Multilevel (*.xml)")
        if not fn: return
        if not fn.lower().endswith(".xml"): fn += ".xml"
        try: self.ml.save(fn)
        except Exception as ex: self.win.statusBar().showMessage(f"save failed: {ex}"); return
        self.ml_path = fn
        self.win.statusBar().showMessage(f"saved {os.path.basename(fn)}  ({len(self.ml.stages)} stages)")
        self.win._push_recent('recent_multilevels', fn)


class LevelSettings(QDialog):
    """Edits the open level's tunable parameters. The goal/win size (triggerarea) and zoom live
    in the parent multilevel XML (auto-located in the level's folder); the playfield edges,
    growth rate and starting size live in the level's own .xml shell. Saves to both."""
    def __init__(self, win):
        super().__init__(win)
        self.win = win; self.setWindowTitle("Level Settings"); self.resize(440, 600)
        self.lay = QVBoxLayout(self)
        self.info = QLabel(""); self.info.setWordWrap(True); self.info.setStyleSheet(f"color:{ACCENT}")
        self.lay.addWidget(self.info)
        # scrollable body so a long parameter list never overflows the screen
        _sc = QScrollArea(); _sc.setWidgetResizable(True)
        _body = QWidget(); _bl = QVBoxLayout(_body); _bl.setContentsMargins(0, 0, 0, 0)
        self.host = QWidget(); self.form = QFormLayout(self.host); _bl.addWidget(self.host)
        _bl.addStretch(1); _sc.setWidget(_body)
        self.lay.addWidget(_sc, 1)
        btns = QWidget(); bh = QHBoxLayout(btns); bh.setContentsMargins(0, 0, 0, 0)
        self.choose_btn = QPushButton("Choose multilevel…"); self.choose_btn.clicked.connect(self._choose_ml)
        self.save_btn = QPushButton("Save settings"); self.save_btn.clicked.connect(self._save)
        bh.addWidget(self.choose_btn); bh.addStretch(1); bh.addWidget(self.save_btn)
        self.lay.addWidget(btns)
        self.ml = self.ml_path = self.stage = self.shell_path = None
        self.edits = {}; self._shell_txt = ""

    def _choose_ml(self):
        if not getattr(self.win, 'path', None):
            self.win.statusBar().showMessage("Open a level first."); return
        start = os.path.dirname(self.win.path)
        fn, _ = QFileDialog.getOpenFileName(self, "Select the multilevel XML", start, "Multilevel (*.xml)")
        if not fn: return
        try:
            ml = TM.MultiLevel(fn)
        except Exception as ex:
            self.win.statusBar().showMessage(f"not a multilevel: {ex}"); return
        name = os.path.splitext(os.path.basename(self.win.path))[0]
        if not any(s.get('name') == name for s in ml.stages):
            self.win.statusBar().showMessage(f"'{name}' isn't listed in {os.path.basename(fn)}"); return
        if not hasattr(self.win, '_ml_search_dirs'): self.win._ml_search_dirs = []
        d = os.path.dirname(fn)
        if d not in self.win._ml_search_dirs: self.win._ml_search_dirs.append(d)
        self.refresh()
        self.win.statusBar().showMessage(f"linked {name} → {os.path.basename(fn)}")

    def _section(self, title):
        lbl = QLabel(title); lbl.setStyleSheet("font-weight:bold; margin-top:10px;")
        self.form.addRow(lbl)

    def _row(self, src, key, label, value, tip=None, choices=None):
        cur = "" if value is None else str(value)
        if choices is not None:
            w = QComboBox()
            for val, disp in choices: w.addItem(disp, val)
            idx = next((i for i, (val, _) in enumerate(choices) if val == cur), -1)
            if idx < 0:
                w.addItem(cur, cur); idx = w.count() - 1
            w.setCurrentIndex(idx)
        else:
            w = QLineEdit(cur)
        if tip: w.setToolTip(tip)
        lbl = QLabel(label)
        if tip: lbl.setToolTip(tip)
        self.edits[(src, key)] = w
        self.form.addRow(lbl, w); return w

    @staticmethod
    def _wval(w):
        if isinstance(w, QComboBox):
            d = w.currentData()
            return (d if d is not None else w.currentText()).strip()
        return w.text().strip()

    def refresh(self):
        while self.form.rowCount(): self.form.removeRow(0)
        self.edits = {}
        win = self.win
        if not getattr(win, 'path', None) or win.level is None:
            self.info.setText("Open a level first."); return
        name = os.path.splitext(os.path.basename(win.path))[0]
        self.shell_path = os.path.splitext(win.path)[0] + ".xml"
        self.ml, self.ml_path, self.stage = win._find_multilevel()
        info = [f"Level: {name}"]
        if self.stage is not None:
            info.append(f"Goal lives in: {os.path.basename(self.ml_path)}")
            self._section("Goal & stage  (multilevel)")
            for key, label, choices, tip in STAGE_SPECS:
                self._row('stage', key, label, self.stage.get(key), tip, choices)
            self._section("Multilevel rules")
            for key, label, choices, tip in ML_SPECS:
                if key in self.ml.attrs:
                    self._row('ml', key, label, self.ml.attrs[key], tip, choices)
            vt = self.edits.get(('ml', 'victorytype'))
            if isinstance(vt, QComboBox):
                vt.currentIndexChanged.connect(self._victory_changed)
        else:
            info.append("No parent multilevel found in this folder, so the goal area (triggerarea) "
                        "isn't available — it's stored in the multilevel XML, not the level shell.")
        # shell
        txt = open(self.shell_path, encoding='utf-8', errors='ignore').read() if os.path.isfile(self.shell_path) else ""
        self._shell_txt = txt
        def g(k):
            m = re.search(k + r'="([0-9.eE+\-]+)"', txt); return m.group(1) if m else None
        self._section("Level shell  (this level's .xml)")
        for k, label in (('growthrate', "Growth rate"), ('edgeleft', "Edge left"), ('edgetop', "Edge top"),
                         ('edgeright', "Edge right"), ('edgebottom', "Edge bottom")):
            v = g(k)
            if v is not None: self._row('shell', k, label, v)
        m = re.search(r'<goostart[^>]*\barea="([0-9.eE+\-]+)"', txt)
        if m: self._row('shell', 'goostart_area', "Start area (starting size)", m.group(1))
        self.info.setText("\n".join(info))
        self._wire_meters()
        self._victory_changed()

    def _victory_changed(self, *_):
        """Re-label the goal and grey out the field that doesn't apply for the chosen victory type."""
        trig = self.edits.get(('stage', 'triggerarea'))
        num = self.edits.get(('ml', 'numspecialentities'))
        vt_w = self.edits.get(('ml', 'victorytype'))
        vt = self._wval(vt_w) if vt_w is not None else '0'
        if trig is not None:
            lbl = self.form.labelForField(trig)
            if vt == '1':
                txt = "Stage-transition area"; tip = "Goo area that advances to the next stage. Not the win condition — you win by collecting objects."
            elif vt == '2':
                txt = "Stage-transition area"; tip = "Goo area that advances to the next stage. The size meter is hidden for this victory type."
            else:
                txt = "Goal area — reach this size to win"; tip = "Reach this goo area to beat the level (victory type 0)."
            trig.setToolTip(tip)
            if lbl is not None: lbl.setText(txt); lbl.setToolTip(tip)
        if num is not None:
            applies = (vt == '1')
            num.setEnabled(applies)
            lbl = self.form.labelForField(num)
            if lbl is not None:
                lbl.setEnabled(applies)
                lbl.setText("Objects to collect to WIN" if applies else "Special entities to collect")

    # ---- size-in-meters helpers:  meters = 2*sqrt(area/pi)*meterperpix ----
    @staticmethod
    def _f(e):
        try: return float(e.text())
        except (TypeError, ValueError): return None

    def _mpp(self):
        e = self.edits.get(('stage', 'meterperpix'))
        return self._f(e) if e else None

    def _wire_meters(self):
        self._guard = False
        self.e_goalm = self.e_startm = None
        mpp_e = self.edits.get(('stage', 'meterperpix'))
        trig_e = self.edits.get(('stage', 'triggerarea'))
        start_e = self.edits.get(('shell', 'goostart_area'))
        if mpp_e is None:
            return                                    # no zoom available → meters can't be derived
        if trig_e is not None:
            self.e_goalm = QLineEdit(); self.form.addRow("  ↳ goal size (meters)", self.e_goalm)
            self.e_goalm.editingFinished.connect(lambda: self._meters_to_area(self.e_goalm, trig_e))
        if start_e is not None:
            self.e_startm = QLineEdit(); self.form.addRow("  ↳ start size (meters)", self.e_startm)
            self.e_startm.editingFinished.connect(lambda: self._meters_to_area(self.e_startm, start_e))
        for e in (mpp_e, trig_e, start_e):
            if e is not None: e.textChanged.connect(self._area_to_meters)
        self._area_to_meters()

    def _area_to_meters(self):
        if getattr(self, '_guard', False): return
        self._guard = True
        mpp = self._mpp()
        for area_e, m_e in ((self.edits.get(('stage', 'triggerarea')), self.e_goalm),
                            (self.edits.get(('shell', 'goostart_area')), self.e_startm)):
            if area_e is not None and m_e is not None:
                a = self._f(area_e)
                m_e.setText(f"{2*math.sqrt(max(a,0.0)/math.pi)*mpp:.2f}" if (a is not None and mpp) else "")
        self._guard = False

    def _meters_to_area(self, m_e, area_e):
        if getattr(self, '_guard', False): return
        mpp = self._mpp(); m = self._f(m_e)
        if mpp and m is not None:
            self._guard = True
            area_e.setText(f"{math.pi*(m/(2*mpp))**2:.0f}")
            self._guard = False

    def _save(self):
        # stage + multilevel
        if self.stage is not None:
            for (src, key), e in self.edits.items():
                v = self._wval(e)
                if src == 'stage': self.stage[key] = v
                elif src == 'ml': self.ml.attrs[key] = v
            try:
                self.ml.save(self.ml_path)
            except Exception as ex:
                self.win.statusBar().showMessage(f"multilevel save failed: {ex}"); return
        # shell .xml (targeted attribute replacement, preserves the rest)
        if os.path.isfile(self.shell_path):
            txt = self._shell_txt
            for (src, key), e in self.edits.items():
                if src != 'shell': continue
                v = self._wval(e)
                if key == 'goostart_area':
                    txt = re.sub(r'(<goostart[^>]*\barea=")[0-9.eE+\-]+(")',
                                 lambda mm: mm.group(1) + v + mm.group(2), txt)
                else:
                    txt = re.sub(key + r'="[0-9.eE+\-]+"', f'{key}="{v}"', txt, count=1)
            open(self.shell_path, 'w', encoding='utf-8').write(txt)
            self._shell_txt = txt
            new_edges = self.win._read_edges(self.shell_path)
            if new_edges:
                self.win.edges = new_edges
                if self.win.level: self.win._populate(refit=False)
        self.win.statusBar().showMessage("level settings saved")


def pil_to_pixmap(im):
    if im is None: return None
    im = im.convert("RGBA")
    qim = QImage(im.tobytes("raw", "RGBA"), im.width, im.height, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qim.copy())


class EntityReport(QDialog):
    """Lists every entity in the current level by name (exactly as the Inspector shows it —
    the entity's 'type') with how many times each is used. 'Copy names' puts the list on the
    clipboard for compiling corrections."""
    def __init__(self, win):
        super().__init__(win)
        self.win = win; self.setWindowTitle("Entity Report")
        self.resize(420, 560)
        lay = QVBoxLayout(self)
        self.summary = QLabel(""); self.summary.setStyleSheet(f"color:{ACCENT}")
        lay.addWidget(self.summary)
        self.search = QLineEdit(); self.search.setPlaceholderText("Filter…")
        self.search.textChanged.connect(self._fill); lay.addWidget(self.search)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["entity name", "count"])
        self.table.setSortingEnabled(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.table, 1)
        row = QWidget(); rh = QHBoxLayout(row); rh.setContentsMargins(0, 0, 0, 0)
        rh.addStretch(1)
        b_names = QPushButton("Copy names"); b_names.clicked.connect(lambda: self._copy(False))
        b_counts = QPushButton("Copy names + counts"); b_counts.clicked.connect(lambda: self._copy(True))
        rh.addWidget(b_names); rh.addWidget(b_counts)
        lay.addWidget(row)
        self._rows = []

    def refresh(self):
        from collections import Counter
        counts = Counter()
        if self.win.level:
            for L in self.win.level['layers']:
                for e in L['entities']:
                    if e.get('type'): counts[e['type']] += 1
        self._rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        self.summary.setText(f"{sum(counts.values())} entities · {len(self._rows)} unique names")
        self._fill()

    def _fill(self, *_):
        q = self.search.text().strip().lower()
        shown = [(t, n) for t, n in self._rows if not q or q in t.lower()]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(shown))
        for i, (t, n) in enumerate(shown):
            it_t = QTableWidgetItem(t)
            it_n = QTableWidgetItem(); it_n.setData(Qt.DisplayRole, int(n))
            self.table.setItem(i, 0, it_t); self.table.setItem(i, 1, it_n)
        self.table.setSortingEnabled(True)

    def _copy(self, with_counts):
        q = self.search.text().strip().lower()
        rows = [(t, n) for t, n in self._rows if not q or q in t.lower()]
        if with_counts:
            lines = [f"{t}\t{n}" for t, n in rows]
        else:
            lines = [t for t, n in rows]
        QApplication.clipboard().setText("\n".join(lines))
        self.win.statusBar().showMessage(f"copied {len(rows)} entity names to clipboard")


class AssetBrowser(QDialog):
    """Browseable spawn palette. In ENTITY mode it lists valid entity-type names from
    strings.xml (+ the egypt house composites) — these are the names the game can actually
    spawn, so it won't crash. In DECORATION mode it lists atlas cells (decorations are just
    drawn tiles and accept any cell name). Thumbnails resolve through the sprite resolver."""
    THEMES = ['egypt', 'future', 'dino', 'modern', 'roman', 'japan', 'general',
              'anti', 'croc', 'rat', 'stonepath', 'space']

    def __init__(self, win):
        super().__init__(win)
        self.win = win; self.setWindowTitle("Asset Browser")
        self.resize(720, 580); self.setMinimumWidth(440)
        self._thumbs = {}
        lay = QVBoxLayout(self)
        bar = QWidget(); h = QVBoxLayout(bar); h.setContentsMargins(0, 0, 0, 0)
        row2 = QWidget(); r2 = QHBoxLayout(row2); r2.setContentsMargins(0, 0, 0, 0)
        r2.addWidget(QLabel("Spawn as:"))
        self.spawn_as = QComboBox(); self.spawn_as.addItems(["entity (eaten)", "decoration (background)"])
        self.spawn_as.currentIndexChanged.connect(self._mode_changed)
        r2.addWidget(self.spawn_as)
        r2.addWidget(QLabel("on layer:"))
        self.layer_sel = QComboBox(); self.layer_sel.addItems(getattr(win, 'layer_names', []) or [])
        ln = getattr(win, 'layer_names', [])
        if 'Main' in ln: self.layer_sel.setCurrentIndex(ln.index('Main'))
        r2.addWidget(self.layer_sel, 1)
        h.addWidget(row2)
        row = QWidget(); rh = QHBoxLayout(row); rh.setContentsMargins(0, 0, 0, 0)
        self.cat = QComboBox(); self.cat.currentTextChanged.connect(self._refresh)
        self.search = QLineEdit(); self.search.setPlaceholderText("Search assets…")
        self.search.textChanged.connect(self._refresh)
        rh.addWidget(self.cat, 1); rh.addWidget(self.search, 2)
        h.addWidget(row); lay.addWidget(bar)
        self.count = QLabel(""); self.count.setStyleSheet(f"color:{ACCENT}"); lay.addWidget(self.count)
        self.scroll = QScrollArea(); self.scroll.setWidgetResizable(True)
        self.grid_host = QWidget(); self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8); self.scroll.setWidget(self.grid_host)
        lay.addWidget(self.scroll, 1)
        self._rebuild()

    def _mode(self):
        return 'entity' if self.spawn_as.currentIndex() == 0 else 'deco'

    def _theme_of(self, name):
        th = getattr(self.win.assets, 'entity_theme', {}).get(name)
        if th: return th.lower()
        c = self.win.assets._base_cell(name)
        if c is not None:
            return next((t for t in self.THEMES if c.atlas.startswith(t)), 'other')
        return next((t for t in self.THEMES if name.startswith(t)), 'other')

    def _build_catalog(self):
        """(name, kind, category, display) for the current mode."""
        A = self.win.assets; out = []
        if self._mode() == 'entity':
            names = set(A.entity_names()) | set(getattr(A, 'house_recipe', {}))
            names |= {'egypt_poor_house_eight', 'egypt_rich_house_four'}
            # entity types already used in the open level are valid to spawn — always include them
            lvl = self.win.level
            if lvl:
                for L in lvl['layers']:
                    for e in L['entities']:
                        if e.get('type'): names.add(e['type'])
            for n in names:
                cat = 'egypt (houses)' if n in getattr(A, 'house_recipe', {}) or n in (
                    'egypt_poor_house_eight', 'egypt_rich_house_four') else self._theme_of(n)
                out.append((n, 'entity', cat, A.display_name(n)))
        else:
            for cell, cobj in A.cells.items():
                theme = next((t for t in self.THEMES if cobj.atlas.startswith(t)), 'other')
                out.append((cell, 'deco', theme, cell))
        return out

    def _rebuild(self):
        self.catalog = self._build_catalog()
        cats = sorted({c for _, _, c, _ in self.catalog})
        self.cat.blockSignals(True); self.cat.clear()
        self.cat.addItem("All categories"); self.cat.addItems(cats)
        self.cat.blockSignals(False)
        self._refresh()

    def _mode_changed(self, *_):
        # entity spawns default to Main, decorations to Main Bottom
        ln = getattr(self.win, 'layer_names', [])
        want = 'Main' if self._mode() == 'entity' else 'Main Bottom'
        if want in ln: self.layer_sel.setCurrentIndex(ln.index(want))
        self._rebuild()

    def _thumb(self, name, kind):
        key = (name, kind)
        if key in self._thumbs: return self._thumbs[key]
        A = self.win.assets
        im = A.build_sprite(name) if kind == 'entity' else (A.cell_image(name) or A.build_sprite(name))
        pm = pil_to_pixmap(im)
        if pm is not None:
            pm = pm.scaled(QSize(64, 64), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._thumbs[key] = pm; return pm

    def _refresh(self, *_):
        while self.grid.count():
            it = self.grid.takeAt(0); w = it.widget()
            if w: w.setParent(None)
        cat = self.cat.currentText(); q = self.search.text().strip().lower()
        items = [it for it in self.catalog
                 if (cat in ("All categories", "") or it[2] == cat)
                 and (not q or q in it[0].lower() or q in it[3].lower())]
        items.sort(key=lambda t: t[3].lower())
        shown = items[:400]
        base = f"{len(items)} assets" + (f"  (showing first {len(shown)})" if len(items) > len(shown) else "")
        if self._mode() == 'entity' and not self.win.assets.entity_names():
            base += "   —  strings.xml not found: only houses + this level's entities are listed"
        self.count.setText(base)
        cols = 6
        for idx, (name, kind, category, display) in enumerate(shown):
            b = QToolButton(); b.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
            label = display or name
            b.setText(label if len(label) <= 16 else label[:15] + "…")
            b.setToolTip(f"{display}\n[{name}]  ({kind})")
            b.setIconSize(QSize(64, 64)); b.setFixedSize(96, 96)
            pm = self._thumb(name, kind)
            if pm is not None: b.setIcon(QIcon(pm))
            b.clicked.connect(lambda _=False, n=name, k=kind: self._spawn(n, k))
            self.grid.addWidget(b, idx // cols, idx % cols)

    def _spawn(self, name, kind):
        if self.win.level is None:
            self.win.statusBar().showMessage("Open a level first, then spawn assets."); return
        li = self.layer_sel.currentIndex(); li = li if li >= 0 else None
        if kind == 'entity': self.win.spawn_entity(name, layer_idx=li)
        else: self.win.spawn_decoration(name, layer_idx=li)


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tasty Planet 2 — Level Editor"); self.resize(1360, 880)
        self.bus = Bus(); self.level = None; self.path = None
        self.layer_names = []; self.items_by_layer = []
        self.edges = (-1600.0, -1200.0, 1600.0, 1200.0)
        self._bnd_block = False; self._bnd_rect = None; self._bnd_handles = []
        self.walls_editable = False; self._wall_block = False
        self._wall_handles = []; self._sel_wall = None
        self._onion = None; self._onion_on = False; self._onion_data = None
        self._onion_nudge = [0.0, 0.0]; self._onion_scale_mult = 1.0
        self._last_path = None
        self.bg_editable = False
        self.history = []; self.hist_idx = -1
        self._saved_level_state = None
        self._cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_FILE)
        self._cfg = self._load_config()
        self._assets_root = self._load_assets_root()
        self.gfx = self._graphics_dir()
        self.assets = self._load_assets(); self._pm = {}
        self.scene = QGraphicsScene(self); self.view = Canvas(self.scene, self); self.setCentralWidget(self.view)
        self.inspector = Inspector(self); self.addDockWidget(Qt.RightDockWidgetArea, self.inspector)
        self.layers = LayersDock(self); self.addDockWidget(Qt.LeftDockWidgetArea, self.layers)
        self.mldock = MultiLevelDock(self); self.addDockWidget(Qt.LeftDockWidgetArea, self.mldock); self.mldock.hide()
        self.anim_t = 0.0; self.timer = QTimer(self); self.timer.timeout.connect(self._anim_step)
        self._menu(); self.scene.selectionChanged.connect(self._sel)

    def _load_assets(self):
        # the game XMLs aren't all in one place: imagemaps/entities/animations usually live in
        # assets/graphics, while level XMLs live in assets/levels. Search the assets tree first.
        seen = set(); cands = []
        for d in (self._graphics_dir(), self._assets_root, self._levels_dir(), self._multilevels_dir(),
                  os.path.dirname(os.path.abspath(__file__)), os.getcwd()):
            if d and d not in seen:
                seen.add(d); cands.append(d)
        def find(fn):
            for d in cands:
                p = os.path.join(d, fn)
                if os.path.isfile(p): return p
            return os.path.join(self.gfx, fn)
        im = find("imagemaps.xml")
        if not os.path.isfile(im):
            return Assets(self.gfx)
        a = Assets(self.gfx, imagemaps=im, entities=find("entityintersections.xml"),
                   animations=find("animationdefs.xml"), strings=find("strings.xml"))
        return a

    def _default_assets_root(self):
        root = os.path.dirname(DEFAULT_GFX)
        return root if os.path.isdir(root) else os.getcwd()

    def _validate_assets_root(self, root):
        return (isinstance(root, str) and os.path.isdir(root) and
                os.path.isdir(os.path.join(root, 'graphics')) and
                os.path.isdir(os.path.join(root, 'levels')))

    def _graphics_dir(self):
        return os.path.join(self._assets_root, 'graphics') if self._assets_root else os.getcwd()

    def _levels_dir(self):
        return os.path.join(self._assets_root, 'levels') if self._assets_root else os.getcwd()

    def _multilevels_dir(self):
        return os.path.join(self._levels_dir(), 'multilevels') if self._assets_root else os.getcwd()

    def _load_assets_root(self):
        saved_root = self._cfg.get('assets_root')
        if self._validate_assets_root(saved_root):
            if self._cfg.pop('graphics_path', None) is not None:
                self._save_config()
            return saved_root
        legacy = self._cfg.get('graphics_path')
        if self._validate_assets_root(legacy):
            self._cfg.pop('graphics_path', None)
            self._save_setting('assets_root', legacy)
            return legacy
        if saved_root or legacy:
            msg = QMessageBox(self)
            msg.setIcon(QMessageBox.Warning)
            msg.setWindowTitle("Assets folder reset")
            msg.setText("The saved assets folder is no longer valid for this editor.")
            msg.setInformativeText("Please choose the game's assets folder again. The setting has been reset.")
            msg.exec()
        self._cfg.pop('graphics_path', None)
        root = self._default_assets_root()
        if self._validate_assets_root(root):
            self._save_setting('assets_root', root)
            return root
        self._cfg.pop('assets_root', None)
        self._save_config()
        return os.getcwd()

    def _load_config(self):
        if not os.path.isfile(self._cfg_path):
            return {}
        try:
            with open(self._cfg_path, encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_config(self):
        try:
            with open(self._cfg_path, 'w', encoding='utf-8') as f:
                json.dump(self._cfg, f, indent=2)
        except OSError:
            pass

    def _save_setting(self, key, value):
        self._cfg[key] = value
        self._save_config()

    def _recent_list(self, key):
        items = self._cfg.get(key, [])
        if not isinstance(items, list):
            return []
        out = []
        seen = set()
        for path in items:
            if isinstance(path, str) and path and path not in seen:
                out.append(path)
                seen.add(path)
        return out[:5]

    def _push_recent(self, key, path):
        if not path:
            return
        items = self._recent_list(key)
        items = [p for p in items if os.path.normcase(p) != os.path.normcase(path)]
        items.insert(0, path)
        self._save_setting(key, items[:5])

    def _recent_label(self, path):
        base = os.path.basename(path)
        folder = os.path.basename(os.path.dirname(path))
        return f"{base}  [{folder}]" if folder else base

    def sprite_pixmap(self, name, rgba, frame):
        key = (name, rgba, frame)
        if key not in self._pm:
            spr = self.assets.build_sprite(name, rgba, frame)
            self._pm[key] = pil_to_qpixmap(spr) if spr is not None else None
        return self._pm[key]

    def _mark_level_saved(self):
        self._saved_level_state = copy.deepcopy(self.level) if self.level is not None else None

    def has_unsaved_level_changes(self):
        return self.level is not None and self._saved_level_state is not None and self.level != self._saved_level_state

    def confirm_level_switch(self):
        if not self.has_unsaved_level_changes():
            return True
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Warning)
        msg.setWindowTitle("Unsaved changes")
        msg.setText("The current level has unsaved changes.")
        msg.setInformativeText("Do you want to save before loading another level?")
        msg.setStandardButtons(QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        msg.setDefaultButton(QMessageBox.Save)
        choice = msg.exec()
        if choice == QMessageBox.Cancel:
            return False
        if choice == QMessageBox.Discard:
            return True
        return self.save_bin()

    def resolve_deco_names(self, layer):
        """type 0 inherits previous tile; type 1 -> tileTypes[cell]; type 2 -> inline string."""
        out = []; last = None; tt = self.level.get('tileTypes', [])
        for d in layer.get('decorations', []):
            if d.get('type') == 2: nm = d.get('string')
            elif d.get('type') == 1:
                c = d.get('cell', 0); nm = tt[c]['value'] if 0 <= c < len(tt) else None
            else: nm = last
            if nm is not None: last = nm
            out.append(nm)
        return out

    def _tiletype_index(self, name):
        """Index of `name` in this level's tileTypes table, appending it if absent."""
        tt = self.level.setdefault('tileTypes', [])
        for i, t in enumerate(tt):
            if t.get('value') == name: return i
        tt.append({'value': name}); self.level['tileTypeCount'] = len(tt)
        return len(tt) - 1

    def _deco_set_sprite(self, deco, name):
        """Point a decoration at a sprite the way the game expects static tiles: by index into
        the level's tileTypes table (type 1), registering the name there if needed. Inline
        type-2 strings only work for theme-loaded animated sprites (wateranim/watershore), not
        arbitrary static tiles, so the editor never creates type-2 tiles."""
        if not name: return
        deco['type'] = 1
        deco['cell'] = self._tiletype_index(name)
        deco.pop('string', None)

    def _menu(self):
        def act(menu, text, fn, sc=None):
            a = QAction(text, self); a.triggered.connect(fn)
            if sc: a.setShortcut(sc)
            menu.addAction(a); return a
        m = self.menuBar().addMenu("&File")
        self._recent_level_menu = m.addMenu("Open recent level")
        self._recent_multilevel_menu = m.addMenu("Open recent multilevel")
        act(m, "Open .bin…", self.open_bin, "Ctrl+O")
        act(m, "Open multilevel…", self.open_multilevel)
        m.addSeparator()
        act(m, "New blank level…", self.new_level, "Ctrl+N")
        act(m, "New multilevel…", self.new_multilevel)
        act(m, "Set assets folder…", self.set_gfx); m.addSeparator()
        act(m, "Save .bin", self.save_bin, "Ctrl+S"); act(m, "Save .bin As…", self.save_bin_as)
        e = self.menuBar().addMenu("&Edit")
        act(e, "Undo", self.undo, QKeySequence.Undo)
        act(e, "Redo", self.redo, QKeySequence.Redo)
        act(e, "Redo (Ctrl+Shift+Z)", self.redo, "Ctrl+Shift+Z")
        e.addSeparator()
        act(e, "Duplicate", self.duplicate_selection, "Shift+D")
        act(e, "Copy", self.copy_selection, QKeySequence.Copy)
        act(e, "Paste", self.paste_clipboard, QKeySequence.Paste)
        e.addSeparator()
        dele = act(e, "Delete selected", self.delete_selection, QKeySequence.Delete)
        dele.setShortcuts([QKeySequence(QKeySequence.Delete), QKeySequence("Backspace")])
        e.addSeparator()
        mv = e.addMenu("Move selection to layer")
        for i, nm in enumerate(["Background", "Background Front", "Main Bottom", "Main", "Unnamed Layer"]):
            act(mv, f"{i+1}. {nm}", (lambda idx: (lambda: self.move_selection_to_layer(idx)))(i))
        v = self.menuBar().addMenu("&View")
        self.anim_act = QAction("Animate", self, checkable=True); self.anim_act.toggled.connect(self._toggle_anim); v.addAction(self.anim_act)
        act(v, "Fit to level", self._fit)
        self.onion_act = QAction("Onion-skin previous stage", self, checkable=True)
        self.onion_act.setToolTip("Ghost the previous multilevel stage to line stages up (Shift+arrows nudge, [ ] scale)")
        self.onion_act.toggled.connect(self.toggle_onion); v.addAction(self.onion_act)
        wl = self.menuBar().addMenu("Walls")
        self.walls_act = QAction("Edit walls", self, checkable=True)
        self.walls_act.setToolTip("Show collision walls and allow moving/resizing them")
        self.walls_act.toggled.connect(self.toggle_walls_edit); wl.addAction(self.walls_act)
        wl.addSeparator()
        act(wl, "Add wall (blocks everything)", lambda: self.add_wall('everything'))
        act(wl, "Add wall (blocks goo only)", lambda: self.add_wall('goo_only'))
        wl.addSeparator()
        act(wl, "Add point to selected wall", self._wall_add_point)
        act(wl, "Remove last point from selected wall", self._wall_remove_point)
        pa = self.menuBar().addMenu("Paths")
        mvp = pa.addMenu("New path on layer")
        for i, nm in enumerate(["Background", "Background Front", "Main Bottom", "Main", "Unnamed Layer"]):
            act(mvp, f"{i+1}. {nm}", (lambda idx: (lambda: self.add_path(idx)))(i))
        act(pa, "New path (Main layer)", lambda: self.add_path(None))
        pa.addSeparator()
        act(pa, "Add point to last path", self._path_add_point)
        w = self.menuBar().addMenu("&Window")
        act(w, "Asset Browser", self.open_asset_browser, "Ctrl+B")
        act(w, "Entity Report", self.open_entity_report, "Ctrl+R")
        act(w, "Level Settings…", self.open_level_settings, "Ctrl+L")

        m.aboutToShow.connect(self._refresh_recent_menus)

    def _refresh_recent_menus(self):
        def rebuild(menu, key, open_fn, empty_text):
            menu.clear()
            items = self._recent_list(key)
            if not items:
                placeholder = QAction(empty_text, self)
                placeholder.setEnabled(False)
                menu.addAction(placeholder)
                return
            for path in items:
                a = QAction(self._recent_label(path), self)
                a.setToolTip(path)
                a.triggered.connect(lambda _=False, p=path, fn=open_fn: fn(p))
                menu.addAction(a)
        rebuild(self._recent_level_menu, 'recent_levels', self.open_recent_level, 'No recent levels')
        rebuild(self._recent_multilevel_menu, 'recent_multilevels', self.open_recent_multilevel, 'No recent multilevels')

    def open_asset_browser(self):
        if getattr(self, "_browser", None) is None:
            self._browser = AssetBrowser(self)
        self._browser.show(); self._browser.raise_(); self._browser.activateWindow()

    def open_entity_report(self):
        if getattr(self, "_report", None) is None:
            self._report = EntityReport(self)
        self._report.refresh()
        self._report.show(); self._report.raise_(); self._report.activateWindow()

    def open_level_settings(self):
        if getattr(self, "_settings", None) is None:
            self._settings = LevelSettings(self)
        self._settings.refresh()
        self._settings.show(); self._settings.raise_(); self._settings.activateWindow()

    def _spawn_center(self):
        """Center of the current view in scene coordinates."""
        r = self.view.mapToScene(self.view.viewport().rect()).boundingRect()
        return r.center().x(), r.center().y()

    def _layer_index(self, *names, default=0):
        layers = self.level.get('layers', [])
        ln = getattr(self, 'layer_names', [])
        for want in names:
            for i, nm in enumerate(ln):
                if nm == want: return i
        return min(default, len(layers)-1) if layers else 0

    def _sel_objects(self):
        return [i for i in self.scene.selectedItems() if isinstance(i, (EntityItem, DecoItem, WallItem))]

    def _select_refs(self, refs):
        ids = {id(r) for r in refs}
        self.scene.clearSelection()
        first = None
        for it in self.scene.items():
            if isinstance(it, (EntityItem, DecoItem, WallItem)) and id(it.ref) in ids:
                it.setSelected(True); first = first or it
        if first and isinstance(first, (EntityItem, DecoItem)): self.inspector.bind(first)

    def _kind_of(self, item):
        if isinstance(item, WallItem): return 'walls'
        return 'entities' if isinstance(item, EntityItem) else 'decorations'

    def open_canvas_context_menu(self, view_pos, global_pos):
        if self.level is None:
            return
        scene_pos = self.view.mapToScene(view_pos)
        hits = self.scene.items(scene_pos)
        ent = next((it for it in hits if isinstance(it, EntityItem)), None)
        can_paste = bool(getattr(self, '_clip', None))
        menu = QMenu(self)
        if ent is not None:
            self.scene.clearSelection()
            ent.setSelected(True)
            self.inspector.bind(ent)
            copy_act = menu.addAction("Copy")
            dup_act = menu.addAction("Duplicate")
            del_act = menu.addAction("Delete")
            menu.addSeparator()
            paste_act = menu.addAction("Paste")
            paste_act.setEnabled(can_paste)
            picked = menu.exec(global_pos)
            if picked is copy_act:
                self.copy_selection()
            elif picked is dup_act:
                self.duplicate_selection()
            elif picked is del_act:
                self.delete_selection()
            elif picked is paste_act:
                self.paste_clipboard()
            return
        paste_act = menu.addAction("Paste")
        paste_act.setEnabled(can_paste)
        if menu.exec(global_pos) is paste_act:
            self.paste_clipboard()

    def _bake_follower(self, layer, idx):
        """A decoration with type 0 inherits the PREVIOUS tile's sprite. Before removing the
        deco at `idx`, make the next one self-contained if it inherits, so the run-length chain
        doesn't break (which corrupts/crashes the level)."""
        decos = layer.get('decorations', [])
        if 0 <= idx + 1 < len(decos) and decos[idx + 1].get('type') == 0:
            names = self.resolve_deco_names(layer)
            self._deco_set_sprite(decos[idx + 1], names[idx + 1])

    def delete_selection(self):
        items = self._sel_objects()
        if not items or self.level is None: return
        targets = [(self._kind_of(it), it.ref) for it in items]
        n = 0
        for kind, ref in targets:
            for L in self.level['layers']:
                lst = L[kind]; removed = False
                for i, o in enumerate(lst):
                    if o is ref:
                        if kind == 'decorations': self._bake_follower(L, i)
                        del lst[i]; removed = True; n += 1; break
                if removed: break
        self.scene.clearSelection(); self.inspector.bind(None)
        self._populate(refit=False); self.commit()
        self.statusBar().showMessage(f"deleted {n} object(s)")

    def duplicate_selection(self):
        items = self._sel_objects()
        if not items or self.level is None: return
        new = []
        for it in items:
            ref = copy.deepcopy(it.ref); kind = self._kind_of(it)
            if kind == 'walls':
                ref['pos_x'] = ref.get('pos_x', 0.0) + 40.0; ref['pos_y'] = ref.get('pos_y', 0.0) + 40.0
                ref['wall_id'] = self._next_wall_id()
                self.level['layers'][it.layer_idx]['walls'].append(ref); new.append(ref); continue
            if kind == 'decorations':
                self._deco_set_sprite(ref, getattr(it, '_name', None))
            k = ENTITY_K if kind == 'entities' else DECO_K
            off = 40.0 / k                                  # ~40 scene units, so the copy is visible
            ref['position'] = [ref['position'][0] + off, ref['position'][1] + off]
            self.level['layers'][it.layer_idx][kind].append(ref); new.append(ref)
        self._populate(refit=False); self.commit(); self._select_refs(new)
        self.statusBar().showMessage(f"duplicated {len(new)} object(s)")

    def copy_selection(self):
        items = self._sel_objects()
        if not items: return
        self._clip = [(self._kind_of(it), it.layer_idx, copy.deepcopy(it.ref),
                       getattr(it, '_name', None) if isinstance(it, DecoItem) else None) for it in items]
        self.statusBar().showMessage(f"copied {len(items)} object(s)")

    def paste_clipboard(self):
        clip = getattr(self, '_clip', None)
        if not clip or self.level is None: return
        nlayers = len(self.level['layers']); new = []
        for kind, li, ref, nm in clip:
            r = copy.deepcopy(ref); li = min(li, nlayers - 1)
            if kind == 'walls':
                r['pos_x'] = r.get('pos_x', 0.0) + 40.0; r['pos_y'] = r.get('pos_y', 0.0) + 40.0
                r['wall_id'] = self._next_wall_id()
                self.level['layers'][li]['walls'].append(r); new.append(r); continue
            k = ENTITY_K if kind == 'entities' else DECO_K
            off = 40.0 / k
            r['position'] = [r['position'][0] + off, r['position'][1] + off]
            if kind == 'decorations' and nm:
                # register the sprite in this level's tileTypes and reference it by index (type 1)
                # so a cross-level paste doesn't keep a stale index from the source level
                self._deco_set_sprite(r, nm)
            self.level['layers'][li][kind].append(r); new.append(r)
        self._populate(refit=False); self.commit(); self._select_refs(new)
        self.statusBar().showMessage(f"pasted {len(new)} object(s)")

    def convert_object(self, item, to_kind):
        """Convert a selected object between entity (eaten) and decoration (background),
        preserving position, rotation, color and approximate rendered size."""
        if self.level is None or item is None: return
        is_ent = isinstance(item, EntityItem)
        if (is_ent and to_kind == 'entity') or ((not is_ent) and to_kind == 'deco'): return
        ref = item.ref; li = item.layer_idx
        name = ref.get('type') if is_ent else (getattr(item, '_name', None) or ref.get('string'))
        if not name: return
        src = 'entities' if is_ent else 'decorations'
        lst = self.level['layers'][li][src]
        for i, o in enumerate(lst):
            if o is ref: del lst[i]; break
        w = max(getattr(item, 'w', 0), 1); h = max(getattr(item, 'h', 0), 1)
        if to_kind == 'deco':                                   # entity -> decoration
            scale = entity_render_scale(ref.get('mass', 300.0), w, h)
            dim = max(1, int(round(scale * DIM_BASE)))
            new = {'position': [ref['position'][0] * ENTITY_K, ref['position'][1] * ENTITY_K],
                   'size': {'raw': ref.get('rotation', {}).get('raw', 0)}, 'extra_flag': 0,
                   'color_rgba': list(ref.get('color', {}).get('rgba', [255, 255, 255, 255])),
                   'dimensions': {'raw': [dim, dim]}, 'priority': {'total': 50}}
            self._deco_set_sprite(new, name)
            self.level['layers'][li]['decorations'].append(new)
        else:                                                   # decoration -> entity
            # invert entity_render_scale: scale = sqrt(mass/(w*h))*boost  ->  mass = (scale/boost)^2*w*h
            scale = (ref.get('dimensions', {}).get('raw', [DIM_BASE])[0] or DIM_BASE) / DIM_BASE
            asp = max(w, h) / max(min(w, h), 1)
            A_LO, A_PEAK, BMAX, DECAY = 2.1, 3.2, 1.8, 0.38
            if asp <= A_LO:
                boost = 1.0
            elif asp <= A_PEAK:
                boost = 1.0 + (asp - A_LO) / (A_PEAK - A_LO) * (BMAX - 1.0)
            else:
                boost = max(1.0, BMAX - DECAY * (asp - A_PEAK))
            mass = (scale / boost) ** 2 * w * h
            f158, f15c = self._entity_field_defaults()
            new = {'type': name,
                   'position': [ref['position'][0] / ENTITY_K, ref['position'][1] / ENTITY_K],
                   'field_158': f158, 'field_15c': f15c, 'vec': {'raw': [0, 0]}, 'field_250': {'raw': 0},
                   'rotation': {'raw': ref.get('size', {}).get('raw', 0)}, 'has_box': 0,
                   'color': {'rgba': list(ref.get('color_rgba', [255, 255, 255, 255]))},
                   'mass': float(max(mass, 1.0)), 'priority': 1000,
                   'has_move_direction': 0, 'has_path_follow': 0, 'has_emitter': 0}
            self.level['layers'][li]['entities'].append(new)
        self._populate(refit=False); self.commit()
        self.statusBar().showMessage(f"converted '{name}' to {to_kind}")

    def _move_ref(self, item, target_idx):
        """Move one object's ref to target layer (no repopulate/commit). Returns its ref or None."""
        if self.level is None or item is None: return None
        ref = item.ref; kind = self._kind_of(item); layers = self.level['layers']
        if not (0 <= target_idx < len(layers)): return None
        src = None
        for L in layers:
            for i, o in enumerate(L[kind]):
                if o is ref:
                    if kind == 'decorations': self._bake_follower(L, i)
                    del L[kind][i]; src = L; break
            if src is not None: break
        if src is None: return None
        # re-register a moved decoration's sprite as a tileTypes index (type 1) so it doesn't
        # depend on neighbours in its new layer and isn't a crash-prone inline type-2 tile
        if kind == 'decorations':
            self._deco_set_sprite(ref, getattr(item, '_name', None))
        layers[target_idx][kind].append(ref)
        return ref

    def move_to_layer(self, item, target_idx):
        if self._move_ref(item, target_idx) is None: return
        self._populate(refit=False); self.commit()
        self.statusBar().showMessage(f"moved to layer '{self.layer_names[target_idx]}'")

    def move_selection_to_layer(self, target_idx):
        items = self._sel_objects()
        if not items or self.level is None: return
        target_idx = max(0, min(target_idx, len(self.level['layers']) - 1))
        refs = [r for r in (self._move_ref(it, target_idx) for it in items) if r is not None]
        if not refs: return
        self._populate(refit=False); self.commit(); self._select_refs(refs)
        self.statusBar().showMessage(f"moved {len(refs)} object(s) to '{self.layer_names[target_idx]}'")

    def spawn_decoration(self, cell_name, layer_idx=None):
        if self.level is None: return
        cx, cy = self._spawn_center()
        li = layer_idx if layer_idx is not None else self._layer_index('Main Bottom', 'Main', default=2)
        deco = {'position': [cx / DECO_K, cy / DECO_K],
                'size': {'raw': 0}, 'extra_flag': 0, 'color_rgba': [255, 255, 255, 255],
                'dimensions': {'raw': [int(DIM_BASE), int(DIM_BASE)]}, 'priority': {'total': 50}}
        self._deco_set_sprite(deco, cell_name)
        self.level['layers'][li]['decorations'].append(deco)
        self._populate(refit=False); self.commit()
        self.statusBar().showMessage(f"spawned decoration '{cell_name}' on '{self.layer_names[li]}'")

    def _entity_field_defaults(self):
        """Match the level's existing entities for the flags that govern cross-stage persistence
        (field_158) and field_15c. Newly spawned entities used to hardcode 0, which is why they
        didn't persist in a multilevel while copied entities (carrying the level's value) did."""
        c158 = collections.Counter(); c15c = collections.Counter()
        for L in self.level.get('layers', []):
            for e in L.get('entities', []):
                c158[e.get('field_158', 1)] += 1; c15c[e.get('field_15c', 0)] += 1
        return (c158.most_common(1)[0][0] if c158 else 1,
                c15c.most_common(1)[0][0] if c15c else 0)

    def spawn_entity(self, type_name, mass=300.0, layer_idx=None):
        if self.level is None: return
        cx, cy = self._spawn_center()
        li = layer_idx if layer_idx is not None else self._layer_index('Main', 'Main Bottom', default=3)
        f158, f15c = self._entity_field_defaults()
        ent = {'type': type_name, 'position': [cx / ENTITY_K, cy / ENTITY_K],
               'field_158': f158, 'field_15c': f15c, 'vec': {'raw': [0, 0]}, 'field_250': {'raw': 0},
               'rotation': {'raw': 0}, 'has_box': 0, 'color': {'rgba': [255, 255, 255, 255]},
               'mass': float(mass), 'priority': 1000,
               'has_move_direction': 0, 'has_path_follow': 0, 'has_emitter': 0}
        self.level['layers'][li]['entities'].append(ent)
        self._populate(refit=False); self.commit()
        persist = " (persists)" if f158 else ""
        self.statusBar().showMessage(f"spawned entity '{type_name}' on '{self.layer_names[li]}'{persist}")

    @staticmethod
    def _read_edges(xml):
        if not os.path.isfile(xml): return None
        t = open(xml, encoding="utf-8", errors="ignore").read()
        g = lambda k: float(re.search(k + r'="([0-9.eE+\-]+)"', t).group(1))
        try: return g("edgeleft"), g("edgetop"), g("edgeright"), g("edgebottom")
        except AttributeError: return None
    @staticmethod
    def _read_layer_names(xml):
        if not os.path.isfile(xml): return []
        return re.findall(r'<layer\s+name="([^"]+)"', open(xml, encoding="utf-8", errors="ignore").read())

    def _find_multilevel(self):
        """Locate the multilevel XML that references the open level by name, returning
        (MultiLevel, path, stage_dict) or (None, None, None). Searches the level's folder, its
        subfolders (e.g. a 'multilevels' subfolder), the parent and its subfolders, plus any
        folders the user has pointed at this session."""
        if not getattr(self, 'path', None): return (None, None, None)
        name = os.path.splitext(os.path.basename(self.path))[0]
        folder = os.path.dirname(self.path); parent = os.path.dirname(folder)
        cands = [folder, os.path.join(folder, 'multilevels'),
                 parent, os.path.join(parent, 'multilevels')]
        for base in (folder, parent):
            cands += [d for d in glob.glob(os.path.join(base, '*')) if os.path.isdir(d)]
        cands += getattr(self, '_ml_search_dirs', [])
        seen = set()
        for d in cands:
            if not d or d in seen or not os.path.isdir(d): continue
            seen.add(d)
            for fn in sorted(glob.glob(os.path.join(d, "*.xml"))):
                try:
                    txt = open(fn, encoding='utf-8', errors='ignore').read()
                except OSError:
                    continue
                if '<multilevel' not in txt or f'name="{name}"' not in txt:
                    continue
                try:
                    ml = TM.MultiLevel(fn)
                except Exception:
                    continue
                for st in ml.stages:
                    if st.get('name') == name:
                        return (ml, fn, st)
        return (None, None, None)

    def open_bin(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Open level .bin", self._levels_dir(), "Level (*.bin)")
        if fn:
            self._load(fn)
            self._push_recent('recent_levels', fn)

    def open_recent_level(self, fn):
        if not fn or not os.path.isfile(fn):
            self.statusBar().showMessage(f"recent level not found: {fn}")
            return
        if not self.confirm_level_switch():
            return
        self._load(fn)
        self._push_recent('recent_levels', fn)

    def _load(self, fn):
        self.path = fn; self.level = read_level(fn)
        xml = os.path.splitext(fn)[0] + ".xml"
        self.edges = self._read_edges(xml) or (-1600.0, -1200.0, 1600.0, 1200.0)
        names = self._read_layer_names(xml)
        n = self.level.get('layerCount', len(self.level.get('layers', [])))
        self.layer_names = (names + [f"Layer {i}" for i in range(len(names), n)])[:n]
        self.history = [copy.deepcopy(self.level)]; self.hist_idx = 0
        # cache this stage's meterperpix (for eat-size in cm); None if not part of a multilevel
        try:
            _, _, st = self._find_multilevel()
            self._mpp = float(st['meterperpix']) if st and st.get('meterperpix') else None
        except Exception:
            self._mpp = None
        self._populate(refit=True)
        if self._onion_on:                                  # rebuild ghost for the newly-loaded stage
            self._load_onion_data(); self._refresh_onion()
        self._mark_level_saved()
    def set_gfx(self):
        d = QFileDialog.getExistingDirectory(self, "Select game assets folder", self._assets_root)
        if not d: return
        if not self._validate_assets_root(d):
            QMessageBox.warning(self, "Invalid assets folder",
                                "Choose the game's assets folder that contains both 'graphics' and 'levels'.")
            return
        self._assets_root = d
        self.gfx = self._graphics_dir()
        self._cfg.pop('graphics_path', None)
        self.assets = self._load_assets(); self._pm.clear()
        self._save_setting('assets_root', d)
        if self.level: self._populate(refit=False)

    def save_bin(self):
        if not self.level: return False
        if not self.path: return self.save_bin_as()
        write_level(self.level, self.path); self.statusBar().showMessage(f"saved {self.path}")
        self._mark_level_saved()
        self._push_recent('recent_levels', self.path)
        return True
    def save_bin_as(self):
        if not self.level: return False
        fn, _ = QFileDialog.getSaveFileName(self, "Save .bin", self.path or self._levels_dir(), "Level (*.bin)")
        if not fn:
            return False
        self.path = fn
        ok = self.save_bin()
        if ok:
            self._push_recent('recent_levels', fn)
        return ok

    def open_multilevel(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Open multilevel", self._multilevels_dir(), "Multilevel (*.xml)")
        if not fn: return
        try: ml = TM.MultiLevel(fn)
        except Exception as ex: self.statusBar().showMessage(f"not a multilevel: {ex}"); return
        self.mldock.load(ml, fn)
        if ml.stages:
            if not self.load_stage(ml.stages[0]['name'], fn):
                return
        self._push_recent('recent_multilevels', fn)

    def open_recent_multilevel(self, fn):
        if not fn or not os.path.isfile(fn):
            self.statusBar().showMessage(f"recent multilevel not found: {fn}")
            return
        if not self.confirm_level_switch():
            return
        try: ml = TM.MultiLevel(fn)
        except Exception as ex:
            self.statusBar().showMessage(f"not a multilevel: {ex}"); return
        self.mldock.load(ml, fn)
        if ml.stages:
            if not self.load_stage(ml.stages[0]['name'], fn):
                return
        self._push_recent('recent_multilevels', fn)

    def new_level(self):
        """Create a fresh blank .bin + .xml shell and open it for editing."""
        fn, _ = QFileDialog.getSaveFileName(self, "New blank level .bin", self._levels_dir(), "Level (*.bin)")
        if not fn: return
        if not fn.lower().endswith(".bin"): fn += ".bin"
        try:
            write_blank_level(fn)
        except Exception as ex:
            self.statusBar().showMessage(f"could not create level: {ex}"); return
        self._load(fn)
        self.statusBar().showMessage(f"new blank level {os.path.basename(fn)} — add it to a multilevel to set its goal/zoom")

    def new_multilevel(self):
        """Start an empty multilevel in the builder dock."""
        self.mldock.load(new_multilevel_model(), None)
        self.mldock.raise_()
        self.statusBar().showMessage("new multilevel — add stages with '+ blank', then Save As into assets/levels/multilevels/")

    def _resolve_stage(self, name, ml_path):
        base = os.path.dirname(ml_path)
        for c in (base, os.path.dirname(base)):
            p = os.path.join(c, name + ".bin")
            if os.path.isfile(p): return p
    def load_stage(self, name, ml_path):
        if not self.confirm_level_switch():
            return False
        p = self._resolve_stage(name, ml_path)
        if not p:
            p, _ = QFileDialog.getOpenFileName(self, f"Locate {name}.bin", self._levels_dir(), "Level (*.bin)")
            if not p: return False
        self._load(p)
        self._push_recent('recent_levels', p)
        self.statusBar().showMessage(f"editing stage {name}")
        return True

    # ---- history ----
    def commit(self):
        if self.level is None: return
        self.history = self.history[:self.hist_idx + 1]
        self.history.append(copy.deepcopy(self.level))
        if len(self.history) > HIST_MAX:
            self.history.pop(0)
        self.hist_idx = len(self.history) - 1
    def undo(self):
        if self.hist_idx > 0:
            self.hist_idx -= 1; self.level = copy.deepcopy(self.history[self.hist_idx])
            self._populate(refit=False); self.statusBar().showMessage("undo")
    def redo(self):
        if self.hist_idx < len(self.history) - 1:
            self.hist_idx += 1; self.level = copy.deepcopy(self.history[self.hist_idx])
            self._populate(refit=False); self.statusBar().showMessage("redo")

    # ---- populate ----
    def _populate(self, refit=True):
        vt = self.view.transform()
        self.scene.clear()
        self._wall_handles = []; self._sel_wall = None     # scene.clear() destroyed any handles
        self._onion = None                                  # ...and the onion group
        el, et, er, eb = self.edges
        pf = QGraphicsRectItem(el, et, er - el, eb - et)
        pf.setPen(QPen(QColor("#454b57"), 3)); pf.setZValue(-10000); self.scene.addItem(pf)
        self._bnd_rect = pf
        self._bnd_handles = [BoundaryHandle(c, self) for c in range(4)]
        for h in self._bnd_handles: self.scene.addItem(h)
        self._update_boundary()

        self.items_by_layer = []
        for li, L in enumerate(self.level.get('layers', [])):
            items = {'decorations': [], 'entities': [], 'paths': [], 'walls': []}
            names = self.resolve_deco_names(L)
            for d, nm in zip(L.get('decorations', []), names):
                it = DecoItem(d, li, self, nm, self.bg_editable)
                if it.pixmap().isNull(): continue
                self.scene.addItem(it); items['decorations'].append(it)
            for ent in L.get('entities', []):
                it = EntityItem(ent, li, self); self.scene.addItem(it); items['entities'].append(it)
            for path in L.get('paths', []):
                pit = PathItem(path, li, self); self.scene.addItem(pit); items['paths'].append(pit)
                for j in range(len(path.get('spline_points', []))):
                    wp = WaypointItem(path, j, pit, self); self.scene.addItem(wp); items['paths'].append(wp)
            for wall in L.get('walls', []):
                for it in self._make_walls(wall, li):
                    self.scene.addItem(it); items['walls'].append(it)
            self.items_by_layer.append(items)

        self.layers.rebuild(self.layer_names)
        self._set_scene_rect()
        if refit: self._fit()
        else: self.view.setTransform(vt)
        self.apply_visibility()
        if self._onion_on and self._onion_data: self._refresh_onion()   # scene.clear() removed it
        layers = self.level.get('layers', [])
        ne = sum(len(L.get('entities', [])) for L in layers)
        nd = sum(len(L.get('decorations', [])) for L in layers)
        self.statusBar().showMessage(
            f"{len(layers)} layers · {ne} entities · {nd} decorations · edges {int(er-el)}×{int(eb-et)} · "
            f"sprites {'ON' if self.assets.ok else 'OFF'} · bg-edit {'ON' if self.bg_editable else 'off'}")

    def _make_walls(self, wall, li):
        return [WallItem(wall, li, self, getattr(self, 'walls_editable', False))]

    def _update_boundary(self):
        if not getattr(self, '_bnd_rect', None): return
        el, et, er, eb = self.edges
        self._bnd_block = True
        self._bnd_rect.setRect(el, et, er - el, eb - et)
        for h, (x, y) in zip(self._bnd_handles, [(el, et), (er, et), (er, eb), (el, eb)]):
            h.setPos(x, y)
        self._bnd_block = False

    def _edge_from_handle(self, corner, x, y):
        el, et, er, eb = self.edges
        MIN = 50.0
        if corner == 0:   el, et = x, y      # top-left
        elif corner == 1: er, et = x, y      # top-right
        elif corner == 2: er, eb = x, y      # bottom-right
        elif corner == 3: el, eb = x, y      # bottom-left
        if el > er - MIN:
            if corner in (0, 3): el = er - MIN
            else: er = el + MIN
        if et > eb - MIN:
            if corner in (0, 1): et = eb - MIN
            else: eb = et + MIN
        self.edges = (el, et, er, eb)
        self._update_boundary(); self._set_scene_rect()
        self.statusBar().showMessage(f"boundary {int(er - el)}×{int(eb - et)}  ·  drag corners; saved to .xml on release")

    # ---- collision walls ----
    @staticmethod
    def _rect_wall_verts(width, length):
        """Normalized vertices (x spans ±0.5) for a W×H rectangle; world = vertex*width."""
        hy = (0.5 * length / width) if width else 0.5
        return [[-0.5, -hy], [0.5, -hy], [0.5, hy], [-0.5, hy]]

    def _next_wall_id(self):
        ids = [w.get('wall_id', 0) for L in self.level.get('layers', []) for w in L.get('walls', [])]
        return (max(ids) + 1) if ids else 1000

    def add_wall(self, blocks='everything'):
        if self.level is None: return
        cx, cy = self._spawn_center()
        li = self._layer_index('Main', 'Main Bottom', default=3)
        W, H = 800.0, 140.0
        wall = {'pos_x': float(cx), 'pos_y': float(cy), 'width': W, 'length': H,
                'wall_type_name': blocks, 'has_shapes_flag': 0,
                'shapes': [{'shape_type_name': 'poly', 'data': {'vertices': self._rect_wall_verts(W, H)}}],
                'reserved': 0, 'wall_id': self._next_wall_id()}
        if not self.walls_editable:
            self.walls_editable = True
            if hasattr(self, 'walls_act'): self.walls_act.setChecked(True)
        self.level['layers'][li]['walls'].append(wall)
        self._populate(refit=False); self.commit()
        for it in self.scene.items():
            if isinstance(it, WallItem) and it.ref is wall:
                self.scene.clearSelection(); it.setSelected(True); break
        self.statusBar().showMessage(f"added a {blocks} wall on '{self.layer_names[li]}' — drag it / its corners to fit")

    def toggle_walls_edit(self, on):
        self.walls_editable = bool(on)
        self._populate(refit=False)
        self.statusBar().showMessage("wall editing " + ("on — click a wall to move/resize it" if on else "off"))

    # ---- paths (splines that entities can follow) ----
    def add_path(self, layer_idx=None):
        if self.level is None: return
        li = layer_idx if layer_idx is not None else self._layer_index('Main', 'Main Bottom', default=3)
        el, et, er, eb = self.edges
        cy = (et + eb) / 2; x0 = el + (er - el) * 0.2; x1 = er - (er - el) * 0.2
        n = 5
        sps = [{'p0': [x0 + (x1 - x0) * k / (n - 1), cy], 'p1': [0, 0], 'p2': [0, 0]} for k in range(n)]
        existing = [p.get('path_name') for L in self.level['layers'] for p in L['paths']]
        k = 1
        while f"path{k}" in existing: k += 1
        name = f"path{k}"
        ids = [p.get('internal_id_guess', 0) for L in self.level['layers'] for p in L['paths']]
        path = {'path_name': name, 'position': [0.0, 0.0], 'extent_x_guess': 0.0, 'extent_y_guess': 0.0,
                'path_flag': 1, 'spline_points': sps, 'internal_id_guess': (max(ids) + 1) if ids else 1000}
        recompute_path(path)
        self.level['layers'][li]['paths'].append(path); self._last_path = path
        self._populate(refit=False); self.commit()
        self.statusBar().showMessage(f"added path '{name}' on '{self.layer_names[li]}' — drag the dots to shape it · "
                                     "double-click the line to add a point · double-click a dot to remove · "
                                     "then select an entity and pick it under 'follow path'")

    def _recompute_path(self, path):
        recompute_path(path)

    def _path_add_point_at(self, path, x, y):
        sps = path.get('spline_points', [])
        if len(sps) < 2:
            sps.append({'p0': [x, y], 'p1': [0, 0], 'p2': [0, 0]})
        else:
            best, bd = 0, 1e30
            for i in range(len(sps) - 1):
                d = _point_seg_dist(x, y, sps[i]['p0'], sps[i + 1]['p0'])
                if d < bd: bd, best = d, i
            sps.insert(best + 1, {'p0': [x, y], 'p1': [0, 0], 'p2': [0, 0]})
        recompute_path(path); self._last_path = path
        self._populate(refit=False); self.commit()
        self.statusBar().showMessage("added a path point")

    def _path_add_point(self):
        p = getattr(self, '_last_path', None)
        if not p:
            self.statusBar().showMessage("add a path first (Paths → New path), or drag one of its points"); return
        sps = p['spline_points']
        if len(sps) >= 2:
            a, b = sps[0]['p0'], sps[1]['p0']
            self._path_add_point_at(p, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2)

    def _path_remove_point(self, path, idx):
        sps = path.get('spline_points', [])
        if len(sps) > 2 and 0 <= idx < len(sps):
            del sps[idx]; recompute_path(path); self._last_path = path
            self._populate(refit=False); self.commit()
            self.statusBar().showMessage("removed a path point")
        else:
            self.statusBar().showMessage("a path needs at least 2 points")

    def _wall_add_point(self):
        w = getattr(self, '_sel_wall', None)
        if w is None:
            self.statusBar().showMessage("select a wall first (turn on Walls → Edit walls)"); return
        wv = w.world_verts()
        if len(wv) >= 2:
            a, b = wv[0], wv[1]
            wv.insert(1, [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2])
            w.set_world_verts(wv); self._rebuild_wall_handles(); self.commit()
            self.statusBar().showMessage("added a point — drag it to shape the wall")

    def _wall_remove_point(self):
        w = getattr(self, '_sel_wall', None)
        if w is None: return
        if w.remove_vertex(len(w.world_verts()) - 1):
            self._rebuild_wall_handles(); self.commit()
            self.statusBar().showMessage("removed a point")
        else:
            self.statusBar().showMessage("a wall needs at least 3 points")

    def _clear_wall_handles(self):
        for h in getattr(self, '_wall_handles', []):
            try:
                if h.scene(): self.scene.removeItem(h)
            except RuntimeError:
                pass
        self._wall_handles = []; self._sel_wall = None

    def _rebuild_wall_handles(self):
        """Force-recreate handles for the selected wall (after add/remove vertex)."""
        w = self._sel_wall
        for h in self._wall_handles:
            try:
                if h.scene(): self.scene.removeItem(h)
            except RuntimeError:
                pass
        self._wall_handles = []
        if w is not None:
            self._wall_handles = [WallVertexHandle(i, w, self) for i in range(len(w.world_verts()))]
            for h in self._wall_handles: self.scene.addItem(h)
            self._update_wall_handles()

    def _sync_wall_handles(self):
        if not getattr(self, 'walls_editable', False):
            self._clear_wall_handles(); return
        sel = self.scene.selectedItems()
        walls = [it for it in sel if isinstance(it, WallItem)]
        others = [it for it in sel if isinstance(it, (EntityItem, DecoItem))]
        if len(walls) == 1:
            w = walls[0]; n = len(w.world_verts())
            if self._sel_wall is not w or len(self._wall_handles) != n:
                self._sel_wall = w; self._rebuild_wall_handles()
            else:
                self._update_wall_handles()
        elif others:
            self._clear_wall_handles()
        # Empty selection: keep handles. Pressing a handle clears the wall's selection (handles
        # aren't selectable); tearing them down here would drop the drag onto the wall body.

    def _update_wall_handles(self):
        w = getattr(self, '_sel_wall', None)
        if not w or not self._wall_handles: return
        wv = w.world_verts()
        self._wall_block = True
        for h in self._wall_handles:
            if 0 <= h.index < len(wv): h.setPos(wv[h.index][0], wv[h.index][1])
        self._wall_block = False

    # ---- onion-skin: ghost of the previous multilevel stage, to align stages ----
    def toggle_onion(self, on):
        self._onion_on = bool(on)
        if on:
            if self._load_onion_data(): self._refresh_onion()
        else:
            self._clear_onion()
            self.statusBar().showMessage("onion-skin off")

    def _clear_onion(self):
        g = getattr(self, '_onion', None)
        if g is not None:
            try:
                if g.scene(): self.scene.removeItem(g)
            except RuntimeError:
                pass                                        # C++ object already gone (scene was cleared)
        self._onion = None

    def _load_onion_data(self):
        self._onion_data = None
        ml, mlp, st = self._find_multilevel()
        if not ml or st is None:
            self.statusBar().showMessage("onion-skin: this level isn't part of a multilevel"); return False
        names = [s.get('name') for s in ml.stages]
        cur = os.path.splitext(os.path.basename(self.path))[0] if self.path else None
        if cur not in names:
            self.statusBar().showMessage("onion-skin: current stage isn't listed in its multilevel"); return False
        i = names.index(cur)
        if i > 0:
            other = ml.stages[i - 1]; rel = "previous"
        elif i + 1 < len(ml.stages):
            other = ml.stages[i + 1]; rel = "next"
        else:
            self.statusBar().showMessage("onion-skin: this multilevel has only one stage"); return False
        pb = self._resolve_stage(other.get('name'), mlp)
        if not pb:
            self.statusBar().showMessage(f"onion-skin: couldn't find {other.get('name')}.bin"); return False
        try: other_lv = read_level(pb)
        except Exception as ex:
            self.statusBar().showMessage(f"onion-skin: {ex}"); return False
        try: scale = float(other.get('meterperpix')) / float(st.get('meterperpix'))
        except Exception: scale = 1.0
        try:
            dx = float(st.get('posx', 0)) - float(other.get('posx', 0))
            dy = float(st.get('posy', 0)) - float(other.get('posy', 0))
        except Exception: dx = dy = 0.0
        self._onion_data = {'lv': other_lv, 'scale': scale, 'dx': dx, 'dy': dy,
                            'edges': self._read_edges(os.path.splitext(pb)[0] + ".xml"), 'name': other.get('name')}
        self._onion_nudge = [0.0, 0.0]; self._onion_scale_mult = 1.0
        self.statusBar().showMessage(f"onion-skin: {rel} stage '{other.get('name')}' ×{scale:.3f}  ·  Shift+arrows nudge, [ ] scale")
        return True

    def _refresh_onion(self):
        self._clear_onion()
        d = getattr(self, '_onion_data', None)
        if not (self._onion_on and d): return
        lv = d['lv']; A = self.assets
        g = QGraphicsItemGroup(); g.setHandlesChildEvents(False)
        if d['edges']:
            el, et, er, eb = d['edges']
            bpen = QPen(QColor(SELECT)); bpen.setCosmetic(True); bpen.setWidthF(2.0); bpen.setStyle(Qt.DashLine)
            rb = QGraphicsRectItem(el, et, er - el, eb - et); rb.setPen(bpen); rb.setBrush(Qt.NoBrush)
            rb.setAcceptedMouseButtons(Qt.NoButton); g.addToGroup(rb)
        tt = lv.get('tileTypes', [])
        for L in lv['layers']:
            last = None
            for dd in L['decorations']:
                t = dd.get('type')
                if t == 2: nm = dd.get('string')
                elif t == 1:
                    c = dd.get('cell', 0); nm = tt[c]['value'] if 0 <= c < len(tt) else None
                else: nm = last
                if nm is not None: last = nm
                spr = self.sprite_pixmap(nm, tuple(dd.get('color_rgba', [255, 255, 255, 255])), 0) if nm else None
                if spr is None: continue
                it = QGraphicsPixmapItem(spr); it.setOffset(-spr.width() / 2, -spr.height() / 2)
                dims = dd.get('dimensions', {}).get('raw', [DIM_BASE, DIM_BASE])
                it.setScale(max((dims[0] or DIM_BASE) / DIM_BASE, 0.001))
                it.setRotation(-dd.get('size', {}).get('raw', 0) / 100.0)
                it.setPos(dd['position'][0] * DECO_K, dd['position'][1] * DECO_K)
                it.setAcceptedMouseButtons(Qt.NoButton); g.addToGroup(it)
            for e in L['entities']:
                nm = e.get('type')
                spr = self.sprite_pixmap(nm, tuple(e.get('color', {}).get('rgba', [255, 255, 255, 255])), 0)
                if spr is None: continue
                px, py = A.pivot(nm)
                it = QGraphicsPixmapItem(spr); it.setOffset(-px * spr.width(), -py * spr.height())
                it.setScale(entity_render_scale(e.get('mass', 1.0), spr.width(), spr.height()))
                it.setRotation(-e.get('rotation', {}).get('raw', 0) / 100.0)
                it.setPos(e['position'][0] * ENTITY_K, e['position'][1] * ENTITY_K)
                it.setAcceptedMouseButtons(Qt.NoButton); g.addToGroup(it)
        g.setOpacity(0.5); g.setZValue(16000); g.setAcceptedMouseButtons(Qt.NoButton)
        g.setScale(d['scale'] * self._onion_scale_mult)
        g.setPos(d['dx'] + self._onion_nudge[0], d['dy'] + self._onion_nudge[1])
        self.scene.addItem(g); self._onion = g

    def keyPressEvent(self, ev):
        if self._onion_on and self._onion is not None and self._onion_data:
            k = ev.key(); step = 25.0
            if (ev.modifiers() & Qt.ShiftModifier) and k in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
                if k == Qt.Key_Left:  self._onion_nudge[0] -= step
                elif k == Qt.Key_Right: self._onion_nudge[0] += step
                elif k == Qt.Key_Up:   self._onion_nudge[1] -= step
                else:                  self._onion_nudge[1] += step
                self._refresh_onion()
                self.statusBar().showMessage(f"onion offset ({self._onion_nudge[0]:.0f}, {self._onion_nudge[1]:.0f})"); return
            if k in (Qt.Key_BracketLeft, Qt.Key_BracketRight):
                self._onion_scale_mult *= 0.98 if k == Qt.Key_BracketLeft else 1.02
                self._refresh_onion()
                self.statusBar().showMessage(f"onion scale ×{self._onion_data['scale'] * self._onion_scale_mult:.3f}"); return
        super().keyPressEvent(ev)

    def _persist_edges(self):
        xml = (os.path.splitext(self.path)[0] + ".xml") if getattr(self, 'path', None) else None
        if not xml or not os.path.isfile(xml):
            self.statusBar().showMessage("no .xml shell found to save the boundary into"); return
        t = open(xml, encoding="utf-8", errors="ignore").read()
        el, et, er, eb = self.edges
        for k, v in (("edgeleft", el), ("edgetop", et), ("edgeright", er), ("edgebottom", eb)):
            t = re.sub(k + r'="[0-9.eE+\-]+"', f'{k}="{v:.6f}"', t, count=1)
        open(xml, 'w', encoding='utf-8').write(t)
        self.statusBar().showMessage(f"boundary saved to {os.path.basename(xml)}  ({int(er-el)}×{int(eb-et)})")

    def _set_scene_rect(self):
        r = self.scene.itemsBoundingRect(); el, et, er, eb = self.edges
        r = r.united(QRectF(el, et, er - el, eb - et)).adjusted(-200, -200, 200, 200)
        self.scene.setSceneRect(r)
    def _fit(self):
        el, et, er, eb = self.edges; self.view.fitInView(QRectF(el, et, er - el, eb - et), Qt.KeepAspectRatio)

    # ---- background editability ----
    def set_bg_editable(self, on):
        self.bg_editable = on
        for items in self.items_by_layer:
            for it in items['decorations']:
                it.set_editable(on)
        self.statusBar().showMessage(f"background editing {'ON' if on else 'off'}")

    # ---- visibility ----
    def apply_visibility(self, *_):
        if not self.items_by_layer: return
        tvis = {t: c.isChecked() for t, c in self.layers.type_checks.items()}
        lvis = [c.isChecked() for c in self.layers.layer_checks]
        dim = self.layers.dim_bg.isChecked()
        main_idx = self.layer_names.index("Main") if "Main" in self.layer_names else len(self.items_by_layer) - 1
        for li, items in enumerate(self.items_by_layer):
            lv = lvis[li] if li < len(lvis) else True
            for t, lst in items.items():
                for it in lst:
                    it.setVisible(lv and tvis.get(t, True))
                    it.setOpacity(0.25 if (dim and li < main_idx) else 1.0)

    # ---- selection / animation ----
    def _sel(self):
        sel = [i for i in self.scene.selectedItems() if isinstance(i, (EntityItem, DecoItem))]
        self.inspector.bind(sel[0] if sel else None)
        self._sync_wall_handles()
    def _toggle_anim(self, on):
        if on: self.timer.start(50)
        else:
            self.timer.stop()
            for items in self.items_by_layer:
                for it in items['entities']: it.frame = 0; it.setOpacity(1.0); it.rebuild(); it.place()
            self.apply_visibility()
    def _anim_step(self):
        self.anim_t += 0.05
        for items in self.items_by_layer:
            for it in items['entities']:
                if it.fcount < 2: continue
                a = self.assets.resolve_anim(it.ref['type'])
                if not a: continue
                idx = a.frames.index(a.frame_at(self.anim_t))
                if idx != it.frame: it.frame = idx; it.rebuild()
                _, _, ang, sc, op = self.assets.frame_transform(it.ref['type'], it.frame)
                it.setRotation(-it.ref['rotation']['raw'] / 100.0 + ang); it.setOpacity(op)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10)); app.setStyleSheet(QSS)
    Main().show(); sys.exit(app.exec())
