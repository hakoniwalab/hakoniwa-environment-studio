#!/usr/bin/env python3
"""Normalize approved CC0 scenery sources into catalog-ready GLBs.

Sources are read from the git-ignored .cache/scenery-sources folder. The
output frame is glTF x-right/y-up/z-back: centered in x/z, base at y=0, with
dimensions in metres. Textures are embedded in each output GLB.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache/scenery-sources"
OUTPUT = ROOT / "catalogs/scenery-cc0/assets"


@dataclass(frozen=True)
class Part:
    output: str
    source: str
    size: tuple[float, float, float]  # width, depth, height in metres
    nodes: tuple[str, ...] = ()
    texture_max_px: int | None = None


PARTS = (
    Part("pine-tall.glb", "extracted/nature-kit/Models/GLTF format/tree_pineTallA.glb", (7.5, 7.5, 12.0)),
    Part("oak.glb", "extracted/nature-kit/Models/GLTF format/tree_oak.glb", (8.0, 8.0, 11.0)),
    Part("broadleaf.glb", "extracted/nature-kit/Models/GLTF format/tree_default.glb", (6.5, 6.0, 10.0)),
    Part("bush.glb", "extracted/nature-kit/Models/GLTF format/plant_bush.glb", (1.2, 1.2, 0.75)),
    Part("house-small.glb", "extracted/city-kit-suburban/Models/GLB format/building-type-h.glb", (6.0, 5.0, 4.5)),
    Part("house-medium.glb", "extracted/city-kit-suburban/Models/GLB format/building-type-a.glb", (8.0, 6.5, 6.0)),
    Part("house-large.glb", "extracted/city-kit-suburban/Models/GLB format/building-type-b.glb", (10.0, 8.0, 8.0)),
    Part("shop.glb", "extracted/city-kit-commercial/Models/GLB format/building-a.glb", (7.0, 7.0, 9.0)),
    Part("commercial-block.glb", "extracted/city-kit-commercial/Models/GLB format/building-j.glb", (20.0, 13.0, 15.0)),
    Part("street-light.glb", "extracted/city-kit-roads/Models/GLB format/light-curved.glb", (0.5, 2.25, 6.75)),
    Part("heritage-lamp.glb", "polyhaven/street_lamp_02/street_lamp_02_1k.gltf", (0.7, 1.5, 4.0),
         texture_max_px=256),
    Part("concrete-barrier.glb", "polyhaven/concrete_road_barrier_02/concrete_road_barrier_02_1k.gltf", (2.35, 0.35, 0.9),
         texture_max_px=256),
    Part("chainlink-fence.glb", "polyhaven/modular_chainlink_fence/modular_chainlink_fence_1k.gltf", (3.0, 0.08, 2.0),
         ("modular_chainlink_fence_double_",), texture_max_px=256),
    Part("timber-wall.glb", "extracted/nature-kit/Models/GLTF format/fence_planksDouble.glb", (3.0, 0.25, 2.0)),
)


TEXTURE_FIELDS = (
    "baseColorTexture",
    "metallicRoughnessTexture",
    "normalTexture",
    "emissiveTexture",
    "occlusionTexture",
    "image",
)


def downscale_textures(scene: trimesh.Scene, maximum: int) -> None:
    """Limit embedded material textures while retaining colour/alpha channels."""
    resized: dict[int, Image.Image] = {}
    for geometry in scene.geometry.values():
        material = getattr(geometry.visual, "material", None)
        if material is None:
            continue
        for field in TEXTURE_FIELDS:
            texture = getattr(material, field, None)
            if not isinstance(texture, Image.Image) or max(texture.size) <= maximum:
                continue
            key = id(texture)
            if key not in resized:
                resized[key] = texture.copy()
                resized[key].thumbnail((maximum, maximum), Image.Resampling.LANCZOS)
            setattr(material, field, resized[key])


def normalized(part: Part) -> trimesh.Scene:
    source = CACHE / part.source
    if not source.is_file():
        raise SystemExit(f"missing cached source: {source}")
    original = trimesh.load_scene(source)
    nodes = (tuple(node for node in original.graph.nodes_geometry if any(node.startswith(prefix) for prefix in part.nodes))
             if part.nodes else tuple(original.graph.nodes_geometry))
    scene = trimesh.Scene()
    for number, node in enumerate(nodes):
        transform, geometry_name = original.graph.get(node)
        geometry = original.geometry[geometry_name].copy()
        geometry.apply_transform(transform)
        scene.add_geometry(geometry, node_name=f"part-{number}", geom_name=f"part-{number}")
    low, high = scene.bounds
    extent = high - low
    width, depth, height = part.size
    scale = np.diag([width / extent[0], height / extent[1], depth / extent[2], 1.0])
    centre = (low + high) / 2
    move = np.eye(4)
    move[:3, 3] = (-centre[0], -low[1], -centre[2])
    # Bake normalization into vertices. Environment Studio intentionally
    # ignores asset node transforms when it copies primitives into an output.
    for geometry in scene.geometry.values():
        geometry.apply_transform(scale @ move)
    if part.texture_max_px is not None:
        downscale_textures(scene, part.texture_max_px)
    return scene


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for part in PARTS:
        scene = normalized(part)
        destination = OUTPUT / part.output
        destination.write_bytes(scene.export(file_type="glb"))
        polygons = sum(len(mesh.faces) for mesh in scene.geometry.values())
        print(f"{destination.relative_to(ROOT)}: {polygons} triangles, {destination.stat().st_size} bytes")


if __name__ == "__main__":
    main()
