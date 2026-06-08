"""
TP2 asset resolver + sprite builder (headless, PIL only — no Qt).

Resolves a level-prop name to a tinted PIL RGBA sprite using the game's files:
    assets/graphics/<theme>imagemap<N>.jpg (+ ...mask.png)   texture atlases
    imagemaps.xml            name -> atlas cell (trimmed rect)
    entityintersections.xml  per-entity natdiam (sizing)
    animationdefs.xml        name -> animation (frames: cell + per-frame transform)

Name resolution order:
    1. animation def by convention: NAME, NAME_1, or first NAME_* def  -> use its frames
    2. direct atlas cell named NAME
    3. candy_*  -> 'candy' base + 'candy_overlay', recolored by prop RGBA
    4. NAME1    -> first numbered cell
Unresolved names return None (the editor draws a placeholder marker).
"""
import os, re, math, difflib, xml.etree.ElementTree as ET
from functools import lru_cache
from PIL import Image, ImageChops

class Cell:
    __slots__=('name','atlas','x1','y1','x2','y2','cox','coy','ow','oh')
    def __init__(self,atlas,e):
        gi=lambda k:int(e.get(k))
        self.name=e.get('name'); self.atlas=atlas
        self.x1,self.y1,self.x2,self.y2=gi('x1'),gi('y1'),gi('x2'),gi('y2')
        self.cox,self.coy=gi('cropoffsetx'),gi('cropoffsety')
        self.ow,self.oh=gi('origwidth'),gi('origheight')
    @property
    def w(self): return self.x2-self.x1
    @property
    def h(self): return self.y2-self.y1

class Frame:
    __slots__=('cell','offx','offy','angle','scale','opacity','flipx','flipy','time')
    def __init__(self,e):
        gf=lambda k,d=0.0: float(e.get(k,d))
        self.cell=e.get('cellname')
        self.offx,self.offy=gf('offsetx'),gf('offsety')
        self.angle,self.scale=gf('angle'),gf('scale',1.0)
        self.opacity=gf('opacity',1.0)
        self.flipx=e.get('flipx')=='true'; self.flipy=e.get('flipy')=='true'
        self.time=gf('time',1.0)

class Anim:
    __slots__=('name','frames','cx','cy','looping','total')
    def __init__(self,e):
        self.name=e.get('name')
        self.cx=float(e.get('centerx',0.5)); self.cy=float(e.get('centery',0.5))
        self.looping=e.get('looping')=='true'
        self.frames=[Frame(f) for f in e.findall('frame')]
        self.total=sum(max(f.time,1e-6) for f in self.frames) or 1.0
    def frame_at(self, t):
        if not self.frames: return None
        t=t % self.total if self.looping else min(t,self.total-1e-6)
        acc=0.0
        for f in self.frames:
            acc+=max(f.time,1e-6)
            if t<acc: return f
        return self.frames[-1]

class Assets:
    def __init__(self, graphics_dir, imagemaps='imagemaps.xml',
                 entities='entityintersections.xml', animations='animationdefs.xml',
                 strings='strings.xml'):
        self.dir=graphics_dir
        self.cells={}; self.natdiam={}; self.anims={}; self.entity_display={}; self.entity_theme={}
        self._table_names=set()
        # authoritative entity table (names -> display, theme), if present
        try:
            from tp2_entities import DISPLAY as _ED, THEME as _ET
            self.entity_display.update(_ED); self.entity_theme.update(_ET)
            self._table_names=set(_ED)
        except Exception:
            pass
        def near(p): return p if os.path.isabs(p) else os.path.join(graphics_dir,p)
        im,en,an=near(imagemaps),near(entities),near(animations)
        self.ok=os.path.isdir(graphics_dir) and os.path.isfile(im)
        if os.path.isfile(im):
            for imap in ET.parse(im).getroot().findall('imagemap'):
                a=imap.get('name')
                for c in imap.findall('cell'): self.cells[c.get('name')]=Cell(a,c)
        if os.path.isfile(en):
            txt=open(en,encoding='utf-8',errors='ignore').read()
            for m in re.finditer(r'name="([^"]+)"[^>]*?natdiam="([0-9.eE+\-]+)"',txt,re.S):
                self.natdiam[m.group(1)]=float(m.group(2))
        if os.path.isfile(an):
            for a in ET.parse(an).getroot().findall('animationdef'):
                self.anims[a.get('name')]=Anim(a)
        st=near(strings)
        if os.path.isfile(st):
            self._load_strings(st)
        # precompute prefix list for animation lookup
        self._anim_names=list(self.anims.keys())
        # explicit entity->art aliases (entity name in level != art name, unfortunately)
        self.aliases={
            'scientist_young_lab1':'modern_sciyoung',
            'scientist_old_lab1':'modern_sciold',
            'leaf':'cucumberleaf',
            'stoneblock1':'stone1',
            'galacticturtle':'turtles_all_the_way_down',
            'fabricspace':'turtle_tile',
            # giants are the star sprite, recolored via the entity's color (like grapes)
            'redgiant':'star', 'supergiant':'star', 'hypergiant':'star', 'hypergiant_moving':'star',
            # planets: each a full planet cell, recolored.
            'neptune_future1':'planet_gas', 'jupiter_future1':'planet_gas_2',
            'saturn_future1':'planet_rings', 'mars_future1':'planet_rockey',
            'venus_future1':'planet_rockey', 'mercury_future1':'planet_rockey',
            'moon_future1':'planet_rockey',
            'green_grape':'grape_olive_1', 'purple_grape':'grape_olive_1',
            'green_olive':'grape_olive_1', 'purple_olive':'grape_olive_1',
            # Egypt house CLUSTERS (spelled-out) are single grid sprites; individuals are
            # composited via house_recipe below (base + garden + tinted tarp on a shared canvas).
            'egypt_poor_house_eight':'poorhouse8', 'egypt_rich_house_four':'richouse4',
        }
        # composite recipe: shared-canvas parts (garden under base under tinted tarp).
        # tints are the exe's per-house tarp colors (gaps reconstructed from the pattern).
        self.house_recipe={
            'egypt_poor_house_1':dict(base='poorhouse', garden=None, tarp='poorhousetarp', tint='#b5c5c5'),
            'egypt_poor_house_2':dict(base='poorhouse', garden=None, tarp='poorhousetarp', tint='#b9cba2'),
            'egypt_poor_house_3':dict(base='poorhouse', garden=None, tarp='poorhousetarp', tint='#cbc2b2'),
            'egypt_rich_house_1':dict(base='richouse', garden='richhousegarden1', tarp='richousetarp', tint='#a8c681'),
            'egypt_rich_house_2':dict(base='richouse', garden='richhousegarden1', tarp='richousetarp', tint='#d9cb7b'),
            'egypt_rich_house_3':dict(base='richouse', garden='richhousegarden1', tarp='richousetarp', tint='#d07765'),
            'egypt_rich_house_4':dict(base='richouse', garden='richhousegarden2', tarp='richousetarp', tint='#a8c681'),
            'egypt_rich_house_5':dict(base='richouse', garden='richhousegarden2', tarp='richousetarp', tint='#d9cb7b'),
            'egypt_rich_house_6':dict(base='richouse', garden='richhousegarden2', tarp='richousetarp', tint='#d07765'),
        }
        # base cell -> gloss/shine overlay composited on top after tinting (like candy)
        self._glow={'grape_olive_1':'grape_olive_2'}
        # multi-cell composites overlaid on a shared canvas, THEN tinted by the entity color
        # (star = two halves; redgiant/supergiant/hypergiant alias to 'star' and recolor)
        self.composites={'star':['star_bottom','star_top']}
        # normalized index (lowercase, alphanumeric only) for fuzzy name->cell resolution:
        # catches creditchip->credit_chip, solarpanel->solar_panel, futurebuilding2->future_building_2
        self._norm=lambda s: re.sub(r'[^a-z0-9]','',s.lower())
        self._cell_norm={}
        for cn in self.cells:
            self._cell_norm.setdefault(self._norm(cn), cn)
        self._cell_keys=list(self._cell_norm)
        self._strip_prefixes=('tree_','plant_','prop_','deco_','obj_')
        self._theme_prefixes=('egypt_','future_','japan_','roman_','dino_','modern_',
                              'anti_','croc_','rat_','lab_')
        self._numwords={'one':'1','two':'2','three':'3','four':'4','five':'5','six':'6',
                        'seven':'7','eight':'8','nine':'9','ten':'10','eleven':'11','twelve':'12'}
        # stem synonyms tried only as a fallback (a real 'boulder1' cell, if present, wins first
        # because _base_cell looks the literal name up before calling _fuzzy_cell)
        self._synonyms={'boulder':'rock'}

    def _numword(self, name):
        return ''.join(self._numwords.get(p, p) for p in re.split(r'(_)', name))

    def _load_strings(self, path):
        """Parse the SpreadsheetML strings file; '<entity>_disp' rows give the valid entity
        names the game can spawn, mapped to their display labels (e.g. candy_green -> Green Candy)."""
        NS='{urn:schemas-microsoft-com:office:spreadsheet}'
        try: root=ET.parse(path).getroot()
        except Exception: return
        ws=root.find(f'{NS}Worksheet')
        table=ws.find(f'{NS}Table') if ws is not None else None
        if table is None: return
        for r in table.findall(f'{NS}Row'):
            vals=[]; ci=0
            for c in r.findall(f'{NS}Cell'):
                idx=c.get(f'{NS}Index')
                if idx: ci=int(idx)-1
                d=c.find(f'{NS}Data')
                while len(vals)<ci: vals.append('')
                vals.append(d.text if (d is not None and d.text) else ''); ci+=1
            if vals and vals[0].endswith('_disp'):
                k = vals[0][:-5]
                if k not in self.entity_display:           # table wins; strings only fills gaps
                    self.entity_display[k] = vals[1] if len(vals)>1 else k

    def entity_names(self):
        """Valid spawnable entity-type names. Prefers the authoritative table; falls back to
        strings-derived names only if the table isn't available."""
        if self._table_names:
            return sorted(self._table_names)
        return sorted(self.entity_display.keys())

    def display_name(self, entity):
        return self.entity_display.get(entity, entity)

    def _fuzzy_cell(self, name):
        """Last-resort name->cell: try the name and theme/prefix-stripped variants, each
        also with number-words mapped to digits, matched underscore-insensitively.
        e.g. egypt_poor_house_eight -> poor_house_8 -> norm 'poorhouse8' -> cell poorhouse8."""
        bases=[name]
        for p in self._strip_prefixes + self._theme_prefixes:
            if name.startswith(p) and len(name) > len(p):
                bases.append(name[len(p):])
        # strip trailing instance/theme suffixes: asteroid1_future1->asteroid1, hypergiant_moving->hypergiant
        suf=re.sub(r'(_(?:future|egypt|dino|roman|japan|modern|anti|space)\d*|_moving|_disp)$', '', name)
        if suf != name: bases.append(suf)
        for b in list(bases):
            for k, v in self._synonyms.items():
                if k in b: bases.append(b.replace(k, v))
        for b in bases:
            for cand in (b, self._numword(b)):
                if cand in self.cells: return self.cells[cand]
                hit=self._cell_norm.get(self._norm(cand))
                if hit: return self.cells[hit]
        # final resort: close-match for dev typos (fusioncell->fushion_cell, richhouse->richouse),
        # but require the matched cell to share the query's trailing number so we never
        # snap e.g. *_garden_one onto *_garden2.
        def dsuf(s):
            m=re.search(r'(\d+)$', s); return m.group(1) if m else ''
        for b in bases:
            q=self._norm(self._numword(b))
            for m in difflib.get_close_matches(q, self._cell_keys, n=4, cutoff=0.86):
                if dsuf(m)==dsuf(q):
                    return self.cells[self._cell_norm[m]]
        return None

    # ---- atlas compositing ----
    @lru_cache(maxsize=None)
    def _atlas(self, atlasname):
        base=os.path.join(self.dir, atlasname)
        try:
            rgb=Image.open(base+'.jpg').convert('RGB')
            rgb.putalpha(Image.open(base+'mask.png').convert('L'))
            return rgb
        except (FileNotFoundError, OSError):
            return None      # atlas image not present -> caller falls back
    def _cell_rgba(self, cell):
        atlas=self._atlas(cell.atlas)
        if atlas is None: return None
        packed=atlas.crop((cell.x1,cell.y1,cell.x2,cell.y2))
        canvas=Image.new('RGBA',(cell.ow,cell.oh),(0,0,0,0))
        canvas.paste(packed,(cell.cox,cell.coy)); return canvas

    def cell_image(self, name):
        """Untinted PIL RGBA image of a named atlas cell (no entity resolution), or None."""
        c=self.cells.get(name)
        return self._cell_rgba(c) if c else None

    def cells_in(self, *atlas_or_substr):
        """Cell names filtered by atlas name or substring match (helper for browsing)."""
        out=[]
        for n,c in self.cells.items():
            if any(s in n or s==c.atlas or s in c.atlas for s in atlas_or_substr): out.append(n)
        return sorted(out)

    def atlas_of(self, name):
        """Best-effort atlas filename a sprite/entity name resolves into, or None.
        Used to tell which atlases a level actually loads (so we don't offer cells the
        game can't render in that level)."""
        c=self.cells.get(name)
        if c is None:
            try: c=self._base_cell(name)
            except Exception: c=None
        return c.atlas if c else None

    # ---- animation lookup ----
    def resolve_anim(self, name):
        a=self.anims.get(name) or self.anims.get(name+'_1')
        if a: return a
        pref=name+'_'
        for k in self._anim_names:
            if k.startswith(pref): return self.anims[k]
        return None

    def frame_count(self, name):
        a=self.resolve_anim(name); return len(a.frames) if a else 1
    def pivot(self, name):
        a=self.resolve_anim(name); return (a.cx,a.cy) if a else (0.5,0.5)
    def frame_transform(self, name, i):
        """(offx,offy,angle_deg,scale,opacity) for animation frame i, else identity."""
        a=self.resolve_anim(name)
        if not a or not a.frames: return (0,0,0.0,1.0,1.0)
        f=a.frames[i % len(a.frames)]
        return (f.offx,f.offy,f.angle,f.scale,f.opacity)

    # ---- sprite building ----
    def _base_cell(self, name, frame_i=0):
        name=self.aliases.get(name, name)
        # skeletal characters (modern_sciyoung, ...) have no single sprite; use the
        # standing body part as a representative (full assembly is a future feature I plan to do).
        if name+'_stand_body' in self.anims or name+'_walk_body' in self.anims:
            body=self.anims.get(name+'_stand_body') or self.anims.get(name+'_walk_body')
            if body and body.frames:
                c=self.cells.get(body.frames[0].cell)
                if c: return c
        a=self.resolve_anim(name)
        if a and a.frames:
            return self.cells.get(a.frames[frame_i % len(a.frames)].cell)
        c=self.cells.get(name)
        if c: return c
        if name.startswith('candy_') and 'candy' in self.cells: return self.cells['candy']
        if name+'1' in self.cells: return self.cells.get(name+'1')
        return self._fuzzy_cell(name)

    def compose_house(self, name):
        """Assemble an egypt house from shared-canvas parts: garden (back), base,
        then the tarp tinted by its per-house color. All parts share ow/oh so a plain
        alpha-composite aligns them via their built-in crop offsets."""
        r=self.house_recipe[name]
        canvas=None
        for part, tint in ((r['garden'], None), (r['base'], None), (r['tarp'], r['tint'])):
            if not part: continue
            layer=self.cell_image(part)
            if layer is None: continue
            if tint:
                tc=tuple(int(tint[i:i+2],16) for i in (1,3,5))
                keepA=layer.getchannel('A')
                layer=ImageChops.multiply(layer, Image.new('RGBA', layer.size, tc+(255,)))
                layer.putalpha(keepA)
            if canvas is None:
                canvas=Image.new('RGBA', layer.size, (0,0,0,0))
            if layer.size==canvas.size:
                canvas=Image.alpha_composite(canvas, layer)
        return canvas

    def compose_parts(self, parts):
        """Overlay a list of cells (bottom-first) on their shared canvas, untinted."""
        canvas=None
        for p in parts:
            layer=self.cell_image(p)
            if layer is None: continue
            if canvas is None: canvas=Image.new('RGBA', layer.size, (0,0,0,0))
            if layer.size==canvas.size: canvas=Image.alpha_composite(canvas, layer)
        return canvas

    def build_sprite(self, name, rgba=(255,255,255,255), frame_i=0):
        """Untrimmed PIL RGBA sprite for the given frame, tinted. None if unresolved
        or an explicit empty frame (e.g. '0null')."""
        canon=self.aliases.get(name, name)
        if canon in self.house_recipe:                # composite egypt house (baked tints)
            comp=self.compose_house(canon)
            if comp is not None: return comp
        if canon in self.composites:                  # multi-cell composite, recolored by rgba
            comp=self.compose_parts(self.composites[canon])
            if comp is not None:
                r,g,b,a=rgba
                if (r,g,b)!=(255,255,255):
                    keepA=comp.getchannel('A')
                    comp=ImageChops.multiply(comp, Image.new('RGBA', comp.size, (r,g,b,255)))
                    comp.putalpha(keepA)
                return comp
        base=self._base_cell(name, frame_i)
        if base is None: return None
        spr=self._cell_rgba(base)
        if spr is None: return None
        r,g,b,a=rgba
        if (r,g,b)!=(255,255,255):
            keepA=spr.getchannel('A')
            spr=ImageChops.multiply(spr,Image.new('RGBA',spr.size,(r,g,b,255)))
            spr.putalpha(keepA)
        # candy gloss overlay (only when using the shared candy base)
        if name.startswith('candy_') and self.resolve_anim(name) is None and 'candy_overlay' in self.cells:
            ov=self._cell_rgba(self.cells['candy_overlay'])
            if ov.size==spr.size: spr=Image.alpha_composite(spr,ov)
        # generic gloss/shine overlay (e.g. grape_olive_1 + grape_olive_2), un-tinted, on top
        ovname=self._glow.get(base.name)
        if ovname and ovname in self.cells:
            ov=self._cell_rgba(self.cells[ovname])
            if ov is not None and ov.size==spr.size: spr=Image.alpha_composite(spr,ov)
        # fold per-frame opacity + tint alpha
        fa=self.frame_transform(name,frame_i)[4]
        eff=int(round(a*fa))
        if eff!=255:
            spr.putalpha(spr.getchannel('A').point(lambda v:v*eff//255))
        return spr

    def world_per_pixel(self, name, area):
        nd=self.natdiam.get(name, 300.0)
        R=math.sqrt(max(area,1.0)/math.pi)
        return (2*R)/nd
