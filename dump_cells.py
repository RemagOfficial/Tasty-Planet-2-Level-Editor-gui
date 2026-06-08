"""
dump_cells.py - export named atlas cells as PNGs so you can eyeball which is which.

Usage (run from the editor folder, with the game XMLs + assets/graphics present):
    python dump_cells.py                      # dumps the Egypt house candidates
    python dump_cells.py poorhouse richouse4  # dump specific cells by name
    python dump_cells.py --atlas egyptimagemap7   # dump every cell in an atlas
    python dump_cells.py --like house         # dump every cell whose name contains 'house'

Output goes to ./cell_dump/<cellname>.png. Compare these to your in-game photos,
then tell me the entity->cell mapping and I'll add the aliases.
"""
import os, sys
from tp2assets import Assets

# Adjust if your graphics live elsewhere:
GFX = os.environ.get("TP2_GFX", "assets/graphics")
IMAGEMAPS = "imagemaps.xml"; ENTS = "entityintersections.xml"; ANIMS = "animationdefs.xml"

# The Egypt house cells we're trying to match (poor: 3 cells, rich: 5 cells):
DEFAULT = ["poorhouse", "poorhouse8", "poorhousetarp",
           "richouse", "richouse4", "richhousegarden1", "richhousegarden2", "richousetarp"]

def main():
    A = Assets(GFX, imagemaps=IMAGEMAPS, entities=ENTS, animations=ANIMS)
    args = sys.argv[1:]
    if args and args[0] == "--atlas":
        names = A.cells_in(args[1])
    elif args and args[0] == "--like":
        names = A.cells_in(args[1])
    elif args:
        names = args
    else:
        names = DEFAULT
    os.makedirs("cell_dump", exist_ok=True)
    for name in names:
        img = A.cell_image(name)
        if img is None:
            print(f"  MISSING (no such cell or atlas not found): {name}")
            continue
        path = os.path.join("cell_dump", f"{name}.png")
        img.save(path)
        print(f"  saved {path}  ({img.width}x{img.height})")
    print(f"\nDone. Open the cell_dump/ folder and compare to your in-game photos.")

if __name__ == "__main__":
    main()
