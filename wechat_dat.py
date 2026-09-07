#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信 PC 版加密 .dat 文件解码工具（视频号/聊天视频缓存）
- 微信把视频/图片缓存成 XOR 加密的 .dat
- 暴力枚举 XOR key (0x00-0xFF)，匹配已知文件 magic 还原真实格式
- 纯本地、零依赖、零风险

用法:
  python wechat_dat.py <file.dat>            # 解密到同目录 .dec 文件并打印类型
  python wechat_dat.py --scan <dir>           # 扫描目录，列出可解密的视频
"""
import os
import sys
import glob

# 常见文件头 magic -> 类型
MAGICS = [
    (b"\x89PNG", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF8", "gif"),
    (b"WEBP", "webp"),
    (b"ftyp", "mp4/mov"),   # ftypmp4 / ftypisom (偏移12字节)
    (b"RIFF", "avi/webp"),
    (b"\x00\x00\x00\x18ftyp", "mp4"),
    (b"ID3", "mp3"),
    (b"\xff\xfb", "mp3"),
    (b"OggS", "ogg"),
]


def decrypt_dat(src, dst=None):
    """解密单个 .dat，返回 (type, key)。失败返回 (None, -1)。"""
    with open(src, "rb") as f:
        data = f.read()
    if len(data) < 12:
        return None, -1
    head = data[:16]
    for key in range(256):
        d0 = head[0] ^ key
        d1 = head[1] ^ key
        d2 = head[2] ^ key
        d3 = head[3] ^ key
        for magic, t in MAGICS:
            if len(magic) == 4 and bytes([d0, d1, d2, d3]) == magic:
                out = bytes(b ^ key for b in data)
                if dst:
                    with open(dst, "wb") as f:
                        f.write(out)
                return t, key
            # ftyp 在偏移 4 的情况 (前4字节是 size)
            if magic == b"ftyp" and len(data) > 12:
                if bytes([head[4] ^ key, head[5] ^ key,
                          head[6] ^ key, head[7] ^ key]) == b"ftyp":
                    out = bytes(b ^ key for b in data)
                    if dst:
                        with open(dst, "wb") as f:
                            f.write(out)
                    return "mp4/mov", key
    return None, -1


def scan_dir(directory, min_size=50000):
    """扫描目录，返回 [(dat_path, type, dec_path)] 中可解密为视频的。"""
    results = []
    for fp in glob.glob(os.path.join(directory, "**", "*.dat"), recursive=True):
        if os.path.getsize(fp) < min_size:
            continue
        dec = fp + ".dec"
        t, key = decrypt_dat(fp, dec)
        if t and ("mp4" in t or "mov" in t or "avi" in t):
            results.append((fp, t, key, dec))
        else:
            if os.path.exists(dec):
                os.remove(dec)
    return results


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--scan":
        d = sys.argv[2]
        print(f"[scan] {d}")
        for fp, t, key, dec in scan_dir(d):
            print(f"  VIDEO key={key:02x} -> {dec}  ({os.path.getsize(fp)//1024}KB)")
        print("[done]")
    elif len(sys.argv) >= 2:
        fp = sys.argv[1]
        dec = fp + ".dec"
        t, key = decrypt_dat(fp, dec)
        print(f"src={fp} ({os.path.getsize(fp)//1024}KB)")
        print(f"type={t} key={key:#x}" + (f" -> {dec}" if t else " (不可解密)"))
    else:
        print("usage: python wechat_dat.py <file.dat> | --scan <dir>")
