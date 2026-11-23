#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Баннер для рассылки: ночной город + такси.

Запуск: python3 make_banner.py [ГОРОД] [выходной-файл]
"""
import sys

from PIL import Image, ImageDraw, ImageFont, ImageFilter

CITY = (sys.argv[1] if len(sys.argv) > 1 else "ЕНАКИЕВО").upper()
OUT = sys.argv[2] if len(sys.argv) > 2 else "post.png"

W, H = 1280, 670
FONT_BOLD = "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/TTF/DejaVuSans.ttf"

img = Image.new("RGB", (W, H), (8, 14, 30))
d = ImageDraw.Draw(img)

# --- ночное небо: вертикальный градиент
for y in range(H):
    t = y / H
    r = int(8 + 40 * t)
    g = int(14 + 34 * t)
    b = int(30 + 60 * t)
    d.line([(0, y), (W, y)], fill=(r, g, b))

# --- мягкие блики (городские огни)
glow = Image.new("RGB", (W, H), (0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.ellipse([W * 0.55, -180, W * 1.15, 320], fill=(255, 170, 40))
gd.ellipse([-260, H * 0.45, 420, H * 1.3], fill=(30, 110, 220))
gd.ellipse([W * 0.35, H * 0.62, W * 0.95, H * 1.2], fill=(255, 120, 20))
glow = glow.filter(ImageFilter.GaussianBlur(150))
img = Image.blend(img, Image.blend(img, glow, 0.55), 0.6)
d = ImageDraw.Draw(img)

# --- силуэт города
sky = int(H * 0.58)
blocks = [
    (0, 120, 60), (70, 180, 46), (130, 90, 74), (215, 150, 38), (265, 210, 60),
    (340, 120, 52), (400, 170, 80), (490, 100, 44), (545, 200, 62), (620, 140, 40),
    (670, 110, 70), (750, 190, 54), (815, 130, 46), (870, 160, 66), (945, 100, 40),
    (995, 180, 58), (1060, 120, 50), (1120, 200, 70), (1200, 140, 44), (1250, 180, 30),
]
for x, seed, bw in blocks:
    bh = 70 + (seed % 7) * 26
    d.rectangle([x, sky - bh, x + bw, sky + 10], fill=(6, 10, 22))

# окна
import random
random.seed(7)
for x, seed, bw in blocks:
    bh = 70 + (seed % 7) * 26
    for wy in range(sky - bh + 12, sky - 6, 16):
        for wx in range(x + 6, x + bw - 8, 14):
            if random.random() < 0.28:
                d.rectangle([wx, wy, wx + 5, wy + 7], fill=(255, 205, 110))

# --- дорога
road_top = sky + 10
d.rectangle([0, road_top, W, H], fill=(12, 15, 24))
for i in range(-3, 14):
    x = i * 110
    d.polygon(
        [(W / 2 + (x - W / 2) * 0.45, road_top),
         (W / 2 + (x - W / 2) * 0.45 + 26, road_top),
         (W / 2 + (x - W / 2) * 1.9 + 46, H),
         (W / 2 + (x - W / 2) * 1.9, H)],
        fill=(26, 30, 40),
    )

# --- шашечки такси сверху
c = 30
for i in range(W // c + 1):
    for row in range(2):
        if (i + row) % 2 == 0:
            d.rectangle([i * c, row * c, i * c + c, row * c + c], fill=(250, 250, 250))
        else:
            d.rectangle([i * c, row * c, i * c + c, row * c + c], fill=(10, 10, 12))

# --- жёлтая плашка с заголовком
pad = 64
y0 = 150
box_h = 250
d.rounded_rectangle([pad, y0, pad + 690, y0 + box_h], radius=28, fill=(255, 205, 20))
d.rounded_rectangle([pad, y0, pad + 690, y0 + box_h], radius=28, outline=(255, 240, 160), width=3)

f_title = ImageFont.truetype(FONT_BOLD, 78)
f_sub = ImageFont.truetype(FONT_BOLD, 40)
f_small = ImageFont.truetype(FONT_BOLD, 30)

d.text((pad + 40, y0 + 28), "ТАКСИ", font=f_title, fill=(20, 20, 22))
d.text((pad + 40, y0 + 112), CITY, font=f_title, fill=(20, 20, 22))
d.text((pad + 42, y0 + 198), "Подача от 5 минут · круглосуточно",
       font=f_small, fill=(45, 40, 20))

# --- тёмная подложка под подпись справа
px0, py0, px1, py1 = W - 512, 140, W - 44, 372
overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
od = ImageDraw.Draw(overlay)
od.rounded_rectangle([px0, py0, px1, py1], radius=28, fill=(6, 10, 22, 214))
od.rounded_rectangle([px0, py0, px1, py1], radius=28, outline=(255, 205, 20, 200), width=3)
img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
d = ImageDraw.Draw(img)

# --- подпись справа
f_big = ImageFont.truetype(FONT_BOLD, 52)
f_mid = ImageFont.truetype(FONT_REG, 33)
d.text((px0 + 38, py0 + 34), "Заказ", font=f_mid, fill=(255, 214, 60))
d.text((px0 + 38, py0 + 72), "в 2 клика", font=f_big, fill=(255, 255, 255))
d.text((px0 + 38, py0 + 146), "без звонков", font=f_mid, fill=(198, 210, 228))
d.text((px0 + 38, py0 + 186), "и ожидания", font=f_mid, fill=(198, 210, 228))

# --- стрелка-указатель вниз
ax, ay = W - 278, 408
d.polygon([(ax - 60, ay - 10), (ax + 60, ay - 10), (ax + 60, ay + 40), (ax, ay + 95), (ax - 60, ay + 40)],
          fill=(255, 205, 20))
d.text((ax - 40, ay + 4), "ЗАКАЗ", font=ImageFont.truetype(FONT_BOLD, 26), fill=(20, 20, 22))

img.save(OUT, quality=95)
print("ok", img.size)
