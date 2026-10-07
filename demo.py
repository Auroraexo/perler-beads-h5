#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MVP 演示:生成合成测试图 -> 跑通整条流水线 -> 输出 PNG / BOM / PDF。"""
import os
from PIL import Image, ImageDraw
import bead_pipeline as bp

HERE = os.path.dirname(os.path.abspath(__file__))
PALETTE = os.path.join(HERE, 'palettes', 'fuse_beads_starter.csv')
OUT = os.path.join(HERE, 'output')


def make_sample(path, n=256):
    """合成一张含多色块+渐变的测试图(无需外部图片即可验证流程)。"""
    img = Image.new('RGB', (n, n), (255, 255, 255))
    px = img.load()
    for y in range(n):
        for x in range(n):
            px[x, y] = (int(255 * x / n), int(255 * y / n), 120)
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1, col in [
        (20, 20, 90, 90, (211, 17, 26)),
        (120, 30, 200, 90, (47, 163, 79)),
        (40, 130, 100, 200, (29, 94, 183)),
        (160, 150, 230, 210, (249, 99, 0)),
        (190, 200, 246, 250, (107, 63, 160)),
    ]:
        d.rectangle([x0, y0, x1, y1], fill=col)
    d.ellipse([10, 10, 110, 110], fill=(255, 203, 41))
    img.save(path)


def main():
    os.makedirs(OUT, exist_ok=True)
    sample = os.path.join(OUT, 'sample.png')
    make_sample(sample)
    palette = bp.load_palette(PALETTE)
    for size in (16, 32):
        grid = bp.run_pipeline(sample, palette, size=size, k=24, min_count=5)
        png = os.path.join(OUT, f'bead_grid_{size}.png')
        csvp = os.path.join(OUT, f'bom_{size}.csv')
        pdf = os.path.join(OUT, f'blueprint_{size}.pdf')
        bp.render_grid_png(grid, palette, size, size, png)
        bp.export_csv(bp.build_bom(grid, palette), csvp)
        bp.export_pdf(grid, palette, size, size, pdf)
        bom = bp.build_bom(grid, palette)
        print(f'== {size}x{size} | 用色 {len(bom)} 种 | 总颗数 {sum(c for _,c in bom)} ==')
        for p, c in bom[:6]:
            print(f'   {p["id"]} {p["name"]:<12} {c}')


if __name__ == '__main__':
    main()