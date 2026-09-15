import os
import struct
import zlib
import math

def create_png(width, height, get_pixel_color):
    """
    Python標準ライブラリ（struct, zlib）のみでPNG画像をバイナリ生成する
    """
    raw_rows = []
    for y in range(height):
        # フィルタータイプ 0 (None)
        row = bytearray([0])
        for x in range(width):
            r, g, b, a = get_pixel_color(x, y, width, height)
            row.extend([r, g, b, a])
        raw_rows.append(bytes(row))
    
    raw_data = b"".join(raw_rows)
    compressed_data = zlib.compress(raw_data, level=9)
    
    def make_chunk(chunk_type, data):
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
        return length + chunk_type + data + crc
    
    png_signature = b"\x89PNG\r\n\x1a\n"
    # IHDR: width, height, bit_depth=8, color_type=6 (RGBA), comp=0, filter=0, interlace=0
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr_chunk = make_chunk(b"IHDR", ihdr_data)
    idat_chunk = make_chunk(b"IDAT", compressed_data)
    iend_chunk = make_chunk(b"IEND", b"")
    
    return png_signature + ihdr_chunk + idat_chunk + iend_chunk

def icon_shader(x, y, w, h):
    """
    洗練されたAIアプリ風のグラデーションアイコンを描画
    ダークネイビー背景 + 中央のグロー・4点スパーク（AI星）
    """
    # 正規化座標 (-1.0 to 1.0)
    nx = (x / (w - 1)) * 2 - 1
    ny = (y / (h - 1)) * 2 - 1
    dist = math.sqrt(nx * nx + ny * ny)

    # 背景グラデーション (斜め方向: インディゴ -> ディープブルー)
    bg_t = (nx + ny + 2) / 4.0
    bg_r = int(15 * (1 - bg_t) + 30 * bg_t)
    bg_g = int(23 * (1 - bg_t) + 58 * bg_t)
    bg_b = int(42 * (1 - bg_t) + 138 * bg_t)

    # 中央の円形グロー（シアン〜パープル）
    glow = max(0.0, 1.0 - dist * 1.3)
    glow = glow * glow

    r = bg_r + glow * 80
    g = bg_g + glow * 120
    b = bg_b + glow * 240

    # 4点AIスター（星形スパークル）の計算
    ax = abs(nx)
    ay = abs(ny)
    # 星の形状関数
    star_dist = math.pow(ax, 0.5) + math.pow(ay, 0.5)
    if star_dist < 0.85:
        star_intensity = max(0.0, 1.0 - (star_dist / 0.85))
        star_intensity = math.pow(star_intensity, 1.5)
        # 白色〜明るい水色
        r = r * (1 - star_intensity) + 245 * star_intensity
        g = g * (1 - star_intensity) + 250 * star_intensity
        b = b * (1 - star_intensity) + 255 * star_intensity

    # クランプ
    r = min(255, max(0, int(r)))
    g = min(255, max(0, int(g)))
    b = min(255, max(0, int(b)))
    return (r, g, b, 255)

def main():
    os.makedirs("docs", exist_ok=True)
    sizes = [180, 192, 512]
    
    for size in sizes:
        filename = f"icon-{size}.png"
        filepath = os.path.join("docs", filename)
        print(f"Generating {filepath} ({size}x{size})...")
        png_bytes = create_png(size, size, icon_shader)
        with open(filepath, "wb") as f:
            f.write(png_bytes)
        print(f"Done: {filepath} ({len(png_bytes)} bytes)")

if __name__ == "__main__":
    main()
