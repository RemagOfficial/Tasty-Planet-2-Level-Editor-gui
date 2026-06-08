"""Tasty Planet 2 .bin level parser / serializer / editor.
Format-general: validated to round-trip lab1a.bin and lab1b.bin byte-for-byte.

File layout (little-endian):
  HEADER:  u32 flag(0) | u32 name_count | name_count*(u32 len + name) |
           4*u32 (const 5,0,0,0 here) | u32 tile_count
  TILES:   tile_count * 30-byte records
  MIDDLE:  collision polygons + enemy paths (opaque; preserved verbatim)
  PROPS:   repeated [u32 a | u32 b | u32 namelen | name | 53-byte payload]
  TRAILER: leftover bytes (padding; preserved verbatim)
"""
import struct

GRANITE_REC = 30
PROP_PAYLOAD = 53

class Level:
    def __init__(self, data: bytes):
        self.raw = data
        self._parse()

    # ---------- helpers ----------
    def _u32(self, o): return struct.unpack_from('<I', self.raw, o)[0]

    def _scan_props(self, o):
        """Parse a chain of prop records from offset o; return (count, end_offset)."""
        d = self.raw; n = 0
        while o + 12 <= len(d):
            a, b, nl = struct.unpack_from('<III', d, o)
            if not (1 <= nl <= 24) or o + 12 + nl + PROP_PAYLOAD > len(d):
                break
            name = d[o+12:o+12+nl]
            if name[-1:] != b'\x00' or not all(32 <= c < 127 for c in name[:-1]):
                break
            o += 12 + nl + PROP_PAYLOAD; n += 1
        return n, o

    # ---------- parse ----------
    def _parse(self):
        d = self.raw
        self.flag0 = self._u32(0)
        name_count = self._u32(4)
        o = 8
        self.tile_names = []
        for _ in range(name_count):
            nl = self._u32(o); self.tile_names.append(d[o+4:o+4+nl]); o += 4 + nl
        palette_end = o

        # post-palette: K filler u32s then the tile count. Find K by validating that
        # the resulting 30-byte records have their two pad bytes (+4, +17) zeroed.
        def valid(rec_start, count):
            if count < 0 or rec_start + count*GRANITE_REC > len(d): return False
            for i in range(min(count, 8)):
                b = rec_start + i*GRANITE_REC
                if d[b+4] != 0 or d[b+17] != 0: return False
            return True
        rec_start = tile_count = None
        zero_fallback = None
        for k in range(0, 8):
            cnt = self._u32(palette_end + 4*k)
            rs = palette_end + 4*(k+1)
            if not (0 <= cnt <= 100000) or not valid(rs, cnt):
                continue
            if cnt > 0:                       # a real, non-empty tile layer
                rec_start, tile_count = rs, cnt; break
            if zero_fallback is None:         # remember an empty-layer candidate
                zero_fallback = (rs, cnt)
        if rec_start is None and zero_fallback is not None:
            rec_start, tile_count = zero_fallback
        if rec_start is None:
            raise ValueError("could not locate tile count / records")

        self.header = d[0:rec_start]          # verbatim; tile_count is its last u32
        self._count_off = rec_start - 4
        self.tile_count = tile_count
        self.tiles = [d[rec_start + i*GRANITE_REC: rec_start + (i+1)*GRANITE_REC]
                      for i in range(tile_count)]
        rec_end = rec_start + tile_count*GRANITE_REC

        # locate prop region: first offset >= rec_end whose record chain is long and
        # reaches within a small trailer of EOF.
        prop_start, best_end = None, rec_end
        o = rec_end
        while o < len(d):
            n, end = self._scan_props(o)
            if n >= 4 and end >= len(d) - 64:
                prop_start, best_end = o, end; break
            o += 1
        if prop_start is None:                # level may have no props
            prop_start = best_end = len(d)

        self.middle = d[rec_end:prop_start]
        self.props = []
        o = prop_start
        while o < best_end:
            a, b, nl = struct.unpack_from('<III', d, o)
            name = d[o+12:o+12+nl]
            payload = d[o+12+nl:o+12+nl+PROP_PAYLOAD]
            self.props.append({'a': a, 'b': b, 'name': name[:-1].decode(),
                               'payload': bytearray(payload)})
            o += 12 + nl + PROP_PAYLOAD
        self.trailer = d[best_end:]

    # ---------- serialize ----------
    def serialize(self) -> bytes:
        out = bytearray(self.header)
        struct.pack_into('<I', out, self._count_off, len(self.tiles))
        for t in self.tiles: out += t
        out += self.middle
        for p in self.props:
            name = p['name'].encode() + b'\x00'
            out += struct.pack('<III', p['a'], p['b'], len(name)) + name
            assert len(p['payload']) == PROP_PAYLOAD
            out += p['payload']
        out += self.trailer
        return bytes(out)

    # ---------- prop field accessors ----------
    @staticmethod
    def prop_get(p):
        pl = p['payload']
        return {'x': struct.unpack_from('<i', pl, 0)[0],
                'y': struct.unpack_from('<i', pl, 4)[0],
                'rot': struct.unpack_from('<i', pl, 28)[0],
                'rgba': bytes(pl[33:37])}
    @staticmethod
    def prop_set(p, x=None, y=None, rot=None, rgba=None):
        pl = p['payload']
        if x   is not None: struct.pack_into('<i', pl, 0, x)
        if y   is not None: struct.pack_into('<i', pl, 4, y)
        if rot is not None: struct.pack_into('<i', pl, 28, rot)
        if rgba is not None: pl[33:37] = bytes(rgba)


# ---------- editor helpers ----------
def prop_size(p):        return struct.unpack_from('<d', p['payload'], 37)[0]
def set_prop_size(p, v): struct.pack_into('<d', p['payload'], 37, float(v))
def remove_background_tiles(lvl): lvl.tiles = []
def find_props(lvl, name): return [p for p in lvl.props if p['name'] == name]
def clone_prop(lvl, p, dx=0, dy=0):
    q = {'a': p['a'], 'b': p['b'], 'name': p['name'], 'payload': bytearray(p['payload'])}
    f = Level.prop_get(q); Level.prop_set(q, x=f['x']+dx, y=f['y']+dy)
    lvl.props.append(q); return q


if __name__ == '__main__':
    for fn in ('lab1a.bin', 'lab1b.bin'):
        d = open(fn, 'rb').read()
        L = Level(d); rt = L.serialize()
        print(f"{fn}: names={[n.rstrip(chr(0).encode()).decode() for n in L.tile_names]} "
              f"tiles={L.tile_count} middle={len(L.middle)} props={len(L.props)} "
              f"trailer={len(L.trailer)} | round-trip={rt==d}")
