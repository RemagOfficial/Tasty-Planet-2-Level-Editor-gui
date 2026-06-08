"""Tasty Planet 2 multilevel parser/serializer.

A 'multilevel' (e.g. levels/multilevels/lab1.xml) is the layer/zoom system: an
ordered list of stage-levels, each a separate .bin/.xml scene at its own scale
(meterperpix). As the goo's area crosses a stage's triggerarea, the game advances
to the next, more-zoomed-out stage, so big objects shrink away and new ones appear.

Note: this is XML parsed by the engine's XML reader, so re-serialization need not be
byte-identical (unlike the .bin). We preserve attribute order, CRLF, indentation and
scientific-notation floats so diffs stay minimal and the engine stays happy.
"""
import xml.etree.ElementTree as ET

def _f(v): return f"{float(v):.6e}".replace('e', 'e+').replace('e+-', 'e-') \
    if 'e' not in f"{float(v):.6e}" else f"{float(v):.6e}"
def sci(v):
    # match game format: 1.200000e-003 (3-digit exponent)
    s = f"{float(v):.6e}"
    m, e = s.split('e'); ei = int(e)
    return f"{m}e{'+' if ei>=0 else '-'}{abs(ei):03d}"

ML_ATTRS = ['timelimit','victorytype','numspecialentities','goldtime','silvertime',
            'bronzetime','smallfailstring','tipscriptfunction','comicstartfunction',
            'comicendfunction','levelmusicscript']
LV_ATTRS = ['name','meterperpix','posx','posy','triggerarea','triggerspecial','gootostart']
FLOAT_ATTRS = {'meterperpix','posx','posy'}   # written in scientific notation

class Stage(dict):
    @property
    def name(self): return self.get('name')

class MultiLevel:
    def __init__(self, path=None):
        self.attrs = {}
        self.stages = []
        if path: self.load(path)

    def load(self, path):
        root = ET.parse(path).getroot()
        self.attrs = {k: root.get(k) for k in ML_ATTRS if root.get(k) is not None}
        self.stages = []
        for lv in root.findall('level'):
            self.stages.append(Stage({k: lv.get(k) for k in LV_ATTRS if lv.get(k) is not None}))
        return self

    def _fmt(self, attrs, table):
        out = []
        for k in table:
            if k not in attrs: continue
            v = attrs[k]
            if k in FLOAT_ATTRS:
                try: v = sci(v)
                except (TypeError, ValueError): pass
            out.append(f'{k}="{v}"')
        return out

    def to_xml(self):
        nl = '\r\n'
        lines = ['<multilevel']
        a = self._fmt(self.attrs, ML_ATTRS)
        lines += ['    ' + x for x in a[:-1]] + ['    ' + a[-1] + '>']
        for st in self.stages:
            la = self._fmt(st, LV_ATTRS)
            lines.append('    <level')
            lines += ['        ' + x for x in la[:-1]] + ['        ' + la[-1] + '>']
            lines.append('    </level>')
        lines.append('</multilevel>')
        return nl.join(lines) + nl

    def save(self, path):
        open(path, 'w', newline='').write(self.to_xml())


if __name__ == '__main__':
    for fn in ('lab1.xml', 'lab2.xml'):
        orig = open(fn, newline='').read()
        ml = MultiLevel(fn)
        rt = ml.to_xml()
        same = rt == orig
        print(f"{fn}: stages={[s['name'] for s in ml.stages]} "
              f"meterperpix={[s['meterperpix'] for s in ml.stages]} round-trip={same}")
        if not same:
            # show first difference for inspection
            import difflib
            for i,(a,b) in enumerate(zip(orig.splitlines(), rt.splitlines())):
                if a!=b: print(f"   first diff line {i}:\n     orig={a!r}\n     new ={b!r}"); break
