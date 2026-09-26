# -*- coding: utf-8 -*-
"""
重复文件查找工具 v3
功能：
  - 支持跨盘 / 多目标扫描（自动检测所有盘符）
  - 每个目标独立断点，互不覆盖
  - 扫描前预估总数，进度条显示真实百分比
  - 两阶段扫描（先按大小分组，再对候选算哈希）
  - 文件夹级折叠（整个文件夹重复只报一条）
  - 报告归档，不删旧报告
  - 报告里标注文件来源盘
  - 支持 Ctrl+C 中断续跑
"""

import os
import sys
import time
import json
import gzip
import string
import hashlib
import platform
from collections import defaultdict


# ============ 可调参数 ============

EXCLUDE_DIRS = {
    'Windows', 'System32', 'SysWOW64', 'WinSxS',
    '$Recycle.Bin', 'System Volume Information',
    'ProgramData', 'Recovery', 'PerfLogs',
    'node_modules', '.git', '.svn', 'venv', '.venv',
    '__pycache__', 'site-packages', 'AppData',
    'Library', 'Applications', '.Trash', '.Spotlight-V100',
}

MIN_SIZE = 0          # 小于 1KB 跳过
HASH_CHUNK = 65536
LOG_FILE = 'duplicates_report.txt'
USE_GZIP = False          # 断点是否压缩（True 体积小，False 可读）
CHECKPOINT_APPEND = True
APPEND_FLUSH_EVERY = 500


# ============ 工具函数 ============

def format_size(n):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB', 'PB']:
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def should_skip_dir(dirname):
    return dirname in EXCLUDE_DIRS or dirname.startswith('.')


def get_file_hash(path, chunk_size=HASH_CHUNK):
    hasher = hashlib.md5()
    with open(path, 'rb') as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def print_line(text, width=110):
    print(text[:width].ljust(width), end='\r', flush=True)


def target_key(target):
    """把目标路径变成一个安全的文件名片段"""
    safe = target.replace(':', '').replace('\\', '_').replace('/', '_')
    safe = safe.strip('_') or 'root'
    return safe[:40]


# ============ 断点（追加模式，JSONL） ============

def checkpoint_path(target):
    """追加模式用 .jsonl 后缀"""
    return f"scan_checkpoint_{target_key(target)}.jsonl"


def append_checkpoint(target, new_paths=None, new_hashes=None, stage=None):
    """
    把新完成的一段追加到断点文件末尾。
    - new_paths: 本次新算完哈希的文件路径列表
    - new_hashes: 本次新算出的 {hash: [paths]}（可选，用于加速）
    - stage: 阶段标记（可选，用于阶段 1 的续跑）
    """
    path = checkpoint_path(target)
    record = {}
    if stage is not None:
        record['stage'] = stage
    if new_paths:
        record['done'] = list(new_paths)
    if new_hashes:
        record['hashes'] = new_hashes

    if not record:
        return

    try:
        line = json.dumps(record, ensure_ascii=False)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception as e:
        print(f"\n⚠ 追加断点失败: {e}")


def append_scan_dirs(target, new_dirs, size_map_delta=None):
    """阶段 1 专用：追加已扫描的目录 + 新增的大小分组"""
    path = checkpoint_path(target)
    record = {'scanned_dirs': list(new_dirs)}
    if size_map_delta:
        # size_map_delta 结构: {size: [paths]}，key 转字符串
        record['size_delta'] = {str(k): v for k, v in size_map_delta.items()}

    try:
        line = json.dumps(record, ensure_ascii=False)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception as e:
        print(f"\n⚠ 追加断点失败: {e}")


def load_checkpoint(target):
    """
    读断点：逐行读，把所有片段合并。
    返回 {stage, scanned_dirs, size_map, hash_map, done_paths}
    """
    path = checkpoint_path(target)
    if not os.path.exists(path):
        return {}

    scanned_dirs = set()
    size_map = defaultdict(list)
    hash_map = defaultdict(list)
    done_paths = set()
    stage = None

    try:
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue   # 坏行跳过

                if 'stage' in rec:
                    stage = rec['stage']
                for d in rec.get('scanned_dirs', []):
                    scanned_dirs.add(d)
                for k, v in rec.get('size_delta', {}).items():
                    size_map[int(k)].extend(v)
                for p in rec.get('done', []):
                    done_paths.add(p)
                for h, paths in rec.get('hashes', {}).items():
                    hash_map[h].extend(paths)
    except Exception:
        return {}

    return {
        'stage': stage,
        'scanned_dirs': list(scanned_dirs),
        'size_map': dict(size_map),
        'hash_map': dict(hash_map),
        'done_paths': list(done_paths),
    }


def clear_checkpoint(target):
    path = checkpoint_path(target)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
        
# ============ 预估总数 ============

def estimate_total(folder):
    total = 0
    print("正在预估文件总数...")
    for root, dirs, files in os.walk(folder, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if not should_skip_dir(d)]
        total += len(files)
    return total


# ============ 阶段 1：遍历 + 按大小分组 ============

def scan_sizes(folder, total_estimate, checkpoint, target):
    size_map = defaultdict(list)
    count = 0
    scanned_dirs = set(checkpoint.get('scanned_dirs', []))
    for k, v in checkpoint.get('size_map', {}).items():
        size_map[int(k)] = v

    # 记录"上次已保存"的基准
    saved_dirs = set(scanned_dirs)
    saved_sizes = set()   # 已保存过的 (size, path) 组合，避免重复追加
    for size, paths in size_map.items():
        for p in paths:
            saved_sizes.add((size, p))

    start = time.time()
    print(f"阶段 1/2：遍历文件、按大小分组")

    for root, dirs, files in os.walk(folder, onerror=lambda e: None):
        dirs[:] = [d for d in dirs if not should_skip_dir(d)]
        if root in scanned_dirs:
            count += len(files)
            continue

        for name in files:
            count += 1
            full = os.path.join(root, name)
            try:
                size = os.path.getsize(full)
                if size >= MIN_SIZE:
                    size_map[size].append(full)
            except OSError:
                continue

        scanned_dirs.add(root)

        if count % 100 < len(files):
            elapsed = time.time() - start
            speed = count / elapsed if elapsed > 0 else 0
            percent = count / total_estimate * 100 if total_estimate else 0
            bar_len = 30
            filled = int(bar_len * count / total_estimate) if total_estimate else 0
            bar = '█' * filled + '░' * (bar_len - filled)
            print_line(f"[{bar}] {percent:5.1f}% | {count}/{total_estimate} | "
                       f"{speed:.0f} 个/秒 | {root[-45:]}")

        # ===== 新增：每 5000 个文件追加一次"这一段" =====
        if count % 5000 == 0:
            new_dirs = scanned_dirs - saved_dirs
            # 只挑出还没保存过的新文件
            size_delta = defaultdict(list)
            for size, paths in size_map.items():
                for p in paths:
                    if (size, p) not in saved_sizes:
                        size_delta[size].append(p)
                        saved_sizes.add((size, p))

            if new_dirs or size_delta:
                append_scan_dirs(target, new_dirs, size_delta)
                saved_dirs |= new_dirs
                append_checkpoint(target, stage='size')

    elapsed = time.time() - start
    print_line(f"阶段 1 完成：{count} 个文件，用时 {elapsed:.1f} 秒" + " " * 60)
    print()
    return size_map, scanned_dirs

# ============ 阶段 2：算哈希 ============

def scan_hashes(size_map, checkpoint, target):
    candidates = []
    for size, paths in size_map.items():
        if len(paths) > 1:
            candidates.extend(paths)

    hash_map = defaultdict(list)
    for k, v in checkpoint.get('hash_map', {}).items():
        hash_map[k] = v
    done_paths = set(checkpoint.get('done_paths', []))
    todo = [p for p in candidates if p not in done_paths]

    print(f"阶段 2/2：对 {len(candidates)} 个候选文件计算哈希")
    if done_paths:
        print(f"（断点续跑，已完成 {len(done_paths)}，剩余 {len(todo)}）")
    print("（这一步要读文件内容，较慢）\n")

    start = time.time()
    total = len(candidates)
    done_count = len(done_paths)
    already = len(done_paths)

    buffer = []
    buffer_hashes = {}

    for path in todo:
        done_count += 1
        try:
            h = get_file_hash(path)
            hash_map[h].append(path)
            buffer_hashes.setdefault(h, []).append(path)
        except (OSError, PermissionError):
            pass
        done_paths.add(path)
        buffer.append(path)

        if done_count % 20 == 0 or done_count == total:
            elapsed = time.time() - start
            speed = (done_count - already) / elapsed if elapsed > 0 else 0
            percent = done_count / total * 100 if total else 100
            bar_len = 30
            filled = int(bar_len * done_count / total) if total else bar_len
            bar = '█' * filled + '░' * (bar_len - filled)
            print_line(f"[{bar}] {percent:5.1f}% | {done_count}/{total} | "
                       f"{speed:.0f} 个/秒 | {os.path.basename(path)[-40:]}")

        # ===== 追加写：每 500 个落一次盘 =====
        if len(buffer) >= APPEND_FLUSH_EVERY:
            append_checkpoint(target, new_paths=buffer,
                              new_hashes=buffer_hashes, stage='hash')
            buffer = []
            buffer_hashes = {}

    # 收尾：把最后不足 500 的也写掉
    if buffer:
        append_checkpoint(target, new_paths=buffer,
                          new_hashes=buffer_hashes, stage='hash')

    elapsed = time.time() - start
    print_line(f"阶段 2 完成：用时 {elapsed:.1f} 秒" + " " * 60)
    print()

    return {h: paths for h, paths in hash_map.items() if len(paths) > 1}

# ============ 文件夹级折叠 ============

def build_folder_signature(folder, file_hashes):
    items = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not should_skip_dir(d)]
        for name in files:
            full = os.path.join(root, name)
            h = file_hashes.get(full)
            if h is None:
                try:
                    size = os.path.getsize(full)
                    h = f"__unique__{size}"
                except OSError:
                    continue
            rel = os.path.relpath(full, folder)
            items.append((rel, h))
    items.sort()
    return tuple(items)


def collapse_duplicate_folders(duplicates):
    candidate_folders = set()
    for paths in duplicates.values():
        for p in paths:
            candidate_folders.add(os.path.dirname(p))

    file_hashes = {}
    for h, paths in duplicates.items():
        for p in paths:
            file_hashes[p] = h

    folder_signatures = {}
    for folder in candidate_folders:
        try:
            sig = build_folder_signature(folder, file_hashes)
            if sig:
                folder_signatures[folder] = sig
        except Exception:
            continue

    sig_groups = defaultdict(list)
    for folder, sig in folder_signatures.items():
        sig_groups[sig].append(folder)

    folder_groups = []
    covered_folders = set()
    for sig, folders in sig_groups.items():
        if len(folders) < 2:
            continue
        folders = [f for f in folders if f not in covered_folders]
        if len(folders) < 2:
            continue
        folder_groups.append((folders, sig))
        for f in folders:
            covered_folders.add(f)
            for root, dirs, files in os.walk(f):
                covered_folders.add(root)

    covered_files = set()
    for folders, sig in folder_groups:
        for f in folders:
            for root, dirs, files in os.walk(f):
                for name in files:
                    covered_files.add(os.path.join(root, name))

    remaining = {}
    for h, paths in duplicates.items():
        rest = [p for p in paths if p not in covered_files]
        if len(rest) > 1:
            remaining[h] = rest

    return folder_groups, remaining


def describe_folder_group(folders, sig):
    total_files = len(sig)
    total_size = 0
    for f in folders:
        for root, dirs, files in os.walk(f):
            for name in files:
                try:
                    total_size += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    per_copy = total_size // len(folders) if folders else 0
    wasted = per_copy * (len(folders) - 1)
    return total_files, per_copy, wasted


# ============ 标记来源盘 ============

def tag_source(path, targets):
    """给路径标上是哪个目标盘/文件夹"""
    # 优先匹配更长的路径前缀（避免 C:\ 匹配到所有）
    matched = None
    for t in targets:
        t_norm = os.path.normpath(t)
        p_norm = os.path.normpath(path)
        if p_norm.startswith(t_norm):
            if matched is None or len(t_norm) > len(matched):
                matched = t_norm
    if matched:
        # 盘符形式
        if len(matched) == 3 and matched[1] == ':' and matched.endswith('\\'):
            return f"[{matched[0]}]"
        return f"[{os.path.basename(matched) or matched}]"
    return "[?]"


# ============ 写报告（归档旧报告） ============

def archive_old_report():
    """旧报告改名留档，不删"""
    if os.path.exists(LOG_FILE):
        ts = time.strftime('%Y%m%d_%H%M%S')
        archive = f"duplicates_report_{ts}.txt"
        try:
            os.rename(LOG_FILE, archive)
            print(f"（旧报告已归档为 {archive}）")
        except Exception as e:
            print(f"（归档旧报告失败：{e}）")


def write_report(folder_groups, remaining, targets):
    archive_old_report()

    lines = []
    lines.append("=" * 70)
    lines.append("重复文件报告")
    lines.append(f"扫描目标: {', '.join(targets)}")
    lines.append(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 70)
    lines.append("")

    if folder_groups:
        lines.append(f"【文件夹整体重复】共 {len(folder_groups)} 组")
        lines.append("-" * 70)
        for i, (folders, sig) in enumerate(folder_groups, start=1):
            n, per_copy, wasted = describe_folder_group(folders, sig)
            lines.append(f"\n第 {i} 组：{n} 个文件/份，每份 {format_size(per_copy)}，"
                         f"共 {len(folders)} 份，可省 {format_size(wasted)}")
            for f in folders:
                lines.append(f"    {tag_source(f, targets)} {f}")
        lines.append("")

    if remaining:
        lines.append(f"【文件级重复（未折叠成文件夹的）】共 {len(remaining)} 组")
        lines.append("-" * 70)
        sorted_groups = sorted(
            remaining.items(),
            key=lambda x: -os.path.getsize(x[1][0]) * (len(x[1]) - 1)
        )
        for i, (h, paths) in enumerate(sorted_groups, start=1):
            size = os.path.getsize(paths[0])
            wasted = size * (len(paths) - 1)
            lines.append(f"\n第 {i} 组：{format_size(size)} × {len(paths)} 个，"
                         f"可省 {format_size(wasted)}")
            for p in paths:
                lines.append(f"    {tag_source(p, targets)} {p}")

    if not folder_groups and not remaining:
        lines.append("没有发现重复文件")

    lines.append("")
    lines.append("=" * 70)
    total_wasted = 0
    if folder_groups:
        for folders, sig in folder_groups:
            _, _, wasted = describe_folder_group(folders, sig)
            total_wasted += wasted
    if remaining:
        for paths in remaining.values():
            total_wasted += os.path.getsize(paths[0]) * (len(paths) - 1)
    lines.append(f"可释放空间合计约: {format_size(total_wasted)}")
    lines.append("=" * 70)

    text = "\n".join(lines)
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        f.write(text)
    return text


# ============ 选择目标 ============

def detect_drives():
    """自动找出所有盘符"""
    drives = []
    if platform.system() == 'Windows':
        for d in string.ascii_uppercase:
            p = f"{d}:\\"
            if os.path.exists(p):
                drives.append(p)
    else:
        drives.append("/")
    return drives


def choose_targets():
    print("=" * 55)
    print("请选择扫描模式：")
    print("  1. 扫描所有盘（跨盘找重复）")
    print("  2. 扫描指定文件夹（可多个，逗号隔开）")
    print("=" * 55)
    choice = input("输入 1 或 2: ").strip()

    if choice == '1':
        drives = detect_drives()
        print(f"\n检测到磁盘: {drives}")
        confirm = input("确认扫描这些盘？(y/n): ").strip().lower()
        if confirm != 'y':
            return []
        return drives

    raw = input("输入文件夹路径（多个用逗号隔开，可直接拖进来）: ").strip()
    return [p.strip().strip('"') for p in raw.split(',') if p.strip()]


# ============ 主流程 ============

def main():
    if len(sys.argv) > 1:
        targets = sys.argv[1:]
    else:
        targets = choose_targets()

    valid = [t for t in targets if os.path.exists(t)]
    if not valid:
        print("没有有效的路径")
        return

    print(f"\n将扫描 {len(valid)} 个目标:")
    for t in valid:
        print(f"    {t}")

    overall_start = time.time()
    all_duplicates = defaultdict(list)

    for idx, target in enumerate(valid, 1):
        print(f"\n{'='*60}")
        print(f"[{idx}/{len(valid)}] 扫描: {target}")
        print(f"{'='*60}")

        checkpoint = load_checkpoint(target)
        if checkpoint:
            print(f"⚠ 发现 {target} 的断点（阶段: {checkpoint.get('stage')}）")
            if input("继续上次扫描？(y/n): ").strip().lower() != 'y':
                clear_checkpoint(target)
                checkpoint = {}
            else:
                print("从断点继续...")

        try:
            if checkpoint.get('scanned_dirs'):
                total_estimate = 0
            else:
                total_estimate = estimate_total(target)

            if not checkpoint.get('scanned_dirs'):
                size_map, scanned_dirs = scan_sizes(
                    target, total_estimate, checkpoint, target
                )
                append_checkpoint(target, stage='size_done')
            else:
                size_map = defaultdict(list)
                for k, v in checkpoint.get('size_map', {}).items():
                    size_map[int(k)] = v
                print("阶段 1 已完成，跳过。")

            checkpoint = load_checkpoint(target)
            duplicates = scan_hashes(size_map, checkpoint, target)

            for h, paths in duplicates.items():
                all_duplicates[h].extend(paths)

        except KeyboardInterrupt:
            print(f"\n\n⚠ Ctrl+C，{target} 的断点已保存。")
            print("下次运行选“继续”即可接着扫。")
            return

    overall_elapsed = time.time() - overall_start

    print(f"\n{'='*60}")
    print("正在做文件夹级折叠...")
    folder_groups, remaining = collapse_duplicate_folders(dict(all_duplicates))

    report = write_report(folder_groups, remaining, valid)
    print(f"\n总用时: {overall_elapsed:.1f} 秒")
    print(f"完整报告已保存到: {LOG_FILE}")

    # 屏幕摘要
    print("\n" + "=" * 60)
    if folder_groups:
        print(f"【文件夹整体重复】{len(folder_groups)} 组")
    if remaining:
        print(f"【文件级重复】{len(remaining)} 组")

    SHOW = 8
    if folder_groups:
        sorted_fg = sorted(
            folder_groups,
            key=lambda x: -describe_folder_group(x[0], x[1])[2]
        )
        print(f"\n占用最大的文件夹重复组（前 {min(SHOW, len(sorted_fg))} 个）：")
        for i, (folders, sig) in enumerate(sorted_fg[:SHOW], start=1):
            n, per_copy, wasted = describe_folder_group(folders, sig)
            print(f"\n第 {i} 组：{n} 个文件/份 × {len(folders)} 份，"
                  f"可省 {format_size(wasted)}")
            for f in folders:
                print(f"    {tag_source(f, valid)} {f}")
        if len(sorted_fg) > SHOW:
            print(f"\n（还有 {len(sorted_fg) - SHOW} 组见报告文件）")

    print("\n" + "=" * 60)

    # 全部完成后，清理各目标断点
    for t in valid:
        clear_checkpoint(t)
    print("所有断点已清理。")

    input("\n按回车键退出...")


if __name__ == '__main__':
    main()
