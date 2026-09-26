import os
import hashlib
from collections import defaultdict


def get_hash(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def find_duplicates(folder, min_size=1):
    # 第 1 步：按大小分组
    size_map = defaultdict(list)
    for root, dirs, files in os.walk(folder, onerror=lambda e: None):
        for name in files:
            full = os.path.join(root, name)
            try:
                size = os.path.getsize(full)
                if size >= min_size:
                    size_map[size].append(full)
            except OSError:
                continue

    # 第 2 步：只对"大小相同"的算哈希
    hash_map = defaultdict(list)
    for size, paths in size_map.items():
        if len(paths) < 2:
            continue
        for path in paths:
            try:
                hash_map[get_hash(path)].append(path)
            except OSError:
                continue

    # 第 3 步：只留真正重复的
    return {h: paths for h, paths in hash_map.items() if len(paths) > 1}


if __name__ == '__main__':
    folder = input("要扫描的文件夹（可拖进来）: ").strip().strip('"')

    print("扫描中...")
    dups = find_duplicates(folder)

    if not dups:
        print("没有重复文件")
    else:
        total = 0
        for paths in dups.values():
            size = os.path.getsize(paths[0]) * (len(paths) - 1)
            total += size
        print(f"\n发现 {len(dups)} 组重复，可省 {total/1024/1024:.1f} MB\n")

        # 按可省空间从大到小
        groups = sorted(
            dups.items(),
            key=lambda x: -os.path.getsize(x[1][0]) * (len(x[1]) - 1)
        )
        for i, (h, paths) in enumerate(groups[:20], 1):   # 只显示前 20 组
            mb = os.path.getsize(paths[0]) / 1024 / 1024
            print(f"【{i}】{mb:.1f} MB × {len(paths)} 个")
            for p in paths:
                print(f"    {p}")

        if len(groups) > 20:
            print(f"\n（还有 {len(groups)-20} 组，如需全部请写到文件）")

    input("\n按回车退出...")
