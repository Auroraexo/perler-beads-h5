#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
流水线拼豆 MVP —— 核心算法流水线
    图片 -> 双边滤波降噪 -> KMeans 预降色 -> CIELAB 匹配实体色卡 -> 稀有色合并 -> BOM -> PNG/PDF 图纸

依赖:仅 Pillow(无 numpy/cv2),便于零成本验证。
算法策略对齐全案:传统 CV(双边滤波、K-Means、CIELAB 色彩匹配),不引入重型 AI 模型。
"""
import argparse, csv, math, os
from collections import Counter
from PIL import Image, ImageDraw, ImageFont

try:
    RESAMPLE = Image.Resampling.LANCZOS          # Pillow >= 9.1
except AttributeError:                           # Pillow < 9.1
    RESAMPLE = Image.LANCZOS


# ===================== 色彩空间 =====================
def hex_to_rgb(h):
    h = h.strip().lstrip('#')
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _inv_gamma(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def rgb_to_lab(rgb):
    r, g, b = (_inv_gamma(x / 255.0) for x in rgb)
    x = (r * 0.4124 + g * 0.3576 + b * 0.1805) * 100
    y = (r * 0.2126 + g * 0.7152 + b * 0.0722) * 100
    z = (r * 0.0193 + g * 0.1192 + b * 0.9505) * 100

    def f(t):
        return t ** (1.0 / 3.0) if t > 0.008856 else (7.787 * t + 16.0 / 116.0)

    fx, fy, fz = f(x / 95.047), f(y / 100.0), f(z / 108.883)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e76(a, b):
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def delta_e2000(lab1, lab2):
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1 = (a1 * a1 + b1 * b1) ** 0.5
    C2 = (a2 * a2 + b2 * b2) ** 0.5
    avg_C = (C1 + C2) / 2
    G = 0.5 * ((avg_C ** 7) / (avg_C ** 7 + 25 ** 7)) ** 0.5
    a1p, a2p = a1 * (1 + G), a2 * (1 + G)
    C1p = (a1p * a1p + b1 * b1) ** 0.5
    C2p = (a2p * a2p + b2 * b2) ** 0.5
    avg_Cp = (C1p + C2p) / 2

    def h_deg(ap, bp):
        h = math.degrees(math.atan2(bp, ap))
        return h + 360 if h < 0 else h

    h1p, h2p = h_deg(a1p, b1), h_deg(a2p, b2)
    dhp = h2p - h1p
    if avg_Cp == 0:
        dhp = 0
    elif abs(dhp) > 180:
        dhp = dhp - 360 if dhp > 0 else dhp + 360
    dLp, dCp = L2 - L1, C2p - C1p
    bar_H = (h1p + h2p) / 2
    if abs(h2p - h1p) > 180:
        bar_H = (h1p + h2p + 360) / 2
    T = (1 - 0.17 * math.cos((bar_H - 30) * math.pi / 180)
         + 0.24 * math.cos(2 * bar_H * math.pi / 180)
         + 0.32 * math.cos((3 * bar_H + 6) * math.pi / 180)
         - 0.20 * math.cos((4 * bar_H - 63) * math.pi / 180))
    dTheta = 30 * math.exp(-(((bar_H - 275) / 25) ** 2))
    RC = 2 * math.sqrt((avg_Cp ** 7) / (avg_Cp ** 7 + 25 ** 7))
    SL = 1 + (0.015 * ((bar_H - 50) ** 2)) / math.sqrt(20 + (bar_H - 50) ** 2)
    SC = 1 + 0.045 * avg_Cp
    SH = 1 + 0.015 * avg_Cp * T
    RT = -math.sin(2 * dTheta * math.pi / 180) * RC
    return math.sqrt((dLp ** 2) / (SL ** 2) + (dCp ** 2) / (SC ** 2)
                     + (dhp ** 2) / (SH ** 2) + RT * (dCp / SC) * (dhp / SH))


DE_FNS = {'76': delta_e76, '2000': delta_e2000}


# ===================== 色卡加载 =====================
def load_palette(path):
    rows = []
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        next(reader, None)
        for line in reader:
            if not line or not line[0].strip():
                continue
            pid = line[0].strip()
            name = line[1].strip() if len(line) > 1 and line[1].strip() else line[0].strip()
            hexv = (line[2].strip() if len(line) > 2 else line[1].strip())
            rgb = hex_to_rgb(hexv)
            rows.append({'id': pid, 'name': name, 'hex': hexv.upper(),
                         'rgb': rgb, 'lab': rgb_to_lab(rgb)})
    return rows


# ===================== 算法核心 =====================
def bilateral_filter(pixels, w, h, radius=2, sigma_s=1.5, sigma_c=25.0):
    """逐像素双边滤波(对已缩放到 Board 尺寸的小图极快)。"""
    out = [None] * (w * h)
    r2 = 2 * sigma_s * sigma_s
    c2 = 2 * sigma_c * sigma_c
    for y in range(h):
        for x in range(w):
            r0, g0, b0 = pixels[y * w + x]
            sr = sg = sb = sw = 0.0
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h:
                        r, g, b = pixels[ny * w + nx]
                        wgt = math.exp(-(dx * dx + dy * dy) / r2
                                       - ((r - r0) ** 2 + (g - g0) ** 2 + (b - b0) ** 2) / c2)
                        sr += r * wgt; sg += g * wgt; sb += b * wgt; sw += wgt
            out[y * w + x] = (round(sr / sw), round(sg / sw), round(sb / sw))
    return out


def kmeans(pixels, k, iters=30, seed=0):
    """纯 Python KMeans,用于预降色/稳定色散。"""
    import random
    random.seed(seed)
    n = len(pixels)
    centers = [pixels[random.randrange(n)] for _ in range(k)]
    for _ in range(iters):
        assign = [0] * n
        for i, p in enumerate(pixels):
            best, bd = 0, 1e18
            for c, ce in enumerate(centers):
                d = (p[0] - ce[0]) ** 2 + (p[1] - ce[1]) ** 2 + (p[2] - ce[2]) ** 2
                if d < bd:
                    bd, best = d, c
            assign[i] = best
        acc = [[0, 0, 0] for _ in range(k)]
        cnt = [0] * k
        for i, a in enumerate(assign):
            acc[a][0] += pixels[i][0]; acc[a][1] += pixels[i][1]; acc[a][2] += pixels[i][2]
            cnt[a] += 1
        newc = []
        for c in range(k):
            if cnt[c] > 0:
                newc.append((round(acc[c][0] / cnt[c]), round(acc[c][1] / cnt[c]), round(acc[c][2] / cnt[c])))
            else:
                newc.append(centers[c])
        if newc == centers:
            break
        centers = newc
    return centers


def nearest_index(lab, palette, de_fn):
    best, bd = 0, 1e18
    for i, p in enumerate(palette):
        d = de_fn(lab, p['lab'])
        if d < bd:
            bd, best = d, i
    return best


def merge_rare(grid, palette, w, h, min_count, de_fn):
    """合并颗数 < min_count 的稀有色到最近的高频色,避免工厂"配 1 颗"的极端成本。"""
    flat = [v for row in grid for v in row]
    counts = Counter(flat)
    frequent = {i: c for i, c in counts.items() if c >= min_count}
    if len(frequent) < 2:
        return grid
    result = flat[:]
    for color, cnt in counts.items():
        if cnt >= min_count:
            continue
        best, bd = None, 1e18
        for other in frequent:
            if other == color:
                continue
            d = de_fn(palette[color]['lab'], palette[other]['lab'])
            if d < bd:
                bd, best = d, other
        if best is not None:
            for j, v in enumerate(result):
                if v == color:
                    result[j] = best
    return [result[r * w:(r + 1) * w] for r in range(h)]


def run_pipeline(img_path, palette, size=32, k=24, min_count=5, de_fn=delta_e76, bead='circle'):
    img = Image.open(img_path).convert('RGB')
    img = img.resize((size, size), RESAMPLE)
    w = h = size
    pixels = list(img.getdata())
    den = bilateral_filter(pixels, w, h)                 # 双边滤波降噪
    kmeans(den, k)                                        # KMeans 预降色
    grid = [[0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            grid[y][x] = nearest_index(rgb_to_lab(den[y * w + x]), palette, de_fn)
    grid = merge_rare(grid, palette, w, h, min_count, de_fn)   # 稀有色合并
    return grid


# ===================== BOM =====================
def build_bom(grid, palette):
    flat = [v for row in grid for v in row]
    counts = Counter(flat)
    return sorted(((palette[i], counts[i]) for i in counts),
                  key=lambda t: (-t[1], t[0]['id']))


def export_csv(bom, path):
    with open(path, 'w', newline='', encoding='utf-8') as f:
        wr = csv.writer(f)
        wr.writerow(['color_id', 'color_name', 'hex', 'count'])
        for p, cnt in bom:
            wr.writerow([p['id'], p['name'], p['hex'], cnt])
        wr.writerow(['', '', 'TOTAL', sum(c for _, c in bom)])


# ===================== 像素网格渲染 =====================
def render_grid_png(grid, palette, w, h, path, bead='circle'):
    cell = max(4, int(760 / max(w, h)))
    W, H = w * cell + 22, h * cell + 18
    img = Image.new('RGB', (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for r in range(h):
        for c in range(w):
            col = palette[grid[r][c]]['rgb']
            x0, y0 = 20 + c * cell, r * cell
            if bead == 'circle':
                d.ellipse([x0 + 0.5, y0 + 0.5, x0 + cell - 0.5, y0 + cell - 0.5],
                          fill=col, outline=(130, 130, 130), width=1)
                inner = tuple(max(0, min(255, v - 45)) for v in col)
                inr = cell * 0.42
                d.ellipse([x0 + (cell - inr) / 2, y0 + (cell - inr) / 2,
                           x0 + (cell + inr) / 2, y0 + (cell + inr) / 2], fill=inner)
            else:
                d.rectangle([x0, y0, x0 + cell, y0 + cell], fill=col, outline=(150, 150, 150))
    # 坐标轴
    d.line([(20, 0), (20, h * cell)], fill=(70, 70, 70), width=1)
    d.line([(20, h * cell), (w * cell + 20, h * cell)], fill=(70, 70, 70), width=1)
    try:
        font = ImageFont.truetype('arial.ttf', max(10, min(14, cell)))
    except Exception:
        font = ImageFont.load_default()
    d.text((22, h * cell + 2), '0', fill=(0, 0, 0), font=font)
    d.text((w * cell + 16, h * cell + 2), str(w - 1), fill=(0, 0, 0), font=font)
    d.text((2, 0), '0', fill=(0, 0, 0), font=font)
    d.text((2, h * cell - 12), str(h - 1), fill=(0, 0, 0), font=font)
    img.save(path, format="PNG")


# ===================== PDF 图纸(纯手工生成,无依赖) =====================
def _esc(s):
    return s.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')


def export_pdf(grid, palette, w, h, path, cell=None):
    cell = cell or max(9, int(600 / max(w, h)))
    margin = 36
    grid_w, grid_h = w * cell, h * cell
    page_w = margin + grid_w + 30 + 170
    bom = build_bom(grid, palette)
    line_h = 15
    page_h = margin + grid_h + 26 + len(bom) * line_h + 24 + margin
    top = page_h - margin - grid_h

    ops = []
    ops.append(f'BT /F1 14 Tf {margin} {page_h - margin - 14} Td {_esc("拼豆图纸 Bead Blueprint")} Tj ET')
    for r in range(h):
        for c in range(w):
            col = palette[grid[r][c]]['rgb']
            x = margin + c * cell
            y = top + (h - 1 - r) * cell
            ops.append(f'{col[0]/255:.3f} {col[1]/255:.3f} {col[2]/255:.3f} rg')
            ops.append(f'{x:.2f} {y:.2f} {cell:.2f} {cell:.2f} re f')
            ops.append(f'{col[0]/255:.3f} {col[1]/255:.3f} {col[2]/255:.3f} RG')
            ops.append(f'{x:.2f} {y:.2f} {cell:.2f} {cell:.2f} re S')
    ops.append(f'BT /F1 8 Tf {margin} {top - 14} Td (0) Tj ET')
    ops.append(f'BT /F1 8 Tf {margin + grid_w - 8} {top - 14} Td ({w - 1}) Tj ET')
    ops.append(f'BT /F1 8 Tf {margin - 14} {top} Td (0) Tj ET')
    ops.append(f'BT /F1 8 Tf {margin - 14} {top - grid_h + 4} Td ({h - 1}) Tj ET')
    lx = margin + grid_w + 30
    ops.append(f'BT /F1 10 Tf {lx} {top - 6} Td {_esc("BOM 物料清单")} Tj ET')
    ly = top - 22
    for p, cnt in bom:
        col = p['rgb']
        ops.append(f'{col[0]/255:.3f} {col[1]/255:.3f} {col[2]/255:.3f} rg')
        ops.append(f'{lx} {ly - 10} 10 10 re f')
        ops.append(f'BT /F1 9 Tf {lx + 16} {ly - 9} Td ({p["id"]}) Tj ET')
        ops.append(f'BT /F1 9 Tf {lx + 34} {ly - 9} Td {_esc(p["name"])} Tj ET')
        ops.append(f'BT /F1 9 Tf {lx + 118} {ly - 9} Td ({cnt}) Tj ET')
        ly -= line_h

    stream = '\n'.join(ops) + '\n'
    objs = [
        '<< /Type /Catalog /Pages 2 0 R >>',
        '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:.0f} {page_h:.0f}] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        f'<< /Length {len(stream.encode("latin-1", "replace"))} >>\nstream\n{stream}endstream',
    ]
    out = b'%PDF-1.4\n'
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        b = body.encode('latin-1', 'replace')
        out += f'{i} 0 obj\n{b}\nendobj\n'.encode('latin-1')
    xref_pos = len(out)
    n = len(objs) + 1
    out += f'xref\n0 {n}\n'.encode('latin-1')
    out += b'0000000000 65535 f \n'
    for off in offsets:
        out += f'{off:010d} 00000 n \n'.encode('latin-1')
    out += f'trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF'.encode('latin-1')
    with open(path, 'wb') as fh:
        fh.write(out)


# ===================== CLI =====================
def main():
    ap = argparse.ArgumentParser(description='流水线拼豆 MVP:图片->像素->BOM->PNG/PDF')
    ap.add_argument('image')
    ap.add_argument('--size', type=int, default=32, help='拼豆板尺寸(如 16/32/50)')
    ap.add_argument('--palette', default='palettes/fuse_beads_starter.csv')
    ap.add_argument('--out', default='output')
    ap.add_argument('--bead', choices=['circle', 'square'], default='circle')
    ap.add_argument('--k', type=int, default=24, help='KMeans 预降色簇数')
    ap.add_argument('--min-count', type=int, default=5, help='稀有色合并阈值(颗数)')
    ap.add_argument('--de', choices=['76', '2000'], default='76', help='CIELAB 色差算法')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    palette = load_palette(args.palette)
    de_fn = DE_FNS[args.de]
    grid = run_pipeline(args.image, palette, size=args.size, k=args.k,
                        min_count=args.min_count, de_fn=de_fn, bead=args.bead)
    png = os.path.join(args.out, 'bead_grid.png')
    csvp = os.path.join(args.out, 'bom.csv')
    pdf = os.path.join(args.out, 'blueprint.pdf')
    render_grid_png(grid, palette, args.size, args.size, png)
    export_csv(bp_build_bom(grid, palette), csvp)
    export_pdf(grid, palette, args.size, args.size, pdf)
    bom = bp_build_bom(grid, palette)
    print(f'尺寸 {args.size}x{args.size} | 色卡 {len(palette)} 色 | 用色 {len(bom)} 种 | 总颗数 {sum(c for _,c in bom)}')
    for p, cnt in bom[:12]:
        print(f'  {p["id"]} {p["name"]:<14} {cnt:>6}')
    print(f'输出 -> {png}\n        {csvp}\n        {pdf}')


def bp_build_bom(grid, palette):
    return build_bom(grid, palette)


if __name__ == '__main__':
    main()
