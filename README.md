# Community Bot

A bot for the Darkdustry Discord server. The repository also includes a small Python helper for reading Mindustry maps and schematics and creating preview images.

## Mindustry Python port

Install the image dependency:

```bash
python -m pip install -r requirements.txt
```

The `mindustry_port.py` module offers:

- `read_map(path_or_bytes)`: read map metadata and tile IDs/names from a Mindustry `.msav` file.
- `read_schematic(path_or_bytes)`: read a `.msch` file, including supported config object types.
- `read_schematic_base64(text)`: decode and read a Mindustry schematic share string.
- `map_to_image(map_data)` / `schematic_to_image(schematic)`: return Pillow images.
- `image_to_buffer(image, "PNG")`: encode to an in-memory buffer for Discord uploads or HTTP responses.
- `save_image(image, path, "JPEG")`: save as PNG, JPEG, or another Pillow-supported format.

Example `main.py`:

```python
import discord
from mindustry_port import (
    image_to_buffer,
    map_file_to_image,
    read_schematic_base64,
    schematic_to_image,
)

# In a Discord command handler:
async def send_schematic(ctx, share_string: str):
    schematic = read_schematic_base64(share_string)
    png = image_to_buffer(schematic_to_image(schematic), "PNG")
    await ctx.send(file=discord.File(png, filename="schematic.png"))

async def send_map(ctx, map_path: str):
    png = image_to_buffer(map_file_to_image(map_path), "PNG")
    await ctx.send(file=discord.File(png, filename="map.png"))
```

For local conversion:

```python
from mindustry_port import read_map, map_to_image, save_image

map_data = read_map("world.msav")
print(map_data.name, map_data.width, map_data.height)
save_image(map_to_image(map_data, scale=3), "world-preview.jpg", "JPEG")
```

The readers decode the binary data directly and do not require a running Mindustry instance. Map preview colors are approximations based on a built-in palette; pass a custom `{content_name: (r, g, b)}` mapping to `map_to_image` for your preferred colors. Schematic previews show colored block cells and rotation marks rather than rendering the game's actual sprites/config overlays. The module reads but does not write or modify Mindustry map/schematic files.
